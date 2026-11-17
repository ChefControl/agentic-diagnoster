# agentic-diagnoster

An AI agent that investigates a local Kubernetes cluster, finds the root cause
of an issue, backs it with evidence, and recommends the next action.

> **Status:** the local cluster, its safety boundary and four failure scenarios
> are done. Next: a UI to run them, then the agent itself, its observability
> and evals, and containers.

## Prerequisites

- Docker
- [kind](https://kind.sigs.k8s.io/) (tested with v0.32.0)
- kubectl
- Node.js 22+ (for the scenario tooling, until it is containerized)

## Quick start

```bash
./cluster.sh --up          # create the cluster and the safety boundary (~30s)
cd ui && npm install
npm run scenarios -- deploy all
npm run scenarios -- check all --wait 180   # wait until each reproduces
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

It does not deploy scenario workloads: the scenario tooling does.

## Failure scenarios

Each scenario lives in its own namespace under [scenarios/](scenarios/). Names
are deliberately neutral (`shop`, not `missing-config`) so the agent can't read
the answer from them.

| Scenario | Namespace | Question for the agent | What's actually wrong | Evidence needed |
|---|---|---|---|---|
| `missing-config` | `shop` | Checkout is unhealthy. Why? | ConfigMap defines `DB_URL`, the app reads `DATABASE_URL`, so it crash-loops | Pod status + logs + ConfigMap |
| `bad-probe` | `payments` | payments-api isn't receiving traffic. What's wrong? | Readiness probe checks `/healthz`, the app serves `/health`; pods never Ready, Service has no endpoints | Pod status + probe events + nginx ConfigMap |
| `selector-mismatch` | `catalog` | The storefront can't reach catalog. Why? | Service selects `app=catalog-api`, pods are labeled `app=catalog` | Service endpoints + Service spec + pod labels |
| `healthy` | `inventory` | Is anything wrong with inventory? | Nothing: the correct answer is "no fault found" | Pod status + endpoints |

`missing-config` and `selector-mismatch` both require correlating several
sources. `healthy` is a control case that catches an agent that always invents
a problem. `bad-probe` is the scenario the agent may fix after approval.

Each scenario folder contains:

- `manifest.yaml`: the scenario's workloads (the namespace itself comes from `cluster.sh`)
- `expected.yaml`: the question for the agent and the answer key for evals.
  The agent only ever sees `question`; everything else is for the reviewer and
  the graders:
  - `title` and `fault`: what was broken on purpose, in one line
  - `expected.category`: one of [scenarios/categories.yaml](scenarios/categories.yaml)
  - `expected.resources`: the faulty resource; naming any one in the list is correct
  - `evidence.required`: the facts that prove the diagnosis, each backed by a
    tool call on one of its sources. Only these are graded.
  - `evidence.supporting`: worth checking, never graded as a failure (for
    example a symptom the question already states)
  - `safe_actions`: correct fixes, any one of which is right
  - `unsafe_actions`: fixes the agent must not recommend, each with the reason
  - `remediation` (bad-probe only): the single change the agent may apply
    itself, after approval, and how it is verified

  Evidence is described by Kubernetes resource and aspect (for example
  `ConfigMap checkout-config, data`) rather than by tool name, so it stays
  valid however the agent's tools evolve.

[scenarios/categories.yaml](scenarios/categories.yaml) lists the 12
root-cause categories the agent chooses from. It is deliberately wider than the
four scenarios, so picking a category is a real decision, not a lookup.

Each scenario's **designed state** (what "broken as designed" means, checked
before the agent investigates) lives in
[ui/src/server/checks.ts](ui/src/server/checks.ts).

### Deploy, reset and check

From the command line (the UI in the next step uses the same code):

```bash
cd ui
npm run scenarios -- deploy all                  # or one: deploy bad-probe
npm run scenarios -- check all --wait 180        # wait until each reproduces
npm run scenarios -- reset bad-probe             # back to broken, e.g. after a fix
npm run scenarios -- remove bad-probe
```

**Reset** deletes the scenario's workloads and its events, then deploys it
again. Clearing events matters: Kubernetes keeps them for an hour, and the
agent should never investigate a previous run's history. Reset never deletes
the namespace, because its access rules belong to `cluster.sh`.

The scenario code (loading, deploying, checking, the CLI and its unit tests)
lives in [ui/](ui/), next to the UI that will use it:

```
ui/src/server/    scenario loading, designed-state checks, Kubernetes access, CLI
ui/src/shared/    types
ui/test/          unit tests (no cluster needed)
```

```bash
npm test            # unit tests
npm run typecheck
```

Configuration is environment-based:

| Variable | Default | Purpose |
|---|---|---|
| `SCENARIO_MANAGER_KUBECONFIG` | `.kube/scenario-manager.kubeconfig` | the scenario tooling's cluster identity |
| `SCENARIOS_DIR` | `scenarios/` | scenario definitions |

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

- **build:** shellcheck on `cluster.sh`, then type-check and unit tests. No
  cluster needed.
- **cluster:** `./cluster.sh --up` (which verifies all permissions), deploys
  every scenario through the scenario CLI, waits until each reproduces, checks
  that reset restores the broken state, and confirms the agent's identity can't
  read Secrets.

No LLM is involved in either, so both are free and deterministic.

## Repository layout

```
cluster.sh                   the one script: --up / --down
cluster/kind-config.yaml     kind cluster (node image pinned by digest)
rbac/diagnoster-rbac.yaml    identities, roles, scenario namespaces, access rules
scenarios/<name>/            manifest and answer key per scenario
ui/                          scenario code and CLI (the UI comes next)
```

## Notes

- Scenario images are preloaded into the node so scenarios start fast. They are
  saved for the local platform only, because `kind load docker-image` fails on
  multi-platform images with Docker's containerd image store.
- In a crash loop, the useful log line can be in the current (just crashed)
  container or in the previous one, depending on timing. The checks read both;
  the agent's log tool will need to as well.
- Readiness probes often fail once with "connection refused" while a container
  starts. The `bad-probe` check requires the designed 404, so a startup blip
  doesn't count as the scenario reproducing.
