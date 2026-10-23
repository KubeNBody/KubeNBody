# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Storage Module — backup bucket (pgBackRest repo + MinIO mirror)
# One bucket, two prefixes:
#  - pgbackrest/  pgBackRest repo1 (written via IRSA web-id from the cluster)
#  - minio/       MinIO objects mirrored by `make down`, restored by `make up`
#                 (the cluster and its EBS volumes are ephemeral)
#
# Retention is pgBackRest's job (repo1-retention-full). The pgbackrest/ expiry
# rule below is only a FAR backstop (default 365 days): an S3 lifecycle delete that runs
# ahead of pgBackRest's own expiry would corrupt the repo.

resource "aws_s3_bucket" "backups" {
  bucket        = var.bucket_name
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket = aws_s3_bucket.backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  # Versioning keeps every overwritten/deleted object; pgBackRest expiry and
  # `mc mirror --overwrite` would otherwise pile up noncurrent versions forever.
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Backstop only — keep FAR above pgBackRest's repo1-retention-full.
  rule {
    id     = "pgbackrest-backstop"
    status = "Enabled"

    filter {
      prefix = "pgbackrest/"
    }

    expiration {
      days = var.pgbackrest_backstop_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.backups.arn,
        "${aws_s3_bucket.backups.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.backups]
}
