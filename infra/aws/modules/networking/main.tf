# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Networking Module (AWS)
# VPC + one PUBLIC subnet per AZ, an Internet Gateway, and a free S3 gateway
# endpoint. Deliberately NO private subnets and NO NAT gateway (cost decision): nodes get public IPs, like the GKE public-node setup.
#
# Raw resources (not terraform-aws-modules/vpc) — it's ~60 lines, and matches
# the raw style of the GCP/Azure networking modules.
#
# Gotchas baked in:
#  - map_public_ip_on_launch = true is REQUIRED: a managed node group in a public
#    subnet without a public IP can't reach the EKS API / ECR / S3 and fails with
#    "Instances failed to join the kubernetes cluster".
#  - kubernetes.io/role/elb = 1 lets the EKS service controller place
#    internet-facing NLBs in these subnets.
#  - EKS needs subnets in >= 2 AZs for the control plane, even though the worker
#    and GPU node groups use only one.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # required by EKS

  tags = merge(var.tags, { Name = "${var.name_prefix}-vpc" })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = merge(var.tags, { Name = "${var.name_prefix}-igw" })
}

resource "aws_subnet" "public" {
  for_each = { for i, az in var.azs : az => i }

  vpc_id                  = aws_vpc.main.id
  availability_zone       = each.key
  cidr_block              = cidrsubnet(var.vpc_cidr, var.subnet_newbits, each.value)
  map_public_ip_on_launch = true

  tags = merge(var.tags, {
    Name                     = "${var.name_prefix}-public-${each.key}"
    "kubernetes.io/role/elb" = "1"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-public" })
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# S3 gateway endpoint (free): pgBackRest + MinIO-backup traffic and ECR image
# layers (served from S3) stay inside the VPC.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]

  tags = merge(var.tags, { Name = "${var.name_prefix}-s3" })
}
