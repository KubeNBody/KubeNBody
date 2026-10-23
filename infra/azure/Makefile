# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# KubeNBody — Azure (AKS) infrastructure Makefile
# Same target names as the GCP and AWS Makefiles where they apply.
# Run from infra/azure/ (or `make -C infra/azure <target>`).

SHELL := /bin/bash

.PHONY: help check-state-sa preflight bootstrap init apply destroy connect configure render deploy-k8s deploy-all verify scale-worker-cpu scale-gpu init-secrets

help:
	@echo "KubeNBody — Azure infrastructure"
	@echo ""
	@echo "Usage (typical order):"
	@echo "  make preflight          - Check GPU/CPU/Spot quota + disk SKUs in the region"
	@echo "  make bootstrap          - Create TF state backend (RG + storage account + container)"
	@echo "  make init               - Initialize Terraform (remote azurerm backend)"
	@echo "  make apply              - Apply Azure infrastructure (RG, network, ACR, AKS, identity, KV, backups)"
	@echo "  make configure          - Write configs/azure/session from the terraform outputs (ACR, ESO identity, Key Vault)"
	@echo "  make render             - Offline check of configs/azure (example values)"
	@echo "  make init-secrets       - Populate Key Vault secret values (app creds + pgBackRest key)"
	@echo "  make deploy-k8s         - Deploy Kubernetes resources via configs/azure (staged)"
	@echo "  make deploy-all         - configure + init-secrets + deploy-k8s (after apply & image push)"
	@echo "  make verify             - Post-deploy health gates (nodes, ExternalSecrets, pods, LB)"
	@echo "  make connect            - Get the kubeconfig (context nbody-aks) for the cluster"
	@echo "  make destroy            - Destroy the Azure environment"
	@echo ""
	@echo "Node scaling (manual):"
	@echo "  make scale-worker-cpu N=2 - Scale the CPU worker pool"
	@echo "  make scale-gpu N=1        - Scale GPU pool"
	@echo ""
	@echo "Env: SUBSCRIPTION_ID (default: az account show), STATE_SA (bootstrap, init), LOCATION, RESOURCE_GROUP, CLUSTER_NAME"

# --- Config (override via env) ---
SUBSCRIPTION_ID ?= $(shell az account show --query id -o tsv 2>/dev/null)
LOCATION        ?= polandcentral
RESOURCE_GROUP  ?= nbody-prod-rg
CLUSTER_NAME    ?= nbody-aks

# Every kubectl call targets the AKS context explicitly. `az aks get-credentials`
# names the context after the cluster, so with two clusters (GKE + AKS) in the
# kubeconfig a stray current-context can never misroute an Azure deploy. If the
# context is missing (connect not run), kubectl fails clearly instead of hitting GKE.
KUBE_CONTEXT    ?= $(CLUSTER_NAME)
KUBECTL          = kubectl --context $(KUBE_CONTEXT)

# TF state backend (must match bootstrap outputs)
STATE_RG        ?= nbody-tfstate-rg
STATE_SA        ?=              # globally unique storage account name: required
STATE_CONTAINER ?= tfstate
STATE_KEY       ?= prod.tfstate

ENV_DIR       = environments/prod
BOOTSTRAP_DIR = bootstrap
KUSTOMIZE_DIR = ../../configs/azure
# What the Makefile's targets also use, so Terraform gets the same values
TF_VARS = -var="subscription_id=$(SUBSCRIPTION_ID)" -var="location=$(LOCATION)" \
          -var="resource_group_name=$(RESOURCE_GROUP)" -var="cluster_name=$(CLUSTER_NAME)"

check-state-sa:
	@if [ -z "$(STATE_SA)" ]; then \
		echo "Error: STATE_SA not set: the globally unique name of the Terraform state storage account,"; \
		echo "       e.g. make bootstrap STATE_SA=<name> (and the same on make init)"; \
		exit 1; \
	fi

check-sub:
	@if [ -z "$(SUBSCRIPTION_ID)" ]; then \
		echo "Error: SUBSCRIPTION_ID not set and 'az account show' returned nothing."; \
		echo "Run: az login  (or export SUBSCRIPTION_ID=...)"; \
		exit 1; \
	fi

# Preflight — verify region capacity before a long apply
preflight: check-sub
	@echo "== Preflight ($(LOCATION)) =="
	@echo "-- GPU NCasT4_v3 quota (need Limit > 0) --"
	@az vm list-usage --location $(LOCATION) -o table | grep -iE "NCAS|Header|Name" || echo "  (no NCAS row — request T4 quota)"
	@echo "-- D family (system DSv5 + worker DALSv7) + Spot vCPU quota --"
	@az vm list-usage --location $(LOCATION) -o table | grep -iE "DSv5|DLSv5|Spot" || true
	@echo "-- Premium_LRS disks available --"
	@az vm list-skus --location $(LOCATION) --resource-type disks --query "[?name=='Premium_LRS'] | [0].name" -o tsv || echo "  (Premium_LRS not found)"
	@echo "-- Availability zones (system D4s_v5 / worker D2ls_v5) --"
	@az vm list-skus --location $(LOCATION) --resource-type virtualMachines --query "[?name=='Standard_D4s_v5'].locationInfo[0].zones | [0]" -o tsv || true
	@az vm list-skus --location $(LOCATION) --resource-type virtualMachines --query "[?name=='Standard_D2ls_v5'].locationInfo[0].zones | [0]" -o tsv || echo "  (D2ls_v5 not found in $(LOCATION) — check region/zone availability)"

# Bootstrap — run once to create the remote state backend
bootstrap: check-sub check-state-sa
	@echo "Bootstrapping Azure state backend..."
	cd $(BOOTSTRAP_DIR) && \
		terraform init && \
		terraform apply \
			-var="subscription_id=$(SUBSCRIPTION_ID)" \
			-var="location=$(LOCATION)" \
			-var="state_resource_group_name=$(STATE_RG)" \
			-var="state_storage_account_name=$(STATE_SA)"

# Initialize Terraform with the remote azurerm backend.
# -reconfigure makes this idempotent: it (re)points at the remote backend even if
# a stale/mismatched local .terraform exists. State already lives in the remote
# container, so no migration happens — it just reconnects.
init: check-sub check-state-sa
	@echo "Initializing Terraform (azurerm backend)..."
	cd $(ENV_DIR) && \
		terraform init -reconfigure \
			-backend-config="resource_group_name=$(STATE_RG)" \
			-backend-config="storage_account_name=$(STATE_SA)" \
			-backend-config="container_name=$(STATE_CONTAINER)" \
			-backend-config="key=$(STATE_KEY)"

apply: check-sub
	@echo "=============================================="
	@echo "  Applying Azure Infrastructure"
	@echo "=============================================="
	cd $(ENV_DIR) && \
		terraform apply $(TF_VARS)

destroy: check-sub
	@echo "WARNING: This destroys the Azure environment."
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ]
	cd $(ENV_DIR) && \
		terraform destroy $(TF_VARS)

connect: check-sub
	@echo "Getting AKS admin credentials (kubeconfig context: $(KUBE_CONTEXT))..."
	az aks get-credentials \
		--resource-group $(RESOURCE_GROUP) \
		--name $(CLUSTER_NAME) \
		--overwrite-existing

# Write configs/azure/session from the terraform outputs (run after `make apply`)
configure:
	./scripts/configure.sh

# Offline check: example session, every deploy stage rendered (and kubeconform)
render:
	../common/render.sh $(KUSTOMIZE_DIR) ./scripts/configure.sh

# Deploy configs/azure, staged for the CRDs (infra/common/deploy-k8s.sh, the
# standalone `kustomize`: the overlay has cross-tree bases into configs/base)
deploy-k8s: connect
	../common/deploy-k8s.sh $(KUSTOMIZE_DIR) $(KUBE_CONTEXT)

# Post-apply orchestration: session values, secrets, deploy.
# PREREQS: `make apply` done AND images pushed (scripts/build-and-push.sh --azure).
deploy-all: configure init-secrets deploy-k8s
	@echo ""
	@echo "Wiring + deploy complete. Run 'make verify' to check the gates."

# Post-deploy health gates. The ExternalSecrets check is the key signal that the
# Workload Identity -> Key Vault -> ESO chain works.
verify: connect
	@echo "== targeting context: $(KUBE_CONTEXT) =="
	@echo "== nodes ==" && $(KUBECTL) get nodes
	@echo "== ExternalSecrets (want STATUS=SecretSynced for all) ==" && $(KUBECTL) get externalsecrets -A
	@echo "== PostgresCluster ==" && $(KUBECTL) get postgrescluster -n nbody 2>/dev/null || true
	@echo "== nbody pods ==" && $(KUBECTL) get pods -n nbody
	@echo "== GPU allocatable (after scale-gpu) ==" && $(KUBECTL) get nodes -l accelerator=nvidia -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' 2>/dev/null || true
	@echo "== Grafana LB (want an INTERNAL/private IP, not public) ==" && $(KUBECTL) get svc lgtm -n monitoring -o wide 2>/dev/null || true

init-secrets:
	@echo "Populating Key Vault secret values..."
	./scripts/init-secrets.sh

scale-worker-cpu:
	@if [ -z "$(N)" ]; then echo "Specify N=<count>, e.g. make scale-worker-cpu N=2"; exit 1; fi
	az aks nodepool scale \
		--resource-group $(RESOURCE_GROUP) \
		--cluster-name $(CLUSTER_NAME) \
		--name workercpu \
		--node-count $(N)

scale-gpu:
	@if [ -z "$(N)" ]; then echo "Specify N=<count>, e.g. make scale-gpu N=1"; exit 1; fi
	az aks nodepool scale \
		--resource-group $(RESOURCE_GROUP) \
		--cluster-name $(CLUSTER_NAME) \
		--name gpu \
		--node-count $(N)
