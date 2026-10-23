#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# Generate configs/aws/session/ — the per-session values of the AWS overlay
# Writes three gitignored files (no account IDs are ever committed):
#   configs/aws/session/kustomization.yaml     kustomize Component (JSON = YAML)
#   configs/aws/session/cluster-secret-store.yml  ClusterSecretStore aws-ps
#   configs/aws/session/session.env            values the Makefile reads
#
# The component sets:
#   - ECR renames: app images (kubenbody... -> <ecr>/<name>) and every third-party
#     image in infra/aws/mirror-images.json (-> <ecr>/mirror/...)
#   - IRSA role annotations: ESO ServiceAccount, PostgresCluster (PGO copies it
#     to the SAs it creates, incl. the instance SA), nbody-db-pgbackrest SA
#   - pgBackRest repo1: bucket/endpoint/region and repo1-path = this session's
#     GENERATION; Crunchy images from the ECR mirrors
#   - spec.dataSource (restore) when a previous generation exists
#   - lgtm loadBalancerSourceRanges = your /32
#
# pgBackRest GENERATIONS (Crunchy's documented clone pattern — restore FROM the
# old repo path, write to a NEW one; never two clusters on one repo):
#   s3://<bucket>/pgbackrest/nbody-db/CURRENT  last generation with a final
#                                              backup (moved by `make down`)
#   s3://<bucket>/pgbackrest/nbody-db/ACTIVE   the running session's generation,
#                                              what it restored from, and the
#                                              cluster's OIDC issuer (unique per
#                                              cluster)
# The first configure of a session creates ACTIVE (new generation, restore from
# CURRENT if any); later runs in the same session reuse it, so re-running
# configure (e.g. after your IP changed) never moves the repo of a live cluster.
# An ACTIVE written by a DIFFERENT cluster (crashed session, cluster destroyed by
# hand) is stale: reusing its generation would put two clusters' WAL in one
# repo, so a new generation is started and the stale one is reported.
#
# Usage:
#   configure.sh             real values (cluster stack outputs, S3 pointers,
#                            your public IP; ADMIN_CIDR overrides the IP)
#   configure.sh --example   dummy values, no AWS calls (offline render/checks);
#                            EXAMPLE_RESTORE_FROM=<gen> adds the restore patch
set -euo pipefail

# Never the shell's AWS_PROFILE (it may belong to another account):
# the deploy profile, also when this script is run outside the Makefile.
export AWS_PROFILE="${NBODY_AWS_PROFILE:-nbody}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AWS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT_DIR="$(cd "$AWS_DIR/../.." && pwd)"
CLUSTER_DIR="$AWS_DIR/environments/prod/cluster"
MIRRORS="$AWS_DIR/mirror-images.json"
PG_PREFIX="pgbackrest/nbody-db"

MODE=real
[ "${1:-}" = "--example" ] && MODE=example

tf_out() { (cd "$CLUSTER_DIR" && terraform output -raw "$1" 2>/dev/null || true); }

if [ "$MODE" = example ]; then
    ACCOUNT=000000000000
    REGION=eu-central-1
    ECR_REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
    ESO_ROLE="arn:aws:iam::$ACCOUNT:role/nbody-eks-eso"
    PGBR_ROLE="arn:aws:iam::$ACCOUNT:role/nbody-eks-pgbackrest"
    BUCKET=nbody-backups-example
    VPC_ID=vpc-00000000000000000
    CLUSTER=nbody-eks
    ADMIN_CIDR="${ADMIN_CIDR:-203.0.113.10/32}"   # TEST-NET-3, never routable
    PG_GEN=example
    RESTORE_FROM="${EXAMPLE_RESTORE_FROM:-}"
else
    command -v aws >/dev/null || { echo -e "${RED}aws CLI not found${NC}"; exit 1; }
    REGION="$(tf_out region)"
    ECR_REGISTRY="$(tf_out ecr_registry)"
    ESO_ROLE="$(tf_out eso_role_arn)"
    PGBR_ROLE="$(tf_out pgbackrest_role_arn)"
    BUCKET="$(tf_out backup_bucket)"
    VPC_ID="$(tf_out vpc_id)"
    CLUSTER="$(tf_out cluster_name)"
    OIDC="$(tf_out cluster_oidc_issuer_url)"
    for v in REGION ECR_REGISTRY ESO_ROLE PGBR_ROLE BUCKET VPC_ID CLUSTER OIDC; do
        if [ -z "${!v}" ]; then
            echo -e "${RED}terraform output for $v is empty — apply the cluster stack first (make cluster-apply).${NC}"
            exit 1
        fi
    done
    if [ -z "${ADMIN_CIDR:-}" ]; then
        IP="$(curl -fsS --max-time 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)"
        [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo -e "${RED}could not determine your public IPv4 (set ADMIN_CIDR)${NC}"; exit 1; }
        ADMIN_CIDR="$IP/32"
    fi
    [[ "$ADMIN_CIDR" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] || { echo -e "${RED}ADMIN_CIDR=$ADMIN_CIDR is not an IPv4 CIDR such as 203.0.113.10/32${NC}"; exit 1; }

    # Generation pointers (see header). A missing pointer (404) is normal; any
    # OTHER error (access denied, wrong profile, network) must not be mistaken
    # for "no backup" — that would silently skip the restore.
    s3get() {
        local key="$PG_PREFIX/$1" err
        if err="$(aws s3api head-object --bucket "$BUCKET" --key "$key" --region "$REGION" 2>&1 >/dev/null)"; then
            aws s3 cp "s3://$BUCKET/$key" - --region "$REGION"
        elif printf '%s' "$err" | grep -qE '\(404\)|Not Found'; then
            return 0
        else
            echo -e "${RED}cannot read s3://$BUCKET/$key: $err${NC}" >&2
            return 1
        fi
    }
    ACTIVE="$(s3get ACTIVE)"
    ACTIVE_OIDC="$(printf '%s\n' "$ACTIVE" | sed -n 's/^cluster=//p')"
    if [ -n "$ACTIVE" ] && [ "$ACTIVE_OIDC" = "$OIDC" ]; then
        PG_GEN="$(printf '%s\n' "$ACTIVE" | sed -n 's/^gen=//p')"
        RESTORE_FROM="$(printf '%s\n' "$ACTIVE" | sed -n 's/^restore_from=//p')"
        echo -e "${YELLOW}Reusing this session's pgBackRest generation $PG_GEN (ACTIVE pointer).${NC}"
    else
        if [ -n "$ACTIVE" ]; then
            STALE="$(printf '%s\n' "$ACTIVE" | sed -n 's/^gen=//p')"
            if [ "${ACCEPT_STALE:-}" != 1 ]; then
                echo -e "${RED}Stale ACTIVE pointer: generation $STALE was written by another cluster, a session"
                echo -e "that ended without make down. Its data is newer than CURRENT's. If it holds a completed backup"
                echo -e "(aws s3 ls s3://$BUCKET/$PG_PREFIX/$STALE/backup/ --region $REGION --profile $AWS_PROFILE), restore it:"
                echo -e "  echo $STALE | aws s3 cp - s3://$BUCKET/$PG_PREFIX/CURRENT --region $REGION --profile $AWS_PROFILE"
                echo -e "  aws s3 rm s3://$BUCKET/$PG_PREFIX/ACTIVE --region $REGION --profile $AWS_PROFILE"
                echo -e "or start from CURRENT anyway: re-run with ACCEPT_STALE=1. Nothing was changed.${NC}"
                exit 1
            fi
            echo -e "${YELLOW}Stale ACTIVE pointer (generation $STALE) ignored (ACCEPT_STALE=1): new generation.${NC}"
        fi
        PG_GEN="$(date -u +%Y%m%dt%H%M%Sz)"
        RESTORE_FROM="$(s3get CURRENT | tr -d '[:space:]')"
        printf 'gen=%s\nrestore_from=%s\ncluster=%s\n' "$PG_GEN" "$RESTORE_FROM" "$OIDC" \
            | aws s3 cp - "s3://$BUCKET/$PG_PREFIX/ACTIVE" --region "$REGION" >/dev/null
        echo -e "${GREEN}New pgBackRest generation $PG_GEN${RESTORE_FROM:+ (restoring from $RESTORE_FROM)}.${NC}"
    fi
    [ -n "$PG_GEN" ] || { echo -e "${RED}ACTIVE pointer is malformed: s3://$BUCKET/$PG_PREFIX/ACTIVE${NC}"; exit 1; }
fi

MODE="$MODE" REGION="$REGION" ECR_REGISTRY="$ECR_REGISTRY" ESO_ROLE="$ESO_ROLE" \
PGBR_ROLE="$PGBR_ROLE" BUCKET="$BUCKET" ADMIN_CIDR="$ADMIN_CIDR" PG_GEN="$PG_GEN" \
RESTORE_FROM="$RESTORE_FROM" PG_PREFIX="$PG_PREFIX" MIRRORS="$MIRRORS" \
CLUSTER="$CLUSTER" VPC_ID="$VPC_ID" \
python3 - <<'PY' | python3 "$AWS_DIR/../common/session.py"
import json, os

e = os.environ
ecr, region, bucket = e["ECR_REGISTRY"], e["REGION"], e["BUCKET"]
endpoint = f"s3.{region}.amazonaws.com"
repo_path = lambda gen: f"/{e['PG_PREFIX']}/{gen}"
# the app images by their configs/base names (configs/aws/apps sets the tags)
images = [{"name": n, "newName": f"{ecr}/{n}", "app": True} for n in ("kubenbody", "kubenbody-gpu", "kubenbody-dashboard")]
pgo_ops = []
for m in json.load(open(e["MIRRORS"])):
    target = f"{ecr}/{m['repo']}"
    if "match" in m:
        images.append({"name": m["match"], "newName": target})
    else:
        # PGO takes these from the PostgresCluster spec, not from a container
        # image line; the mirror keeps the upstream tag.
        pgo_ops.append({"op": "add", "path": m["pgo_field"], "value": f"{target}:{m['tag']}"})

pg_ops = [
    {"op": "add", "path": "/spec/metadata",
     "value": {"annotations": {"eks.amazonaws.com/role-arn": e["PGBR_ROLE"]}}},
    {"op": "replace", "path": "/spec/backups/pgbackrest/repos/0/s3",
     "value": {"bucket": bucket, "endpoint": endpoint, "region": region}},
    {"op": "replace", "path": "/spec/backups/pgbackrest/global/repo1-path",
     "value": repo_path(e["PG_GEN"])},
] + pgo_ops
if e.get("RESTORE_FROM"):
    # Crunchy "clone from S3": restore FROM the previous generation; the new
    # cluster writes to its own generation. Only used when the cluster is created.
    pg_ops.append({"op": "add", "path": "/spec/dataSource", "value": {"pgbackrest": {
        "stanza": "db",
        "configuration": [{"secret": {"name": "nbody-db-pgbackrest-secret"}}],
        "global": {"repo1-path": repo_path(e["RESTORE_FROM"])},
        "repo": {"name": "repo1", "s3": {"bucket": bucket, "endpoint": endpoint, "region": region}},
    }}})

def sa_patch(name, ns, role):
    return {"apiVersion": "v1", "kind": "ServiceAccount",
            "metadata": {"name": name, "namespace": ns,
                         "annotations": {"$patch": "replace", "eks.amazonaws.com/role-arn": role}}}

spec = {
    "cloud": "aws",
    "mode": e["MODE"],
    "script": "infra/aws/scripts/configure.sh",
    "images": images,
    "patches": [
        {"target": {"kind": "ServiceAccount", "name": "external-secrets", "namespace": "external-secrets"},
         "patch": sa_patch("external-secrets", "external-secrets", e["ESO_ROLE"])},
        {"target": {"kind": "ServiceAccount", "name": "nbody-db-pgbackrest"},
         "patch": sa_patch("nbody-db-pgbackrest", "nbody", e["PGBR_ROLE"])},
        {"target": {"kind": "PostgresCluster", "name": "nbody-db"}, "patch": pg_ops},
        {"target": {"kind": "Service", "name": "lgtm"},
         "patch": [{"op": "add", "path": "/spec/loadBalancerSourceRanges", "value": [e["ADMIN_CIDR"]]}]},
    ],
    "secret_store": f"""apiVersion: external-secrets.io/v1beta1   # ESO 0.9.9
kind: ClusterSecretStore
metadata:
  name: aws-ps
spec:
  provider:
    aws:
      service: ParameterStore
      region: {region}
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
""",
    "env": {"REGION": region, "CLUSTER": e["CLUSTER"], "ECR_REGISTRY": ecr, "BACKUP_BUCKET": bucket,
            "VPC_ID": e["VPC_ID"], "PG_GEN": e["PG_GEN"], "RESTORE_FROM": e.get("RESTORE_FROM", ""),
            "ADMIN_CIDR": e["ADMIN_CIDR"]},
}
print(json.dumps(spec))
PY

echo -e "${GREEN}Wrote configs/aws/session/ ($MODE): generation=$PG_GEN restore_from=${RESTORE_FROM:-<none>} grafana_cidr=$ADMIN_CIDR${NC}"
