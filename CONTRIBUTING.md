# Contributing to KubeNBody

Bug reports, questions and pull requests are welcome through the repository's issue tracker.

## Before a pull request

Set up the local stack and the development environment as in the [README](README.md#development), then check:

```bash
cd code
poetry run ruff check .
poetry run pytest
cd web-dashboard && npm run type-check && npm run lint && npm test
```

For a change under `configs/` or `infra/`, also run `make render` in `infra/<cloud>/` for every cloud the change touches, and `terraform validate` in the Terraform roots it touches. The CI workflow runs all of these.

`code/tests/data/trajectories/` holds bitwise reference trajectories for every integrator, force algorithm and engine. A change that is meant to alter the numerics must record them again (`cd code && PYTHONPATH=src:tests poetry run python -m trajectories`, and `--gpu` on a CUDA machine) and say so in the pull request. Any other change must leave them unchanged.

## Adding an integrator

1. Add its name to `IntegrationMethod` in `code/src/integrators.py`, and, if the steppers need to recognise it with its variants, an `is_<name>` helper as `is_rk4` does. A multi-stage method used in distributed jobs also gets predicted-background variants (frozen, linear, accel), as `grk4_*` and `gfr_*` do.
2. Write the monolithic and package steppers in `code/src/physics.py` and dispatch to them in `code/src/workers/backends/cpu_backend.py` (`compute_step` and `compute_package_step`).
3. Write the GPU steppers in `code/src/physics_gpu.py` (the `_step_*` and `_package_*` methods of `GPUNBodyEngine`).
4. List it in the dashboard: `code/web-dashboard/src/types/api.ts`, `src/lib/format-integration.ts` and the submit form (`src/components/submit/`).
5. Test it: the convergence order (`tests/test_integrators.py`), conservation (`tests/test_conservation.py`), the reference steppers (`tests/reference_steppers.py`), GPU parity (`tests/test_gpu_parity.py`), and record its reference trajectories.

## Adding a compute backend

A compute backend computes steps for the workers. Implement `ComputeBackend` from `code/src/workers/base/compute_backend.py`: `compute_step` (a whole system) and `compute_package_step` (one package against the epoch-start state of all bodies). Backends that keep the state on the device also implement the resident-state methods, as `GPUBackend` does. Raise `ComputeError` on failures, marking whether they are recoverable. A worker entry point picks the backend: see `code/src/worker_monolithic.py` and `code/src/v2/worker_v2.py`.

## Adding a message backend

A message backend carries the distributed tasks. Implement `MessageBackend` and `TaskHeartbeat` from `code/src/workers/base/message_backend.py`: claiming a task, reclaiming a stale one, ack and nack, and keeping a claimed task alive while it is computed. `RabbitMQBackend` and `RedisBackend` in `code/src/workers/backends/` are the two implementations.

## Adding a cloud

1. Add an overlay `configs/<cloud>/` shaped like the existing three: a directory per part of the base it uses (`base/` with the storage classes, `infra/`, `apps/`, `operators/`, `secrets/`, plus `gpu/` and `monitoring/` where it changes or adds to them) and `stages/operators/`, with patches only for what differs from `configs/base/` (storage classes, node selection, secret store and workload identity, backup target, registry). Account values go into the git-ignored `session/` component, never into committed files.
2. Add `infra/<cloud>/` with the Terraform roots and a Makefile whose `configure`, `render`, `init-secrets` and `deploy-k8s` targets use the shared scripts in `infra/common/` (`session.py`, `render.sh`, `app-secrets.sh`, `deploy-k8s.sh`). Its `configure.sh` marks the three application images with `"app": true`, so `NBODY_IMAGE_REGISTRY` can replace their registry.
3. Teach `scripts/build-and-push.sh` the cloud's registry and login, and add the cloud to the `manifests` matrix of `.github/workflows/ci.yml`.

## Style

Match the surrounding code: its naming, comment density and idiom. Comments say why, not what. Write exponents with a caret (`O(N^2)`, `O(dt^4)`).

## Licence

By contributing you agree that your contributions are licensed under the Apache License 2.0, the licence of the project.
