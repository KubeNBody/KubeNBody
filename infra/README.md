# Infrastructure

One Terraform root per cloud, each driven by its own Makefile, and each
deploying the Kustomize overlay of the same name in `configs/` (all overlays
of the cloud-neutral `configs/base/`):

| Cloud | Terraform and Makefile | Manifests |
|---|---|---|
| Google GKE | [`gcp/`](gcp/README.md) | `configs/gcp/` |
| Azure AKS | [`azure/`](azure/README.md) | `configs/azure/` |
| Amazon EKS | [`aws/`](aws/README.md) | `configs/aws/` |

`common/` holds what the three share: `session.py` writes an overlay's
git-ignored `session/` from the values `make configure` collects,
`render.sh` is the offline check behind `make render`, `app-secrets.sh` lists
the application secrets each `init-secrets.sh` writes, and `deploy-k8s.sh`
deploys in stages: the base, the operators (waiting for their CRDs), the
ClusterSecretStore, then everything else. Every Makefile pins `kubectl` to its
own cluster's context, never the current one (`make connect` adds that context to your
kubeconfig; on GKE and AKS it also makes it the current one).
