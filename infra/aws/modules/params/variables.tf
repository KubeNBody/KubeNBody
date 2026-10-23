# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Params Module Variables

variable "names" {
  description = "Parameter names (= ExternalSecret remoteRef keys)"
  type        = list(string)
}

variable "placeholder" {
  description = "Initial value; init-secrets.sh only overwrites parameters still holding it"
  type        = string
  default     = "CHANGEME-set-by-init-secrets"
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
