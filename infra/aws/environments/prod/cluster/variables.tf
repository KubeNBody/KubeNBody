# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AWS Production — CLUSTER stack variables

variable "account_id" {
  description = "AWS account ID to deploy into (the provider refuses any other)"
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Must be a 12-digit AWS account ID."
  }
}

variable "region" {
  description = "AWS region (must match the base stack)"
  type        = string
  default     = "eu-central-1"
}

variable "state_bucket" {
  description = "TF state bucket (to read the base stack's outputs)"
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name (also the kubeconfig context alias)"
  type        = string
  default     = "nbody-eks"
}

variable "kubernetes_version" {
  # 1.35: standard support until 2027-03-27. One minor behind the newest (1.36)
  # on purpose — the vendored ESO 0.9.9 / PGO 5.7.4 predate both.
  description = "Pinned Kubernetes minor version (EKS standard support only)"
  type        = string
  default     = "1.35"
}

variable "worker_zone" {
  description = "AZ for the workercpu + gpu node groups (preflight: offers c7a.large AND g4dn.xlarge)"
  type        = string
}

variable "endpoint_public_access_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint (e.g. your /32)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# Node groups
variable "system_instance_type" {
  description = "System node type"
  type        = string
  default     = "m6i.xlarge"
}

variable "system_node_count" {
  description = "System node count"
  type        = number
  default     = 2
}

variable "worker_instance_type" {
  description = "CPU worker node type"
  type        = string
  default     = "c7a.large"
}

variable "worker_max_nodes" {
  description = "Max CPU worker nodes (`make scale-worker-cpu N=` refuses N above it)"
  type        = number
  default     = 16
}

variable "gpu_instance_type" {
  description = "GPU node type"
  type        = string
  default     = "g4dn.xlarge"
}

variable "gpu_max_nodes" {
  description = "Max GPU nodes (`make scale-gpu N=` refuses N above it)"
  type        = number
  default     = 2
}
