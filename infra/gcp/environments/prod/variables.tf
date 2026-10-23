# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Production Environment Variables

variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "europe-west1"
}

variable "cluster_name" {
  description = "Name of the GKE cluster"
  type        = string
  default     = "nbody-cluster"
}

variable "app_namespace" {
  description = "Kubernetes namespace for the application"
  type        = string
  default     = "nbody"
}

variable "secret_prefix" {
  description = "Prefix for GCP Secret Manager secrets"
  type        = string
  default     = "nbody"
}

# Network Configuration
variable "subnet_cidr" {
  description = "CIDR range for the subnet"
  type        = string
  default     = "10.0.0.0/24"
}

variable "pods_cidr" {
  description = "CIDR range for pods"
  type        = string
  default     = "10.1.0.0/16"
}

variable "services_cidr" {
  description = "CIDR range for services"
  type        = string
  default     = "10.2.0.0/20"
}

# CPU Node Pool Configuration
variable "cpu_machine_type" {
  description = "Machine type for CPU nodes"
  type        = string
  default     = "e2-standard-4"
}

variable "cpu_node_count" {
  description = "Number of core nodes per zone (regional cpu-pool hosts base services; keep >=1)"
  type        = number
  default     = 1
}

variable "cpu_disk_size_gb" {
  description = "Disk size for CPU nodes in GB"
  type        = number
  default     = 20
}

# CPU Worker Node Pool Configuration (Spot, manually scaled)
variable "worker_zone" {
  description = "Single zone for the worker pools (within var.region; null = <region>-b)"
  type        = string
  default     = null
}

variable "worker_cpu_machine_type" {
  description = "Machine type for CPU worker nodes"
  type        = string
  default     = "c2d-highcpu-2"
}

variable "worker_cpu_node_count" {
  description = "Number of CPU worker nodes (manually scaled; 0 when idle)"
  type        = number
  default     = 0
}

# GPU Node Pool Configuration
variable "gpu_machine_type" {
  description = "Machine type for GPU nodes (must be N1 series for T4)"
  type        = string
  default     = "n1-standard-4"
}

variable "gpu_spot" {
  description = "Run GPU nodes as Spot (cheaper, preemptible; jobs resume from snapshot). Set false for guaranteed scheduling."
  type        = bool
  default     = false
}

variable "gpu_node_count" {
  description = "Number of GPU nodes (per zone for regional clusters)"
  type        = number
  default     = 0
}

variable "gpu_disk_size_gb" {
  description = "Disk size for GPU nodes in GB"
  type        = number
  default     = 50
}
