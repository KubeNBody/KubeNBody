# KubeNBody on Azure Kubernetes Service

Terraform for an AKS deployment, and a Makefile that drives it and the deploy of the
`configs/azure/` overlay of `configs/base/`.

| Directory | What |
|---|---|
| `bootstrap/` | Terraform state: resource group, storage account and container (once per subscription) |
| `environments/prod/` | Resource group, network, ACR, AKS, Workload Identity, Key Vault, the pgBackRest storage account and AcrPull |
| `modules/` | `networking`, `aks`, `acr`, `identity`, `keyvault` |
| `scripts/` | `configure.sh`, `init-secrets.sh` |

## Prerequisites

- `az` (logged in), Terraform >= 1.5, `kubectl`, `kustomize` >= 5 and `helm` 3, Docker,
  `python3` and `openssl`
- Four globally unique names: the state storage account (`STATE_SA`), `acr_name`, `key_vault_name` and `pgbackrest_storage_account_name`
- vCPU quota for the pools in the region, and T4 quota for the GPU pool (`make preflight` checks both).
  The system pool is billed while it exists, idle or not: `make destroy` when done

## Deploy

```bash
cd infra/azure
export SUBSCRIPTION_ID=$(az account show --query id -o tsv)

make preflight                       # quotas, disk SKUs and zones in the region
make bootstrap STATE_SA=<unique name>
cp environments/prod/terraform.tfvars.example environments/prod/terraform.tfvars
#   set subscription_id, acr_name, key_vault_name, pgbackrest_storage_account_name
make init STATE_SA=<unique name>
make apply                           # about 15 minutes, most of it AKS
make configure                       # writes configs/azure/session/ (git-ignored) from the Terraform outputs
../../scripts/build-and-push.sh --azure   # builds the images, pushes them to ACR, sets their tag
make init-secrets                    # generates the application secrets in Key Vault
make deploy-k8s                      # staged: operators and CRDs first, then the secret store, then everything
make verify
```

`make deploy-all` runs `configure`, `init-secrets` and `deploy-k8s` in that order, after
`apply` and the image push.

The region and the names come from the Makefile: `LOCATION` (default `polandcentral`),
`RESOURCE_GROUP` and `CLUSTER_NAME`, e.g. `make apply LOCATION=westeurope`, and the same on
every later `make` call. Grafana is deployed scaled to zero, behind an internal load
balancer: `kubectl --context nbody-aks -n monitoring scale deployment/lgtm --replicas=1`.

The dashboard is at `http://<EXTERNAL-IP>` of
`kubectl --context nbody-aks -n nbody get svc dashboard-frontend-svc`: log in as `user` with
the password `make init-secrets` set or printed.

`make init-secrets` never overwrites a secret that already has a value, and retries while
the Key Vault role assignment propagates. Set `NBODY_DEFAULT_USER_PASSWORD` to choose the
dashboard password, else one is generated (the script prints how to read it). It also stores
the pgBackRest storage key, which reaches the cluster through Key Vault like the other secrets.

`make verify` shows the nodes, the ExternalSecrets (all should be `SecretSynced`: the sign
that Workload Identity, Key Vault and ESO work together), the PostgresCluster, the pods, the
GPUs the nodes advertise and the Grafana load balancer, which gets an internal IP.

`make render` checks the overlay offline: it renders every stage with example values and
validates the result with kubeconform.

## Node pools

| Pool | Default VM size | Default size | Runs |
|---|---|---|---|
| `system` | `Standard_D4s_v5` | 2, across the cluster zones | databases, brokers, API, dashboard, control services |
| `workercpu` | `Standard_D2ls_v5`, on demand | 0, one zone | CPU workers (taint `dedicated=worker`) |
| `gpu` | `Standard_NC4as_T4_v3` (one T4), on demand | 0, one zone | GPU workers (taint `nvidia.com/gpu`) |

The worker pools start empty and are scaled by hand. Scale them back to 0 between runs:
jobs resume when workers come back.

```bash
make scale-worker-cpu N=4
make scale-gpu N=1
```

These scale the node pools. The worker Deployments start at zero too: start the ones you
need, e.g. `kubectl --context nbody-aks -n nbody scale deployment worker-monolithic-cpu
worker-distributed-v2-cpu --replicas=4` (GPU: `worker-monolithic-gpu`,
`worker-distributed-v1-gpu`, `worker-distributed-v2-gpu`), and scale them to zero before the pool.

A VM size is not offered in every zone of every region. If a pool does not allocate, pick
another size in `terraform.tfvars` (`make preflight` shows the zones of the default sizes): in Poland
Central, for example, `Standard_D2als_v7` does not allocate. The
worker pods also tolerate the taint AKS puts on Spot pools, so the worker pool can be moved
to Spot without changing the manifests.

## Notes

- **kubectl context**: every `kubectl` call of the Makefile names the cluster's context
  (`nbody-aks`, written by `make connect`), so a deploy cannot land on another cluster in
  your kubeconfig. Use `--context nbody-aks` for manual commands.
- **GPU**: AKS installs the driver, and the overlay deploys the NVIDIA device plugin.
- **Upgrades**: automatic cluster and node-image upgrades are off, since a node drain
  interrupts long jobs. Upgrade by hand between jobs (`az aks upgrade`, `az aks nodepool
  upgrade --node-image-only`).
- **Backups**: PostgreSQL is backed up by pgBackRest to Azure Blob storage (seven full
  backups are kept). Check with `kubectl --context nbody-aks -n nbody exec <postgres pod> -c
  database -- pgbackrest info`, and test a restore before relying on them.
- **First pulls**: right after `apply`, the AcrPull role may take a few minutes to propagate,
  so the first image pulls can fail and retry.

## Tear down

```bash
make destroy
```

This deletes the environment with its volumes, and with the pgBackRest storage account, so the PostgreSQL backups are deleted too. The state storage from `make bootstrap` is kept.
