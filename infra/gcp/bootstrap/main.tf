# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Bootstrap Infrastructure
# Run this ONCE before the main infrastructure to:
# 1. Create GCS bucket for Terraform state
# 2. Enable required GCP APIs

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Enable Required APIs
resource "google_project_service" "apis" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "servicenetworking.googleapis.com",
  ])

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# GCS Bucket for Terraform State
resource "google_storage_bucket" "terraform_state" {
  name     = "${var.project_id}-nbody-tfstate"
  project  = var.project_id
  location = var.region

  # Prevent accidental deletion
  force_destroy = false

  # Enable versioning for state recovery
  versioning {
    enabled = true
  }

  # Uniform bucket-level access (recommended)
  uniform_bucket_level_access = true

  # Lifecycle rule to clean up old versions
  lifecycle_rule {
    condition {
      num_newer_versions = 5
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    purpose    = "terraform-state"
    project    = "nbody"
    managed_by = "terraform"
  }
}
