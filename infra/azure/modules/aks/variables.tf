# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# AKS Module Variables

variable "cluster_name" {
  description = "AKS cluster name"
  type        = string
}

variable "location" {
  description = "Azure region"
  type        = string
}

variable "resource_group_name" {
  description = "Resource group name"
  type        = string
}

variable "kubernetes_version" {
  description = "Kubernetes version (null = AKS default)"
  type        = string
  default     = null
}

variable "node_subnet_id" {
  description = "Subnet ID for node IPs"
  type        = string
}

# Zones
variable "cluster_zones" {
  description = "Zones for the regional system pool"
  type        = list(string)
  default     = ["1", "2", "3"]
}

variable "worker_zone" {
  description = "Single zone for the worker-cpu and GPU pools"
  type        = string
  default     = "1"
}

# Overlay / service networking
variable "pod_cidr" {
  description = "Overlay CIDR for pod IPs (independent of the VNet)"
  type        = string
  default     = "192.168.0.0/16"
}

variable "service_cidr" {
  description = "CIDR for Kubernetes services"
  type        = string
  default     = "172.16.0.0/16"
}

variable "dns_service_ip" {
  description = "Cluster DNS service IP (must be inside service_cidr)"
  type        = string
  default     = "172.16.0.10"
}

# System pool (base services live here, not tainted)
variable "system_vm_size" {
  description = "VM size for the system pool (~ GKE e2-standard-4)"
  type        = string
  default     = "Standard_D4s_v5"
}

variable "system_node_count" {
  description = "System pool node count (total across zones; hosts base services)"
  type        = number
  default     = 2
}

# CPU worker pool (on-demand)
variable "worker_cpu_vm_size" {
  description = "VM size for CPU workers (2 vCPU/4 GiB ~ GKE c2d-highcpu-2; not burstable)"
  type        = string
  default     = "Standard_D2ls_v5"
}

variable "worker_cpu_node_count" {
  description = "CPU worker node count (manually scaled; 0 when idle)"
  type        = number
  default     = 0
}

# GPU pool (on-demand)
variable "gpu_vm_size" {
  description = "VM size for GPU nodes (1x NVIDIA T4)"
  type        = string
  default     = "Standard_NC4as_T4_v3"
}

variable "gpu_node_count" {
  description = "GPU node count (manually scaled; 0 when idle)"
  type        = number
  default     = 0
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
