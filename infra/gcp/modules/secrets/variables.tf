# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
variable "project_id" {
  description = "GCP Project ID"
  type        = string
}

variable "secret_prefix" {
  description = "Prefix for secret names (e.g., 'nbody-prod')"
  type        = string
  default     = "nbody"
}

variable "eso_service_account_email" {
  description = "Email of the ESO service account that needs access to secrets"
  type        = string
}

variable "labels" {
  description = "Labels to apply to resources"
  type        = map(string)
  default     = {}
}
