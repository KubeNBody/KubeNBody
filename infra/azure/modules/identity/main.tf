# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Identity Module — Azure Workload Identity for cluster consumers
# One user-assigned managed identity + one federated identity credential per
# consumer. The FIC trusts a specific K8s ServiceAccount (via the cluster OIDC
# issuer), replacing GKE's `iam.gke.io/gcp-service-account` model.
#
# Consumers (set in environments/prod): ESO and the app workload. pgBackRest is
# NOT here — it uses a shared key, not Workload Identity.
#
# Gotchas baked in: audience is the fixed list ["api://AzureADTokenExchange"]
# (mismatch => AADSTS700212). Subject = system:serviceaccount:<ns>:<sa>.
# Max 20 FICs per identity (we use 1 each).

resource "azurerm_user_assigned_identity" "consumer" {
  for_each = var.consumers

  name                = "${var.name_prefix}-${each.key}"
  resource_group_name = var.resource_group_name
  location            = var.location

  tags = var.tags
}

resource "azurerm_federated_identity_credential" "consumer" {
  for_each = var.consumers

  name      = "${var.name_prefix}-${each.key}-fic"
  parent_id = azurerm_user_assigned_identity.consumer[each.key].id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = var.oidc_issuer_url
  subject   = "system:serviceaccount:${each.value.namespace}:${each.value.service_account}"
}
