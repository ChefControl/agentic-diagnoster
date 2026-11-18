# agentic-diagnoster

An AI agent that investigates a local Kubernetes cluster, finds the root cause
of an issue, backs it with evidence, and recommends the next action.

> **Status:** the local cluster and its safety boundary are done. Next: failure
> scenarios, a UI to run them, then the agent itself, its observability and
> evals, and containers.

## Prerequisites

- Docker
- [kind](https://kind.sigs.k8s.io/) (tested with v0.32.0)
- kubectl

## Quick start

```bash
./cluster.sh --up          # create the cluster and the safety boundary (~30s)
```

## Cluster

One script manages the local cluster:

```bash
./cluster.sh --up     # create the cluster and the safety boundary
./cluster.sh --down   # delete everything
```

`--up` creates a kind cluster called `diagnoster`, preloads the scenario
images, installs the safety boundary (identities, scenario namespaces, access
rules), **verifies** every permission with the API server, and writes
kubeconfigs to `.kube/`. It is safe to run again, for example to issue fresh
tokens when they expire.

The script always passes `--context kind-diagnoster` to kubectl, so it never
touches whatever cluster your kubeconfig currently points at.

It does not deploy scenario workloads; those come with the scenarios.

## Safety boundary

Nothing in this project uses your admin kubeconfig after `--up`. Three
identities, defined in [rbac/diagnoster-rbac.yaml](rbac/diagnoster-rbac.yaml),
each get only what their job needs, and the **API server** enforces it. A
confused or prompt-injected agent still can't exceed these permissions.

| Identity | Used by | Can | Cannot |
|---|---|---|---|
| `diagnoster-reader` | the agent, every investigation step | get/list pods, logs, events, Services, endpoints, ConfigMaps, Deployments, ReplicaSets in the four scenario namespaces | read Secrets, exec into pods, change anything, see any other namespace |
| `diagnoster-remediator` | the agent's one fix, after approval | get and patch `deployment/payments-api` in `payments` | anything else, including scaling or deleting that same Deployment |
| `diagnoster-scenario-manager` | the scenario tooling, to deploy and reset scenarios | create, change and delete workloads, and clear events, in the four scenario namespaces | read Secrets, exec, create RoleBindings, delete namespaces, touch any other namespace |

The access rules are owned by `cluster.sh`, not by the scenario tooling: it can
fill the scenario namespaces with workloads, but it can never change who may do
what.

`--up` checks every row of this table with `kubectl auth can-i` and fails on
any surprise, so the boundary is tested, not just described.

Kubeconfigs in `.kube/` carry short-lived tokens (24h, `TOKEN_DURATION` to
change). Each identity gets two:

- `.kube/<identity>.kubeconfig` for use from your machine
- `.kube/<identity>.internal.kubeconfig` for use from a container on the `kind`
  Docker network (`docker run --network kind ...`), where the API server is
  reached at `https://diagnoster-control-plane:6443` instead of a localhost port

## CI

[.github/workflows/ci.yml](.github/workflows/ci.yml) runs two jobs:

- **lint:** shellcheck on `cluster.sh`.
- **cluster:** `./cluster.sh --up`, which verifies every permission, then
  confirms the agent's identity can't read Secrets.

## Repository layout

```
cluster.sh                   the one script: --up / --down
cluster/kind-config.yaml     kind cluster (node image pinned by digest)
rbac/diagnoster-rbac.yaml    identities, roles, scenario namespaces, access rules
```

## Notes

- Scenario images are preloaded into the node so scenarios start fast. They are
  saved for the local platform only, because `kind load docker-image` fails on
  multi-platform images with Docker's containerd image store.
