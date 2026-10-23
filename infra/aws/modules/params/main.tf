# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Params Module — SSM Parameter Store entries for app secrets
# One SecureString per secret, named exactly like the remoteRef keys in
# configs/base/secrets/external-secrets.yml, so the AWS overlay only swaps the
# store (to `aws-ps`), never the keys.
#
# Terraform creates each parameter with a PLACEHOLDER and then ignores the
# value; infra/aws/scripts/init-secrets.sh sets real values out-of-band (same
# flow as GCP/Azure — secret values never land in TF state).
#
# Standard tier (free). Default `aws/ssm` KMS key: the IRSA role of ESO needs no
# KMS statement for it (the AWS-managed key's policy allows use via SSM).

resource "aws_ssm_parameter" "secret" {
  for_each = toset(var.names)

  name        = each.key
  description = "nbody secret — value set by infra/aws/scripts/init-secrets.sh"
  type        = "SecureString"
  tier        = "Standard"
  value       = var.placeholder

  tags = var.tags

  lifecycle {
    ignore_changes = [value]
  }
}
