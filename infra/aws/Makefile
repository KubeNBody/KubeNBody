# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# KubeNBody — AWS (EKS) infrastructure Makefile
# Self-contained AWS stack. Same target names as the Azure Makefile
# (../azure/Makefile) where they apply. Run from infra/aws/ (or `make -C aws`).
#
# Two Terraform stacks:
#   environments/prod/base     PERMANENT: VPC, ECR, backup bucket, parameters
#   environments/prod/cluster  EPHEMERAL: EKS + IRSA — `make up` / `make down`
#
# SAFETY: the shell's AWS_PROFILE may belong to another account. All targets
# run as NBODY_AWS_PROFILE (default `nbody`, an inherited AWS_PROFILE is
# overridden) and depend on check-account, which refuses to continue unless the
# caller is in ACCOUNT_ID. The Terraform providers also pin allowed_account_ids.
# Every kubectl call is pinned to --context $(KUBE_CONTEXT): the current kube
# context may be another cluster.

SHELL := /bin/bash

.PHONY: help check-account check-terraform check-state-bucket check-session check-images \
        preflight bootstrap init init-base init-cluster plan-base base plan-cluster cluster-apply apply \
        init-secrets mirror-images ecr-login outputs \
        connect configure render deploy-k8s verify endpoints \
        up down pg-wait pg-backup minio-backup minio-restore promote-backup \
        scale-worker-cpu scale-gpu fmt validate

# --- Config (override via env) ---
# The profile comes from NBODY_AWS_PROFILE, NEVER from an inherited AWS_PROFILE:
# `AWS_PROFILE ?= ...` would silently keep the shell's profile, which may be another account's. `:=` +
# export forces it for every recipe and script.
NBODY_AWS_PROFILE ?= nbody
export AWS_PROFILE := $(NBODY_AWS_PROFILE)
ACCOUNT_ID   ?=
REGION       ?= eu-central-1
STATE_BUCKET ?=
CLUSTER_NAME ?= nbody-eks

KUBE_CONTEXT ?= $(CLUSTER_NAME)
KUBECTL       = kubectl --context $(KUBE_CONTEXT)

BOOTSTRAP_DIR = bootstrap
BASE_DIR      = environments/prod/base
CLUSTER_DIR   = environments/prod/cluster
KUSTOMIZE_DIR = ../../configs/aws
SESSION_ENV   = $(KUSTOMIZE_DIR)/session/session.env
PG_POINTERS   = pgbackrest/nbody-db
TF_MIN        = 1.11.0

TF_VARS         = -var="account_id=$(ACCOUNT_ID)" -var="region=$(REGION)"
TF_CLUSTER_VARS = $(TF_VARS) -var="state_bucket=$(STATE_BUCKET)" -var="cluster_name=$(CLUSTER_NAME)"

# Load a session.env value: $(call session,KEY)
session = $$(sed -n 's/^$(1)=//p' $(SESSION_ENV))

help:
	@echo "KubeNBody — AWS infrastructure"
	@echo ""
	@echo "One-time (permanent base stack):"
	@echo "  make preflight        - G/standard vCPU quotas + instance-type offerings per AZ"
	@echo "  make bootstrap        - Create the TF state bucket"
	@echo "  make init             - Initialize Terraform (base + cluster, S3 backend)"
	@echo "  make plan-base / base - Plan / apply the base stack (VPC, ECR, backup bucket, parameters)"
	@echo "  make init-secrets     - Set real values for the SSM secret parameters"
	@echo "  make mirror-images    - Copy third-party images into ECR mirrors"
	@echo "  make ecr-login        - docker login to ECR (for build-and-push.sh --aws)"
	@echo ""
	@echo "Per session (ephemeral cluster):"
	@echo "  make up               - cluster-apply + connect + configure + deploy + restores + Postgres wait + verify (~20 min)"
	@echo "  make endpoints        - Dashboard / Grafana URLs and NBODY_API_URL"
	@echo "  make scale-worker-cpu N=4 / make scale-gpu N=1"
	@echo "  make down             - final backups + delete LBs/volumes + destroy the cluster stack"
	@echo "                          (SKIP_BACKUP=1 to skip the Postgres/MinIO backups)"
	@echo ""
	@echo "Pieces of up/down: plan-cluster cluster-apply connect configure deploy-k8s verify"
	@echo "                   pg-wait pg-backup minio-backup minio-restore promote-backup"
	@echo "Offline checks:     make fmt / validate / render"
	@echo ""
	@echo "Required env: ACCOUNT_ID (the account to deploy into), STATE_BUCKET"
	@echo "Defaults:     NBODY_AWS_PROFILE=nbody REGION=eu-central-1 CLUSTER_NAME=nbody-eks"
	@echo "              (an inherited AWS_PROFILE is ignored)"

# Guards
check-account:
	@if [ -z "$(ACCOUNT_ID)" ]; then \
		echo "Error: ACCOUNT_ID not set (the 12-digit ID of the account to deploy into)."; exit 1; \
	fi
	@ACTUAL=$$(aws sts get-caller-identity --query Account --output text 2>/dev/null); \
	 if [ -z "$$ACTUAL" ]; then \
		echo "Error: no credentials for AWS_PROFILE=$(AWS_PROFILE) (aws sso login --profile $(AWS_PROFILE)?)"; exit 1; \
	 fi; \
	 if [ "$$ACTUAL" != "$(ACCOUNT_ID)" ]; then \
		echo "Error: AWS_PROFILE=$(AWS_PROFILE) is account $$ACTUAL, expected $(ACCOUNT_ID). Refusing."; exit 1; \
	 fi; \
	 echo "AWS account OK: $$ACTUAL (profile $(AWS_PROFILE))"

check-terraform:
	@V=$$(terraform version -json | python3 -c 'import sys,json;print(json.load(sys.stdin)["terraform_version"])'); \
	 if [ "$$(printf '%s\n%s\n' "$(TF_MIN)" "$$V" | sort -V | head -1)" != "$(TF_MIN)" ]; then \
		echo "Error: terraform $$V < $(TF_MIN) (needed for S3-native state locking). Upgrade first."; exit 1; \
	 fi

check-state-bucket:
	@if [ -z "$(STATE_BUCKET)" ]; then echo "Error: STATE_BUCKET not set."; exit 1; fi

# The overlay must hold REAL session values before anything is deployed.
check-session:
	@if [ "$$(sed -n 's/^MODE=//p' $(SESSION_ENV) 2>/dev/null)" != "real" ]; then \
		echo "Error: configs/aws/session is missing or holds example values — run 'make configure'."; exit 1; \
	 fi

# Step 0: preflight
# G instances (T4) are 0 vCPU on fresh accounts — request quota early.
preflight: check-account
	@echo "== Preflight ($(REGION)) =="
	@echo "-- On-Demand G and VT instances vCPU quota (need >= 4 for 1x g4dn.xlarge) --"
	@aws service-quotas get-service-quota --region $(REGION) --service-code ec2 \
		--quota-code L-DB2E81BA --query 'Quota.Value' --output text \
		|| echo "  (could not read quota L-DB2E81BA)"
	@echo "-- On-Demand Standard (A,C,D,H,I,M,R,T,Z) vCPU quota (need >= 8 + 2 per worker) --"
	@aws service-quotas get-service-quota --region $(REGION) --service-code ec2 \
		--quota-code L-1216C47A --query 'Quota.Value' --output text \
		|| echo "  (could not read quota L-1216C47A)"
	@echo "-- AZs offering each instance type (worker_zone must offer c7a.large AND g4dn.xlarge) --"
	@for t in m6i.xlarge c7a.large g4dn.xlarge; do \
		printf "  %-12s " "$$t"; \
		aws ec2 describe-instance-type-offerings --region $(REGION) --location-type availability-zone \
			--filters Name=instance-type,Values=$$t --query 'InstanceTypeOfferings[].Location' \
			--output text | tr '\t' ' '; \
	 done

# Steps 1-2: bootstrap + base stack (permanent)
bootstrap: check-account check-terraform check-state-bucket
	@echo "Bootstrapping AWS state bucket $(STATE_BUCKET)..."
	cd $(BOOTSTRAP_DIR) && \
		terraform init && \
		terraform apply $(TF_VARS) -var="state_bucket_name=$(STATE_BUCKET)"

# -reconfigure makes this idempotent (re-points at the remote backend even if a
# stale local .terraform exists; state already lives in S3, nothing migrates).
init: init-base init-cluster

init-base: check-account check-terraform check-state-bucket
	cd $(BASE_DIR) && \
		terraform init -reconfigure \
			-backend-config="bucket=$(STATE_BUCKET)" \
			-backend-config="region=$(REGION)" \
			-backend-config="key=prod/base.tfstate"

init-cluster: check-account check-terraform check-state-bucket
	cd $(CLUSTER_DIR) && \
		terraform init -reconfigure \
			-backend-config="bucket=$(STATE_BUCKET)" \
			-backend-config="region=$(REGION)" \
			-backend-config="key=prod/cluster.tfstate"

plan-base: check-account check-terraform
	cd $(BASE_DIR) && terraform plan $(TF_VARS)

base: check-account check-terraform
	@echo "=============================================="
	@echo "  Applying AWS base stack (permanent)"
	@echo "=============================================="
	cd $(BASE_DIR) && terraform apply $(TF_VARS)

init-secrets: check-account
	@echo "Populating SSM Parameter Store secret values..."
	REGION=$(REGION) ./scripts/init-secrets.sh

mirror-images: check-account
	./scripts/mirror-images.sh

ecr-login: check-account
	@REG=$$(cd $(BASE_DIR) && terraform output -raw ecr_registry); \
	 aws ecr get-login-password --region $(REGION) | docker login --username AWS --password-stdin $$REG; \
	 echo "export ECR_REGISTRY=$$REG"

outputs:
	cd $(BASE_DIR) && terraform output
	-cd $(CLUSTER_DIR) && terraform output

# Step 3: cluster stack (ephemeral)
plan-cluster: check-account check-terraform check-state-bucket
	cd $(CLUSTER_DIR) && terraform plan $(TF_CLUSTER_VARS)

cluster-apply: check-account check-terraform check-state-bucket
	@echo "=============================================="
	@echo "  Applying AWS cluster stack (ephemeral, ~15 min)"
	@echo "=============================================="
	cd $(CLUSTER_DIR) && terraform apply $(TF_CLUSTER_VARS)

apply: base cluster-apply

# --profile is written into the kubeconfig exec entry, so kubectl authenticates
# against ACCOUNT_ID whatever the shell's AWS_PROFILE is.
# update-kubeconfig also SWITCHES current-context; this switches it back, so
# your plain `kubectl` keeps pointing where it did (every target here uses
# --context explicitly anyway).
connect: check-account
	@PREV=$$(kubectl config current-context 2>/dev/null || true); \
	 aws eks update-kubeconfig --region $(REGION) --name $(CLUSTER_NAME) \
		--alias $(KUBE_CONTEXT) --profile $(NBODY_AWS_PROFILE) >/dev/null && \
	 echo "kubeconfig context $(KUBE_CONTEXT) written"; \
	 if [ -n "$$PREV" ] && [ "$$PREV" != "$(KUBE_CONTEXT)" ]; then \
		kubectl config use-context "$$PREV" >/dev/null && echo "current-context left at $$PREV"; \
	 fi

# Step 4: overlay session values
configure: check-account
	./scripts/configure.sh

# Offline: example session values, full build + stage build + schema check.
# Refuses to clobber the REAL session of a live cluster.
render:
	@OUT=$$(mktemp -d); ../common/render.sh $(KUSTOMIZE_DIR) ./scripts/configure.sh $$OUT && \
	 LB=$$(grep -c 'type: LoadBalancer' $$OUT/all.yaml); NLB=$$(grep -c 'aws-load-balancer-type: nlb' $$OUT/all.yaml); \
	 if [ "$$LB" != "$$NLB" ]; then \
		echo "Error: $$LB LoadBalancer Services but $$NLB NLB annotations"; exit 1; \
	 fi; \
	 echo "LoadBalancer Services: $$LB, all NLB-annotated"

# Step 5: deploy (staged for CRD ordering, mirrors the GCP/Azure flow)
# Every image the overlay references must exist in ECR, or pods sit in
# ImagePullBackOff: app tags from configs/aws/apps + every mirror repo non-empty.
# App images from another registry (make up NBODY_IMAGE_REGISTRY=...) are not checked.
check-images: check-account
	@FAIL=0; \
	 APP_REG=$$(sed -n 's/^APP_REGISTRY=//p' $(SESSION_ENV)); ECR=$$(sed -n 's/^ECR_REGISTRY=//p' $(SESSION_ENV)); \
	 if [ -n "$$APP_REG" ] && [ "$$APP_REG" != "$$ECR" ]; then echo "  app images from $$APP_REG: not checked"; APPS=""; \
	 else APPS="kubenbody kubenbody-gpu kubenbody-dashboard"; fi; \
	 for n in $$APPS; do \
		T=$$(grep -A1 "name: $$n$$" $(KUSTOMIZE_DIR)/apps/kustomization.yaml | sed -n 's/.*newTag: *"\(.*\)"/\1/p'); \
		aws ecr describe-images --region $(REGION) --repository-name $$n --image-ids imageTag=$$T >/dev/null 2>&1 \
			|| { echo "  missing in ECR: $$n:$$T (scripts/build-and-push.sh --aws)"; FAIL=1; }; \
	 done; \
	 for r in $$(python3 -c 'import json;print(" ".join(m["repo"] for m in json.load(open("mirror-images.json"))))'); do \
		[ "$$(aws ecr list-images --region $(REGION) --repository-name $$r --query 'length(imageIds)' --output text 2>/dev/null)" -gt 0 ] 2>/dev/null \
			|| { echo "  empty mirror: $$r (make mirror-images)"; FAIL=1; }; \
	 done; \
	 [ "$$FAIL" = 0 ] && echo "ECR images OK" || exit 1

deploy-k8s: check-account check-images connect
	../common/deploy-k8s.sh $(KUSTOMIZE_DIR) $(KUBE_CONTEXT)

# Post-deploy gates. ExternalSecrets SecretSynced = IRSA -> Parameter Store works.
verify: check-account
	@echo "== targeting context: $(KUBE_CONTEXT) =="
	@echo "== nodes ==" && $(KUBECTL) get nodes -L eks.amazonaws.com/nodegroup,topology.kubernetes.io/zone
	@echo "== ExternalSecrets (want STATUS=SecretSynced for all) ==" && $(KUBECTL) get externalsecrets -A
	@echo "== PostgresCluster ==" && $(KUBECTL) get postgrescluster -n nbody 2>/dev/null || true
	@echo "== PVCs (want Bound) ==" && $(KUBECTL) get pvc -A
	@echo "== nbody pods ==" && $(KUBECTL) get pods -n nbody
	@echo "== GPU allocatable (after scale-gpu) ==" && $(KUBECTL) get nodes -l accelerator=nvidia -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' 2>/dev/null || true
	@echo "== Load balancers (want NLB hostnames *.elb.<region>.amazonaws.com — a Classic ELB is *.<region>.elb.amazonaws.com) =="
	@for s in nbody/dashboard-frontend-svc monitoring/lgtm; do \
		H=$$($(KUBECTL) get svc -n $${s%%/*} $${s##*/} -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null); \
		case "$$H" in \
			"") echo "  $$s: (no hostname yet)";; \
			*.elb.$(REGION).amazonaws.com) echo "  $$s: NLB $$H";; \
			*) echo "  $$s: NOT AN NLB: $$H"; exit 1;; \
		esac; \
	 done

endpoints: check-account
	@D=$$($(KUBECTL) get svc -n nbody dashboard-frontend-svc -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'); \
	 G=$$($(KUBECTL) get svc -n monitoring lgtm -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'); \
	 echo "Dashboard:  http://$$D"; \
	 echo "Grafana:    http://$$G:3000   (only from $$(sed -n 's/^ADMIN_CIDR=//p' $(SESSION_ENV)); re-run 'make configure deploy-k8s' if your IP changed)"; \
	 echo ""; \
	 echo "export NBODY_KUBE_CONTEXT=$(KUBE_CONTEXT)"; \
	 echo "export NBODY_API_URL=http://$$D"

# Session lifecycle
up: check-account cluster-apply connect configure deploy-k8s
	@echo "Waiting for MinIO before the restore..."
	$(KUBECTL) wait --for=condition=Available deployment/minio -n nbody --timeout=900s
	$(MAKE) --no-print-directory minio-restore
	$(MAKE) --no-print-directory pg-wait
	$(MAKE) --no-print-directory verify
	@echo ""
	$(MAKE) --no-print-directory endpoints
	@echo ""
	@echo "Restored from pgBackRest generation: $$(sed -n 's/^RESTORE_FROM=//p' $(SESSION_ENV)) (empty = fresh database)"

# Wait until Postgres serves: every instance set has readyReplicas == replicas
# and pgBouncer is ready. With a restore (spec.dataSource) the instances only
# start after the restore job finished, so this also waits for the restore.
# PGO's condition names are not in its chart, so this keys on the status
# counts the CRD defines. PG_WAIT_MIN (default 45) bounds a large restore.
PG_WAIT_MIN ?= 45
pg-wait: check-account
	@echo "Waiting for PostgresCluster nbody-db (restore from: $$(sed -n 's/^RESTORE_FROM=//p' $(SESSION_ENV) 2>/dev/null | grep . || echo none), up to $(PG_WAIT_MIN) min)..."
	@for i in $$(seq 1 $$(( $(PG_WAIT_MIN) * 6 ))); do \
		INST=$$($(KUBECTL) -n nbody get postgrescluster nbody-db -o jsonpath='{range .status.instances[*]}{.readyReplicas}/{.replicas} {end}' 2>/dev/null); \
		BOUNCER=$$($(KUBECTL) -n nbody get postgrescluster nbody-db -o jsonpath='{.status.proxy.pgBouncer.readyReplicas}/{.status.proxy.pgBouncer.replicas}' 2>/dev/null); \
		READY=yes; \
		[ -n "$$INST" ] || READY=no; \
		for s in $$INST $$BOUNCER; do \
			R=$${s%/*}; W=$${s#*/}; \
			{ [ -n "$$R" ] && [ -n "$$W" ] && [ "$$W" -gt 0 ] && [ "$$R" = "$$W" ]; } || READY=no; \
		done; \
		if [ "$$READY" = yes ]; then echo "Postgres ready (instances $$INST, pgBouncer $$BOUNCER)."; exit 0; fi; \
		[ $$((i % 6)) -eq 0 ] && echo "  ... instances '$$INST' pgBouncer '$$BOUNCER'"; \
		sleep 10; \
	 done; \
	 echo "Error: Postgres not ready after $(PG_WAIT_MIN) min. Restore / instance pods:"; \
	 $(KUBECTL) -n nbody get jobs,pods -l postgres-operator.crunchydata.com/cluster=nbody-db; \
	 exit 1

# Final full pgBackRest backup to this session's generation; fails on failure.
pg-backup: check-account check-session
	@TS=$$(date +%s); \
	 echo "Triggering manual full backup (id $$TS)..."; \
	 $(KUBECTL) -n nbody annotate postgrescluster nbody-db --overwrite \
		postgres-operator.crunchydata.com/pgbackrest-backup="$$TS" >/dev/null; \
	 for i in $$(seq 1 360); do \
		ID=$$($(KUBECTL) -n nbody get postgrescluster nbody-db -o jsonpath='{.status.pgbackrest.manualBackup.id}'); \
		FIN=$$($(KUBECTL) -n nbody get postgrescluster nbody-db -o jsonpath='{.status.pgbackrest.manualBackup.finished}'); \
		OK=$$($(KUBECTL) -n nbody get postgrescluster nbody-db -o jsonpath='{.status.pgbackrest.manualBackup.succeeded}'); \
		if [ "$$ID" = "$$TS" ] && [ "$$FIN" = "true" ]; then \
			if [ "$${OK:-0}" -ge 1 ]; then echo "Backup $$TS succeeded."; exit 0; fi; \
			echo "Error: backup $$TS finished WITHOUT success — not destroying anything."; exit 1; \
		fi; \
		sleep 10; \
	 done; \
	 echo "Error: backup $$TS did not finish within 60 min."; exit 1

minio-backup: check-account check-session
	KUBE_CONTEXT=$(KUBE_CONTEXT) ./scripts/minio-sync.sh backup

minio-restore: check-account check-session
	KUBE_CONTEXT=$(KUBE_CONTEXT) ./scripts/minio-sync.sh restore

# Move CURRENT to this session's generation (only after a successful pg-backup).
promote-backup: check-account check-session
	@B=$(call session,BACKUP_BUCKET); G=$(call session,PG_GEN); \
	 printf '%s\n' "$$G" | aws s3 cp - "s3://$$B/$(PG_POINTERS)/CURRENT" --region $(REGION) >/dev/null && \
	 echo "CURRENT pgBackRest generation -> $$G"

down: check-account check-terraform check-state-bucket check-session connect
	@echo "This DESTROYS the EKS cluster $(CLUSTER_NAME) (the base stack and backups are kept)."
	@read -p "Continue? [y/N] " c && [ "$$c" = "y" ]
	@if [ "$(SKIP_BACKUP)" = "1" ]; then \
		echo "SKIP_BACKUP=1: no final backups; CURRENT stays at the previous generation."; \
	 else \
		$(MAKE) --no-print-directory pg-backup && \
		$(MAKE) --no-print-directory minio-backup && \
		$(MAKE) --no-print-directory promote-backup || { echo "Backups failed — aborting before any deletion."; exit 1; }; \
	 fi
	@echo "== Deleting app namespaces (removes LB Services and PVCs) =="
	-$(KUBECTL) delete namespace nbody monitoring --wait=true --timeout=20m
	@B=$(call session,BACKUP_BUCKET); VPC=$(call session,VPC_ID); \
	 echo "== Waiting for load balancers in $$VPC to disappear =="; \
	 for i in $$(seq 1 60); do \
		N=$$(aws elbv2 describe-load-balancers --region $(REGION) --query "length(LoadBalancers[?VpcId=='$$VPC'])" --output text); \
		C=$$(aws elb describe-load-balancers --region $(REGION) --query "length(LoadBalancerDescriptions[?VPCId=='$$VPC'])" --output text); \
		[ "$$N" = "0" ] && [ "$$C" = "0" ] && break; sleep 10; \
	 done; \
	 [ "$$N" = "0" ] && [ "$$C" = "0" ] || { echo "Error: load balancers still present in $$VPC — not destroying."; exit 1; }; \
	 echo "== Waiting for PersistentVolumes (EBS) to be released =="; \
	 for i in $$(seq 1 60); do \
		[ -z "$$($(KUBECTL) get pv -o name 2>/dev/null)" ] && break; sleep 10; \
	 done; \
	 [ -z "$$($(KUBECTL) get pv -o name 2>/dev/null)" ] || { echo "Error: PersistentVolumes remain — not destroying."; $(KUBECTL) get pv; exit 1; }; \
	 echo "== Clearing the ACTIVE generation pointer =="; \
	 aws s3 rm "s3://$$B/$(PG_POINTERS)/ACTIVE" --region $(REGION) >/dev/null || true
	@echo "== Destroying the cluster stack =="
	cd $(CLUSTER_DIR) && terraform destroy -auto-approve $(TF_CLUSTER_VARS)
	@VPC=$(call session,VPC_ID); FAIL=0; \
	 V=$$(aws ec2 describe-volumes --region $(REGION) --filters Name=tag:ebs.csi.aws.com/cluster,Values=true Name=status,Values=available --query 'Volumes[].VolumeId' --output text); \
	 [ -z "$$V" ] || { echo "LEFTOVER EBS volumes (billing!): $$V"; FAIL=1; }; \
	 SG=$$(aws ec2 describe-security-groups --region $(REGION) --filters Name=vpc-id,Values=$$VPC Name=group-name,Values='k8s-*' --query 'SecurityGroups[].GroupId' --output text); \
	 [ -z "$$SG" ] || { echo "LEFTOVER k8s security groups: $$SG"; FAIL=1; }; \
	 rm -f $(SESSION_ENV); \
	 if [ "$$FAIL" = 1 ]; then echo "Cluster destroyed, but clean up the leftovers above."; exit 1; fi; \
	 echo "Cluster destroyed; no leftover volumes, load balancers or security groups."

# Scaling + extras
# N is checked against the node group's maxSize (worker_max_nodes/gpu_max_nodes
# in the cluster stack); minSize stays 0. The module ignores desired_size, so TF
# never reverts this.
define scale_ng
	@if [ -z "$(N)" ]; then echo "Specify N=<count>, e.g. make $@ N=2"; exit 1; fi
	@MAX=$$(aws eks describe-nodegroup --region $(REGION) --cluster-name $(CLUSTER_NAME) --nodegroup-name $(1) --query 'nodegroup.scalingConfig.maxSize' --output text); \
	 if [ "$(N)" -gt "$$MAX" ]; then echo "Error: N=$(N) > maxSize $$MAX of $(1) (raise it in the cluster stack tfvars)."; exit 1; fi; \
	 aws eks update-nodegroup-config --region $(REGION) --cluster-name $(CLUSTER_NAME) --nodegroup-name $(1) \
		--scaling-config minSize=0,maxSize=$$MAX,desiredSize=$(N) --query 'update.status' --output text
endef

scale-worker-cpu: check-account
	$(call scale_ng,workercpu)

scale-gpu: check-account
	$(call scale_ng,gpu)

# Static checks (no AWS access needed)
fmt:
	terraform fmt -check -recursive .

# Separate TF_DATA_DIR: `init -backend=false` in the real .terraform would
# break the backend of a live session.
validate:
	@for d in $(BOOTSTRAP_DIR) $(BASE_DIR) $(CLUSTER_DIR); do \
		echo "== $$d"; \
		(cd $$d && export TF_DATA_DIR=.terraform-validate && \
		 terraform init -backend=false -input=false >/dev/null && terraform validate) || exit 1; \
	 done
