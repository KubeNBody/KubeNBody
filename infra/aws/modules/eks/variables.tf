# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# EKS Module Variables

variable "cluster_name" {
  description = "EKS cluster name (also the kubeconfig context alias)"
  type        = string
}

variable "kubernetes_version" {
  description = "Pinned Kubernetes minor version (must be in EKS standard support)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID (from the base stack)"
  type        = string
}

variable "subnet_ids" {
  description = "Public subnets for the control plane and the system node group (>= 2 AZs)"
  type        = list(string)
}

variable "worker_subnet_id" {
  description = "Public subnet in worker_zone for the workercpu and gpu node groups"
  type        = string
}

variable "endpoint_public_access_cidrs" {
  description = "CIDRs allowed to reach the public API endpoint"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# Node groups
variable "system_instance_type" {
  description = "System node type (~ GKE e2-standard-4 / AKS D4s_v5)"
  type        = string
  default     = "m6i.xlarge"
}

variable "system_node_count" {
  description = "System node count (hosts all base services)"
  type        = number
  default     = 2
}

variable "system_disk_gib" {
  description = "System node root disk (GiB)"
  type        = number
  default     = 50
}

variable "worker_instance_type" {
  description = "CPU worker type (2 vCPU/4 GiB AMD, not burstable ~ GKE c2d-highcpu-2)"
  type        = string
  default     = "c7a.large"
}

variable "worker_max_nodes" {
  description = "Upper bound for `make scale-worker-cpu` (raised by the Makefile when needed)"
  type        = number
  default     = 16
}

variable "worker_disk_gib" {
  description = "CPU worker root disk (GiB)"
  type        = number
  default     = 50
}

variable "gpu_instance_type" {
  description = "GPU node type (1x NVIDIA T4)"
  type        = string
  default     = "g4dn.xlarge"
}

variable "gpu_max_nodes" {
  description = "Upper bound for `make scale-gpu`"
  type        = number
  default     = 2
}

variable "gpu_disk_gib" {
  description = "GPU node root disk (GiB) — the CUDA image is several GB"
  type        = number
  default     = 100
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
