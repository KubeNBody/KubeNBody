# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Networking Module Outputs

output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}

output "vpc_cidr" {
  description = "VPC CIDR"
  value       = aws_vpc.main.cidr_block
}

output "public_subnet_ids" {
  description = "Public subnet IDs, in var.azs order"
  value       = [for az in var.azs : aws_subnet.public[az].id]
}

output "subnet_ids_by_az" {
  description = "Public subnet ID per AZ (cluster stack picks the worker_zone subnet from this)"
  value       = { for az, s in aws_subnet.public : az => s.id }
}

output "public_route_table_id" {
  description = "Route table of the public subnets"
  value       = aws_route_table.public.id
}
