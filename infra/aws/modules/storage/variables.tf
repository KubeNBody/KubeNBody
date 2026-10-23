# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Storage Module Variables

variable "bucket_name" {
  description = "Backup bucket name (3-63 chars, lowercase, globally unique)"
  type        = string

  validation {
    condition = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.bucket_name))
    # No dots: dotted bucket names break virtual-hosted-style TLS (pgBackRest, S3 backend).
    error_message = "Must be 3-63 chars: lowercase letters, digits and dashes (no dots)."
  }
}

variable "pgbackrest_backstop_days" {
  # 365, not 90: each `make up` writes a new repo generation and the latest one is
  # only touched again at the next session — a 90-day gap would expire the very
  # backup the next `make up` restores from.
  description = "Backstop expiry for pgbackrest/ objects; must stay far above pgBackRest's own retention and the longest gap between sessions"
  type        = number
  default     = 365

  validation {
    condition     = var.pgbackrest_backstop_days >= 60
    error_message = "Keep the backstop >= 60 days so it can never run ahead of pgBackRest retention."
  }
}

variable "noncurrent_days" {
  description = "Days to keep noncurrent (overwritten/deleted) object versions"
  type        = number
  default     = 7
}

variable "force_destroy" {
  description = "Allow destroying the bucket while it still holds backups"
  type        = bool
  default     = false
}
