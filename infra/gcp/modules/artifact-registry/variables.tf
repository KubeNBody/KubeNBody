# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "region" {
  description = "GCP region for the registry"
  type        = string
}

variable "repository_id" {
  description = "Repository ID (name)"
  type        = string
  default     = "nbody-service"
}

variable "description" {
  description = "Repository description"
  type        = string
  default     = "Container images for N-body simulator"
}

variable "keep_count" {
  description = "Number of recent image versions to keep"
  type        = number
  default     = 1000
}

variable "labels" {
  description = "Labels to apply to the repository"
  type        = map(string)
  default     = {}
}
