# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# ECR Module Variables

variable "app_repositories" {
  description = "Repos for the images built by scripts/build-and-push.sh"
  type        = list(string)
}

variable "mirror_repositories" {
  description = "Repos for mirrored third-party images (from infra/aws/mirror-images.json)"
  type        = list(string)
  default     = []
}

variable "keep_images" {
  description = "Images kept per app repo (older ones expire)"
  type        = number
  default     = 5
}

variable "force_delete" {
  description = "Allow destroying repos that still hold images"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
