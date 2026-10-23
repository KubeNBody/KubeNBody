# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP region for the cluster"
  type        = string
}

variable "cluster_name" {
  description = "Name of the GKE cluster"
  type        = string
  default     = "nbody-cluster"
}

variable "vpc_id" {
  description = "VPC network ID"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID"
  type        = string
}

variable "pods_range_name" {
  description = "Name of the secondary range for pods"
  type        = string
}

variable "services_range_name" {
  description = "Name of the secondary range for services"
  type        = string
}

variable "node_service_account" {
  description = "Service account email for GKE nodes"
  type        = string
}

# CPU Node Pool Variables
variable "cpu_machine_type" {
  description = "Machine type for CPU nodes"
  type        = string
  default     = "e2-standard-4"
}

variable "cpu_node_count" {
  description = "Number of CPU nodes (per zone for regional clusters)"
  type        = number
  default     = 0
}

variable "cpu_disk_size_gb" {
  description = "Disk size for CPU nodes in GB"
  type        = number
  default     = 20
}

# CPU Worker Node Pool Variables
variable "worker_zone" {
  description = "Single zone for the worker pools (within var.region)"
  type        = string
  default     = "europe-west1-b"
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

# GPU Node Pool Variables
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
  default     = 20
}

# Common Variables
variable "labels" {
  description = "Labels to apply to resources"
  type        = map(string)
  default     = {}
}
