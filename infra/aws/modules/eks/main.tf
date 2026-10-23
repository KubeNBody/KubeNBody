# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# EKS Module — cluster, managed node groups, add-ons
# Wraps terraform-aws-modules/eks v21 (a hand-written EKS module would be
# ~400+ lines of IAM, launch templates, SGs, OIDC and access entries). All other
# AWS modules here are raw resources, like the GCP/Azure modules.
#
# v21 input names: name, kubernetes_version, endpoint_*, addons,
# upgrade_policy — most examples online still use the v20 `cluster_*` names.
#
# Baked-in decisions:
#  - Access entries (API mode) + creator admin — the AWS form of AKS local admin.
#  - Pinned version + STANDARD support policy: never drift into extended-support
#    pricing (6x control plane).
#  - System node group across all AZs; worker + GPU pinned to worker_zone (like
#    GKE/AKS). Fixed counts, scaled manually; the module ignores desired_size
#    changes after creation, so `make scale-*` is never reverted by TF.
#  - Root disks via block_device_mappings: the module's default custom launch
#    template IGNORES `disk_size` (module FAQ) — 20 GiB would DiskPressure with
#    the CUDA image.
#  - Node SG self-ingress on all ports: the module default only allows 1025-65535
#    + DNS between nodes.
#  - attach_cluster_primary_security_group = false on every node group (v21
#    default, set explicitly): only ONE cluster-tagged SG on node ENIs, otherwise
#    the built-in LB controller fails "Multiple tagged security groups found".
#  - IMDS hop limit 1 (v21 default): pods can't reach instance metadata — fine,
#    everything that needs AWS creds uses IRSA.
#  - No customer KMS key and no CloudWatch control-plane logs (module defaults
#    are on): every up/down cycle would leave a KMS key pending deletion and bill
#    log ingestion. EKS already envelope-encrypts Kubernetes secrets with an
#    AWS-owned key; a portability test needs neither.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

locals {
  root_volume = {
    xvda = {
      device_name = "/dev/xvda" # AL2023 root device
      ebs = {
        volume_type           = "gp3"
        encrypted             = true
        delete_on_termination = true
      }
    }
  }

  root_disk = {
    for ng, size in {
      system    = var.system_disk_gib
      workercpu = var.worker_disk_gib
      gpu       = var.gpu_disk_gib
      } : ng => {
      xvda = merge(local.root_volume.xvda, {
        ebs = merge(local.root_volume.xvda.ebs, { volume_size = size })
      })
    }
  }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id     = var.vpc_id
  subnet_ids = var.subnet_ids # control plane ENIs + system nodes (>= 2 AZs)

  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.endpoint_public_access_cidrs
  endpoint_private_access      = true # node -> API traffic stays in the VPC

  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = true

  upgrade_policy = {
    support_type = "STANDARD"
  }

  enable_irsa = true # cluster OIDC provider (IRSA for ESO, pgBackRest, EBS CSI)

  create_kms_key              = false
  encryption_config           = null
  enabled_log_types           = []
  create_cloudwatch_log_group = false

  addons = {
    vpc-cni = {
      before_compute = true
    }
    coredns    = {}
    kube-proxy = {}
    # CSI-created volumes carry the driver's default tag ebs.csi.aws.com/cluster=true;
    # `make down` uses it to prove none were orphaned.
    aws-ebs-csi-driver = {
      service_account_role_arn = aws_iam_role.ebs_csi.arn
    }
  }

  node_security_group_additional_rules = {
    ingress_self_all = {
      description = "Node to node, all ports/protocols"
      protocol    = "-1"
      from_port   = 0
      to_port     = 0
      type        = "ingress"
      self        = true
    }
  }

  eks_managed_node_groups = {
    # Base services (Postgres/Redis/MinIO/RabbitMQ/API/dashboard/LGTM). Untainted.
    system = {
      # Exact name (no random suffix): it is the eks.amazonaws.com/nodegroup
      # label value the overlay selects on, and what `make scale-*` targets.
      name            = "system"
      use_name_prefix = false

      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = [var.system_instance_type]
      capacity_type  = "ON_DEMAND"
      subnet_ids     = var.subnet_ids

      min_size     = var.system_node_count
      max_size     = var.system_node_count + 1
      desired_size = var.system_node_count

      block_device_mappings                 = local.root_disk.system
      attach_cluster_primary_security_group = false

      labels = { workload = "system" }
    }

    # Compute-bound CPU workers. Selected by eks.amazonaws.com/nodegroup=workercpu.
    workercpu = {
      # Exact name (no random suffix): it is the eks.amazonaws.com/nodegroup
      # label value the overlay selects on, and what `make scale-*` targets.
      name            = "workercpu"
      use_name_prefix = false

      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = [var.worker_instance_type]
      capacity_type  = "ON_DEMAND"
      subnet_ids     = [var.worker_subnet_id]

      min_size     = 0
      max_size     = var.worker_max_nodes
      desired_size = 0

      block_device_mappings                 = local.root_disk.workercpu
      attach_cluster_primary_security_group = false

      labels = { workload = "worker-cpu" }
      taints = {
        dedicated = {
          key    = "dedicated"
          value  = "worker"
          effect = "NO_SCHEDULE"
        }
      }
    }

    # T4 GPU workers. AL2023 NVIDIA AMI ships driver + container toolkit; the
    # device plugin comes from the configs/aws overlay. `accelerator: nvidia` is
    # the selector the Azure GPU patch already uses.
    gpu = {
      # Exact name (no random suffix): it is the eks.amazonaws.com/nodegroup
      # label value the overlay selects on, and what `make scale-*` targets.
      name            = "gpu"
      use_name_prefix = false

      ami_type       = "AL2023_x86_64_NVIDIA"
      instance_types = [var.gpu_instance_type]
      capacity_type  = "ON_DEMAND"
      subnet_ids     = [var.worker_subnet_id]

      min_size     = 0
      max_size     = var.gpu_max_nodes
      desired_size = 0

      block_device_mappings                 = local.root_disk.gpu
      attach_cluster_primary_security_group = false

      labels = {
        workload    = "gpu"
        accelerator = "nvidia"
      }
      taints = {
        gpu = {
          key    = "nvidia.com/gpu"
          value  = "present"
          effect = "NO_SCHEDULE"
        }
      }
    }
  }

  tags = var.tags
}

# IRSA role for the EBS CSI driver (lives here: the add-on above needs it)
data "aws_iam_policy_document" "ebs_csi_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:kube-system:ebs-csi-controller-sa"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.cluster_name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_trust.json

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}
