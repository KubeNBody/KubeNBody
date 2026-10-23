# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Networking Module Variables

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "region" {
  description = "AWS region (for the S3 endpoint service name)"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR. With the VPC CNI, pods take IPs from the subnets too"
  type        = string
  default     = "10.20.0.0/16"
}

variable "azs" {
  description = "Availability zones, one public subnet each (>= 2 for EKS)"
  type        = list(string)

  validation {
    condition     = length(var.azs) >= 2
    error_message = "EKS needs subnets in at least 2 AZs."
  }
}

variable "subnet_newbits" {
  description = "Bits added to vpc_cidr per subnet (4 on a /16 => /20, ~4k IPs per subnet)"
  type        = number
  default     = 4
}

variable "tags" {
  description = "Resource tags"
  type        = map(string)
  default     = {}
}
