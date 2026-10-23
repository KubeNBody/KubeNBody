# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# KubeNBody — Google Cloud (GKE) infrastructure Makefile

.PHONY: help bootstrap init apply destroy connect configure render deploy-k8s scale-cpu scale-worker-cpu scale-gpu init-secrets

# Default target
help:
	@echo "KubeNBody — Google Cloud infrastructure"
	@echo ""
	@echo "Usage:"
	@echo "  make bootstrap          - Initialize bootstrap (state bucket, APIs)"
	@echo "  make init               - Initialize Terraform for prod environment"
	@echo "  make apply              - Apply Terraform (GCP infrastructure)"
	@echo "  make connect            - Get kubectl credentials for cluster"
	@echo "  make configure          - Write configs/gcp/session from the terraform outputs"
	@echo "  make render             - Offline check of configs/gcp (example values)"
	@echo "  make deploy-k8s         - Deploy configs/gcp (staged, CRDs first)"
	@echo "  make destroy            - Destroy all infrastructure"
	@echo "  make init-secrets       - Initialize GCP Secret Manager values"
	@echo ""
	@echo "Node scaling:"
	@echo "  make scale-cpu N=1      - Scale core (cpu-pool) to N nodes per zone"
	@echo "  make scale-worker-cpu N=2 - Scale CPU worker Spot pool to N nodes"
	@echo "  make scale-gpu N=1      - Scale GPU node pool to N nodes"
	@echo ""
	@echo "Environment variables:"
	@echo "  PROJECT_ID              - GCP project ID (default: the gcloud project)"
	@echo "  REGION                  - GCP region (default: europe-west1)"
	@echo "  CLUSTER_NAME            - GKE cluster name (default: nbody-cluster)"

# Variables
PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
REGION ?= europe-west1
CLUSTER_NAME ?= nbody-cluster
ENV_DIR = environments/prod
BOOTSTRAP_DIR = bootstrap
KUSTOMIZE_DIR = ../../configs/gcp
# What the Makefile's targets also use, so Terraform gets the same values
TF_VARS = -var="project_id=$(PROJECT_ID)" -var="region=$(REGION)" -var="cluster_name=$(CLUSTER_NAME)"
# Every kubectl call is pinned to the GKE cluster's context (the name gcloud
# get-credentials writes), never the current context, which may be another
# cluster. As in infra/azure and infra/aws.
KUBE_CONTEXT ?= gke_$(PROJECT_ID)_$(REGION)_$(CLUSTER_NAME)
KUBECTL = kubectl --context $(KUBE_CONTEXT)

# Check PROJECT_ID is set
check-project:
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "Error: PROJECT_ID is not set"; \
		echo "Run: export PROJECT_ID=your-project-id"; \
		exit 1; \
	fi

# Bootstrap - run once to create state bucket
bootstrap: check-project
	@echo "Bootstrapping infrastructure..."
	cd $(BOOTSTRAP_DIR) && \
		terraform init && \
		terraform apply -var="project_id=$(PROJECT_ID)" -var="region=$(REGION)"

# Initialize Terraform
init: check-project
	@echo "Initializing Terraform..."
	@STATE_BUCKET="$(PROJECT_ID)-nbody-tfstate"; \
	cd $(ENV_DIR) && \
		terraform init -backend-config="bucket=$$STATE_BUCKET"

# Apply Terraform (GCP infrastructure only)
apply: check-project
	@echo "=============================================="
	@echo "  Applying GCP Infrastructure"
	@echo "=============================================="
	cd $(ENV_DIR) && \
		terraform apply $(TF_VARS)

# Fill configs/gcp/session from the terraform outputs (run after `make apply`)
configure:
	./scripts/configure.sh

# Offline check: example session, every deploy stage rendered (and kubeconform)
render:
	../common/render.sh $(KUSTOMIZE_DIR) ./scripts/configure.sh

# Deploy configs/gcp, staged for the CRDs (infra/common/deploy-k8s.sh)
deploy-k8s: check-project connect
	../common/deploy-k8s.sh $(KUSTOMIZE_DIR) $(KUBE_CONTEXT)

# Destroy infrastructure
destroy: check-project
	@echo "WARNING: This will destroy all infrastructure!"
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ]
	@# The load balancers and disks the cluster created are not Terraform's:
	@# deleting the namespaces deletes them (skipped if the cluster is gone)
	-gcloud container clusters get-credentials $(CLUSTER_NAME) --region $(REGION) --project $(PROJECT_ID)
	-$(KUBECTL) delete namespace nbody monitoring --ignore-not-found --wait=true --timeout=20m --request-timeout=30s
	cd $(ENV_DIR) && \
		terraform destroy $(TF_VARS)

# Get kubectl credentials
connect: check-project
	@echo "Getting cluster credentials..."
	gcloud container clusters get-credentials $(CLUSTER_NAME) \
		--region $(REGION) \
		--project $(PROJECT_ID)

# Initialize secrets
init-secrets: check-project
	@echo "Initializing secrets..."
	PROJECT_ID=$(PROJECT_ID) ./scripts/init-secrets.sh

# Scale CPU nodes
scale-cpu: check-project
	@if [ -z "$(N)" ]; then \
		echo "Error: Specify node count with N=<number>"; \
		echo "Example: make scale-cpu N=2"; \
		exit 1; \
	fi
	@echo "Scaling CPU node pool to $(N) nodes..."
	gcloud container clusters resize $(CLUSTER_NAME) \
		--node-pool cpu-pool \
		--num-nodes $(N) \
		--region $(REGION) \
		--project $(PROJECT_ID) \
		--quiet

# Scale CPU worker nodes (Spot pool)
scale-worker-cpu: check-project
	@if [ -z "$(N)" ]; then \
		echo "Error: Specify node count with N=<number>"; \
		echo "Example: make scale-worker-cpu N=2"; \
		exit 1; \
	fi
	@echo "Scaling CPU worker node pool to $(N) nodes..."
	gcloud container clusters resize $(CLUSTER_NAME) \
		--node-pool worker-cpu \
		--num-nodes $(N) \
		--region $(REGION) \
		--project $(PROJECT_ID) \
		--quiet

# Scale GPU nodes
scale-gpu: check-project
	@if [ -z "$(N)" ]; then \
		echo "Error: Specify node count with N=<number>"; \
		echo "Example: make scale-gpu N=1"; \
		exit 1; \
	fi
	@echo "Scaling GPU node pool to $(N) nodes..."
	gcloud container clusters resize $(CLUSTER_NAME) \
		--node-pool gpu-pool \
		--num-nodes $(N) \
		--region $(REGION) \
		--project $(PROJECT_ID) \
		--quiet
