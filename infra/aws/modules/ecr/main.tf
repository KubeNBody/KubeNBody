# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# ECR Module
# Two kinds of repositories:
#  - app repos (nbody, nbody-gpu, nbody-dashboard): pushed by
#    scripts/build-and-push.sh --aws. Lifecycle keeps the last N images — ECR
#    storage is the main idle cost of the base stack (the CUDA image is GBs).
#  - mirror repos (mirror/<path>): third-party images copied once by
#    infra/aws/scripts/mirror-images.sh (Docker Hub limits, Bitnami
#    catalogue churn). No lifecycle — they hold pinned digests the overlay uses.
#
# Nodes pull with the node role's AmazonEC2ContainerRegistryReadOnly (EKS
# module default); no repo policies needed in the same account.

resource "aws_ecr_repository" "app" {
  for_each = toset(var.app_repositories)

  name                 = each.key
  image_tag_mutability = "MUTABLE"
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "app" {
  for_each = aws_ecr_repository.app

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 1 day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the last ${var.keep_images} images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = var.keep_images
        }
        action = { type = "expire" }
      },
    ]
  })
}

resource "aws_ecr_repository" "mirror" {
  for_each = toset(var.mirror_repositories)

  name                 = each.key
  image_tag_mutability = "MUTABLE"
  force_delete         = var.force_delete

  image_scanning_configuration {
    scan_on_push = false # third-party images; scanned upstream
  }

  tags = var.tags
}
