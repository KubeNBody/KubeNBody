# KubeNBody on Google Kubernetes Engine

Terraform for a GKE deployment, and a Makefile that drives it and the deploy of the
`configs/gcp/` overlay of `configs/base/`.

```
infra/gcp/
├── bootstrap/               # Terraform state bucket and the project's APIs (once per project)
├── environments/prod/       # The environment: network, IAM, registry, cluster, secrets
├── modules/
│   ├── networking/          # VPC, subnet, firewall
│   ├── iam/                 # Service accounts and Workload Identity bindings
│   ├── artifact-registry/   # Docker repository for the images
│   ├── gke/                 # Regional cluster and node pools
│   └── secrets/             # Secret Manager secrets (values set by init-secrets)
├── scripts/                 # configure.sh, init-secrets.sh
└── Makefile
```

## Prerequisites

- `gcloud` (logged in), Terraform >= 1.5, `kubectl`, `kustomize` >= 5 and `helm` 3, Docker,
  `python3`, `curl` and `openssl`
- A GCP project with billing enabled, and GPU quota in the region for the GPU pool. The cluster
  and its core pool are billed while they exist, idle or not: `make destroy` when done

## Deploy

```bash
export PROJECT_ID=<your project>      # else the gcloud default project
cd infra/gcp

make bootstrap        # once: state bucket <project>-nbody-tfstate, APIs
make init
make apply            # network, IAM, Artifact Registry, cluster, node pools, Secret Manager
make configure        # writes configs/gcp/session/ (git-ignored) from the Terraform outputs
make init-secrets     # generates the application secrets in Secret Manager
../../scripts/build-and-push.sh --gcp # builds the images, pushes them, sets their tag in configs/gcp
make deploy-k8s       # staged: operators and CRDs first, then the secret store, then everything
```

`make init-secrets` never overwrites a secret that already has a value. Set
`NBODY_DEFAULT_USER_PASSWORD` to choose the dashboard password, else one is generated:
read it with `gcloud secrets versions access latest --secret nbody-default-user-password --project <project>`.
PostgreSQL needs no secret: the operator (PGO) generates its credentials.

`make render` checks the overlay offline: it renders every stage with example values and
validates the result with kubeconform. `REGION` (default `europe-west1`) and `CLUSTER_NAME`
(default `nbody-cluster`) can be set on the `make` command line, and the other variables in
`environments/prod/terraform.tfvars` (see `terraform.tfvars.example`). The worker pools go in
zone `<region>-b` unless `worker_zone` says otherwise.

Grafana is deployed scaled to zero, behind a load balancer open to your public IP only
(`make configure` detects it, `ADMIN_CIDR=<cidr>` overrides it; re-run `make configure
deploy-k8s` when your IP changes):
`kubectl --context gke_<project>_<region>_<cluster> -n monitoring scale deployment/lgtm --replicas=1`
starts it (the admin password is
the `nbody-grafana-admin-password` secret).

The dashboard is at `http://<EXTERNAL-IP>` of
`kubectl --context gke_<project>_<region>_<cluster> -n nbody get svc dashboard-frontend-svc`: log
in as `user` with the password `make init-secrets` set or printed.

## Node pools

| Pool | Default machine | Default size | Runs |
|---|---|---|---|
| `cpu-pool` | `e2-standard-4` | 1 per zone (regional, all the region's zones) | databases, brokers, API, dashboard, control services |
| `worker-cpu` | `c2d-highcpu-2`, Spot | 0, one zone | CPU workers (tainted) |
| `gpu-pool` | `n1-standard-4` + T4, on demand | 0, one zone | GPU workers (tainted) |

The worker pools start empty and are scaled by hand:

```bash
make scale-worker-cpu N=4
make scale-gpu N=1
make scale-cpu N=1          # per zone
```

These scale the node pools. The worker Deployments start at zero too: start the ones you
need, e.g. `kubectl --context gke_<project>_<region>_<cluster> -n nbody scale deployment worker-monolithic-cpu
worker-distributed-v2-cpu --replicas=4` (GPU: `worker-monolithic-gpu`,
`worker-distributed-v1-gpu`, `worker-distributed-v2-gpu`), and scale them to zero before the pool.
Scale them back to 0 between runs: jobs resume when workers come back. The cluster follows
GKE's REGULAR release channel and upgrades its nodes automatically, which drains them: a job
running on a drained node resumes on another, but pause long runs or set a maintenance window.

## Secrets

Terraform creates the secrets in Secret Manager, `make init-secrets` sets their values and
the External Secrets Operator syncs them into the cluster through Workload Identity: the
Redis, MinIO, RabbitMQ and Grafana credentials, the API's JWT signing key and the dashboard
password.

## Backups

PostgreSQL is backed up by pgBackRest to a GCS bucket (seven full backups are kept).

## Tear down

```bash
make destroy
```

This deletes the `nbody` and `monitoring` namespaces first, so the load balancers and disks
the cluster created go with them, then the cluster. If Terraform refuses to delete the cluster
because of its deletion protection, run `make apply` once and `make destroy` again. Empty the backup bucket first (`gsutil -m rm -r 'gs://<project>-pgbackrest/**'`), since Terraform will not delete a bucket that holds objects. The state bucket from `make bootstrap` is kept.
