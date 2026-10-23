# Kubernetes manifests: the cloud-neutral base

Everything the system runs, with no cloud in it. Each cloud's overlay
(`configs/gcp/`, `configs/azure/`, `configs/aws/`) references these
directories and adds only what differs: the StorageClasses behind `ssd` and
`balanced`, the ClusterSecretStore the ExternalSecrets read from, the workload
identity of the External Secrets Operator and of pgBackRest, the backup
repository, the worker node pools, the image registry and tags, and the
LoadBalancer annotations.

## Layout

```
configs/base/
├── kustomization.yaml          # everything below except gpu/
├── base/                       # namespaces
├── operators/                  # External Secrets Operator, PGO (vendored chart)
├── secrets/                    # ExternalSecrets: api, redis, minio, rabbitmq, grafana
├── infra/                      # PostgreSQL (PGO), RabbitMQ, MinIO, Redis with Sentinel (chart)
├── apps/
│   ├── dashboard.yml           # dashboard-backend (the API) and dashboard-frontend
│   ├── orchestrator.yml, merger.yml       # V1 epoch pipeline
│   ├── watchdog.yml, comparison-worker.yml
│   ├── env/                    # env shared by the Deployments (PostgreSQL, RabbitMQ, MinIO, Redis, tracing), one patch per group
│   ├── security/               # pod securityContext of the app Deployments (non-root, no capabilities)
│   ├── network-policies.yml    # network rules of the nbody namespace
│   └── workers/                # worker-distributed-v{1,2}-{cpu,gpu}, worker-monolithic-{cpu,gpu}
├── monitoring/                 # LGTM stack (scaled to zero), Alloy, Grafana dashboards
└── gpu/                        # NVIDIA device plugin, included by the AKS and EKS overlays
```

The base does not render a working cluster by itself (no StorageClasses, no
secret store, image names without a registry): deploy an overlay, with
`make deploy-k8s` in its `infra/<cloud>/`. Every overlay applies the same
stages: the base, then the operators (waiting for their CRDs), then its
ClusterSecretStore, then everything else, all rendered with `--enable-helm`.

## Overlays

Each overlay has a directory per part of the base it uses, referencing the
base's and patching only what its cloud needs (GKE has no `gpu/`, since GKE
installs the device plugin, and takes `monitoring/` unchanged), plus
`session/`: a component with the deployment's account values (registry, identities, secret store, backup
bucket), written by `make configure` and git-ignored.

| | GKE (`gcp/`) | AKS (`azure/`) | EKS (`aws/`) |
|---|---|---|---|
| StorageClasses `ssd`, `balanced` | PD SSD, PD balanced | Premium SSD, Standard SSD | gp3 |
| Secret store | Secret Manager | Key Vault | SSM Parameter Store |
| Workload identity | GKE Workload Identity | Azure Workload Identity | IRSA |
| pgBackRest repository | GCS | Blob storage | S3 |
| Worker scheduling | node selectors | node selectors, Spot toleration | node selectors |
| Grafana | load balancer restricted to your IP | internal load balancer | load balancer restricted to your IP |
| GPU device plugin | installed by GKE | `gpu/` | `gpu/` |
| NetworkPolicies (`apps/network-policies.yml`) | enforced (Calico) | not enforced (no policy engine) | not enforced (VPC CNI default) |

## Shared env

The settings most Deployments share live in `apps/env/`, one JSON patch per
group (`base`: PostgreSQL, Python output and log format, then `rabbitmq`,
`minio`, `redis` and `tracing`). A Deployment gets a group by carrying its annotation, e.g.
`nbody.env/minio: "true"`, and `apps/kustomization.yaml` appends the group's
entries to its env. A setting that differs between Deployments (such as the
CPU monolithic worker's `RABBITMQ_HEARTBEAT`) stays in the manifest.

## Images

The manifests name the images `kubenbody`, `kubenbody-gpu` and `kubenbody-dashboard`.
Each overlay's `session/` sets their registry and its `apps/kustomization.yaml`
their tag (`scripts/build-and-push.sh` updates the tags).

## Secrets

The External Secrets Operator syncs `api-secrets`, `redis-secrets`,
`minio-secrets`, `rabbitmq-secrets` and `grafana-credentials` from the cloud's
secret store (`kubectl get externalsecrets -A`: `grafana-credentials` is in `monitoring`). PGO creates the database
credentials itself (`nbody-db-pguser-nbody`).

## Useful commands

With `K="kubectl --context <cluster context>"` (`nbody-aks`, `nbody-eks`, or the
`gke_...` context `make connect` writes):

```bash
$K get pods -n nbody
$K scale deployment worker-distributed-v2-cpu -n nbody --replicas=4
$K logs -f deployment/worker-distributed-v2-cpu -n nbody
$K get svc dashboard-frontend-svc -n nbody   # EXTERNAL-IP: an IP, a hostname on EKS
$K -n monitoring scale deployment/lgtm --replicas=1   # start Grafana (scaled to 0 on GKE and AKS)
$K port-forward svc/rabbitmq-svc 15672:15672 -n nbody   # RabbitMQ management
```
