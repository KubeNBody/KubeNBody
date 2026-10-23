# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "k8s_namespace" {
  description = "Kubernetes namespace for workload identity"
  type        = string
  default     = "nbody"
}

variable "k8s_service_account" {
  description = "Kubernetes service account name for workload identity"
  type        = string
  default     = "nbody-workload"
}

variable "gke_cluster_id" {
  description = "GKE cluster ID (used to ensure WI bindings wait for cluster)"
  type        = string
  default     = ""
}
