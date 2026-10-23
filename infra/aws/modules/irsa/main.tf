# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# IRSA Module — IAM roles for Kubernetes ServiceAccounts
# One IAM role per consumer, trusted by the cluster OIDC provider for a specific
# ServiceAccount (or a StringLike pattern). The AWS form of the Azure identity
# module (UAI + federated credential) and GKE Workload Identity.
#
# Raw resources (not the IAM community submodule, whose name changed across
# majors). Roles are rebuilt with every cluster: a new cluster has a new OIDC
# issuer, so old trust policies would be useless.
#
# Gotcha: aud must be sts.amazonaws.com; sub is
# system:serviceaccount:<namespace>:<serviceaccount>.

data "aws_iam_policy_document" "trust" {
  for_each = var.consumers

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      # StringLike when the subject has a wildcard (pgBackRest: nbody-db-*).
      test     = length(regexall("[*?]", each.value.service_account)) > 0 ? "StringLike" : "StringEquals"
      variable = "${var.oidc_provider}:sub"
      values   = ["system:serviceaccount:${each.value.namespace}:${each.value.service_account}"]
    }
  }
}

resource "aws_iam_role" "consumer" {
  for_each = var.consumers

  name               = "${var.name_prefix}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.trust[each.key].json

  tags = var.tags
}

resource "aws_iam_role_policy" "consumer" {
  for_each = { for k, v in var.consumers : k => v if v.policy_json != null }

  name   = "${var.name_prefix}-${each.key}"
  role   = aws_iam_role.consumer[each.key].id
  policy = each.value.policy_json
}
