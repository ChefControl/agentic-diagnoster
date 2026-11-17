# agentic-diagnoster

An AI agent that investigates a local Kubernetes cluster, finds the root cause
of an issue, backs it with evidence, and recommends the next action.

> **Status:** the cluster, safety boundary, failure scenarios and the reviewer
> UI are done. Next: the chat for asking the agent, then the agent itself, its
> observability and evals, and containers.

## Prerequisites

- Docker
- [kind](https://kind.sigs.k8s.io/) (tested with v0.32.0)
- kubectl
- Node.js 22+ (for the UI, until it is containerized)
- [Arize Phoenix](https://arize.com/docs/phoenix) for agent traces and eval
  results (one Docker container, see below)

## Quick start

```bash
./cluster.sh --up          # create the cluster and the safety boundary (~30s)
cd ui && npm install
npm run dev                # open http://localhost:5173
```

Phoenix stores the agent's traces and eval experiments. Run it locally:

```bash
docker run -d --name phoenix -p 127.0.0.1:6006:6006 -p 127.0.0.1:4317:4317 \
  -e PHOENIX_ENABLE_AUTH=false -e PHOENIX_TELEMETRY_ENABLED=false \
  -v phoenix-data:/root/.phoenix arizephoenix/phoenix:21.1.0
```

Phoenix 21 turns on login by default, so local use sets `PHOENIX_ENABLE_AUTH=false`.

In the UI, **Scenarios** lists every scenario with its injected fault. Deploy
one and wait until it shows **Reproduced**: every line of its designed state is
checked and it is ready to investigate.

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

It does not deploy scenario workloads: the UI does.

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

From the UI, or from the command line using the same code:

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

## UI

[ui/](ui/) is a React + Vite front end with a small Node API (Fastify) in
TypeScript. The browser never talks to Kubernetes directly: the API does, as
the `diagnoster-scenario-manager` identity.

```
ui/src/web/       React UI
ui/src/server/    API, scenario logic and checks, CLI
ui/src/shared/    types shared by both
ui/test/          unit tests (no cluster needed)
```

**Scenarios** (`#/scenarios`) is written for the reviewer (a white box): every
scenario with its injected fault, category, state and deploy and reset buttons.
Each scenario page shows the injected fault, whether it is ready to
investigate, and the answer key: category, resource, the facts the agent must
find and the correct fix, with grading details (evidence sources, ungraded
facts, unsafe fixes and why) behind a toggle.

Traces and eval results are not rebuilt in the UI: they live in Phoenix, which
the sidebar links to.

```bash
npm run dev         # API on :8787 and UI on :5173 with live reload
npm test            # unit tests
npm run typecheck
npm run build && npm start   # production: one server on :8787 serving UI and API
```

Configuration is environment-based:

| Variable | Default | Purpose |
|---|---|---|
| `API_PORT` | `8787` | API server port |
| `HOST` | `127.0.0.1` | API bind address |
| `SCENARIO_MANAGER_KUBECONFIG` | `.kube/scenario-manager.kubeconfig` | the UI's cluster identity |
| `SCENARIOS_DIR` | `scenarios/` | scenario definitions |
| `PHOENIX_URL` | `http://localhost:6006` | where the UI links to traces and evals, and checks that Phoenix is up |

The kubeconfig is read on every request, so recreating the cluster needs no
restart. If the cluster is down or the token expired, the UI says so and tells
you to run `./cluster.sh --up`.

## Safety boundary

Nothing in this project uses your admin kubeconfig after `--up`. Three
identities, defined in [rbac/diagnoster-rbac.yaml](rbac/diagnoster-rbac.yaml),
each get only what their job needs, and the **API server** enforces it. A
confused or prompt-injected agent still can't exceed these permissions.

| Identity | Used by | Can | Cannot |
|---|---|---|---|
| `diagnoster-reader` | the agent, every investigation step | get/list pods, logs, events, Services, endpoints, ConfigMaps, Deployments, ReplicaSets in the four scenario namespaces | read Secrets, exec into pods, change anything, see any other namespace |
| `diagnoster-remediator` | the agent's one fix, after approval | get and patch `deployment/payments-api` in `payments` | anything else, including scaling or deleting that same Deployment |
| `diagnoster-scenario-manager` | the UI, to deploy and reset scenarios | create, change and delete workloads, and clear events, in the four scenario namespaces | read Secrets, exec, create RoleBindings, delete namespaces, touch any other namespace |

The access rules are owned by `cluster.sh`, not by the UI: the UI can fill the
scenario namespaces with workloads, but it can never change who may do what.

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

- **build:** shellcheck on `cluster.sh`, then type-check, unit tests and a
  production build of the UI. No cluster needed.
- **cluster:** `./cluster.sh --up` (which verifies all permissions), deploys
  every scenario through the same TypeScript code the UI uses, waits until each
  reproduces, checks that reset restores the broken state, and confirms the
  agent's identity can't read Secrets.

No LLM is involved in either, so both are free and deterministic.

## Repository layout

```
cluster.sh                   the one script: --up / --down
cluster/kind-config.yaml     kind cluster (node image pinned by digest)
rbac/diagnoster-rbac.yaml    identities, roles, scenario namespaces, access rules
scenarios/<name>/            manifest and answer key per scenario
ui/                          the UI and its API
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
