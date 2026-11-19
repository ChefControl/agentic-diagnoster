#!/usr/bin/env bash
# Local Kubernetes for agentic-diagnoster.
#
#   ./cluster.sh --up     create the kind cluster and the safety boundary:
#                         agent identities, scenario namespaces, access rules,
#                         then verify the rules and write kubeconfigs to .kube/
#   ./cluster.sh --down   delete the cluster and the kubeconfigs
#
# Scenario workloads are deployed and reset from the UI, not by this script.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER="${CLUSTER:-diagnoster}"
KCTX="kind-${CLUSTER}"
IMAGES=(nginx:1.27-alpine busybox:1.36)
IDENTITIES=(reader remediator scenario-manager)
TOKEN_DURATION="${TOKEN_DURATION:-24h}"

# Every kubectl call is pinned to the kind context, so this script can never
# touch whatever cluster your kubeconfig happens to point at.
k() { kubectl --context "$KCTX" "$@"; }

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }

require() {
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null || { fail "missing required tool: $cmd"; exit 1; }
  done
}

cluster_exists() { kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; }

create_cluster() {
  if cluster_exists; then
    log "kind cluster '$CLUSTER' already exists"
  else
    log "creating kind cluster '$CLUSTER'"
    kind create cluster --config "$ROOT/cluster/kind-config.yaml" --wait 120s
  fi
}

# Preloading makes scenarios start fast and avoids registry rate limits.
# Images are saved for this machine's platform only: `kind load docker-image`
# fails on multi-platform images with Docker's containerd image store.
# Best effort: if it fails, the node pulls the images itself.
preload_images() {
  log "preloading scenario images"
  local platform tmp
  platform="linux/$(docker version --format '{{.Server.Arch}}')"
  tmp=$(mktemp -d)
  for img in "${IMAGES[@]}"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull -q --platform "$platform" "$img" >/dev/null
    if docker save --platform "$platform" "$img" -o "$tmp/image.tar" 2>/dev/null \
      && kind load image-archive --name "$CLUSTER" "$tmp/image.tar" >/dev/null 2>&1; then
      ok "$img"
    else
      fail "$img not preloaded, the node will pull it"
    fi
  done
  rm -rf "$tmp"
}

install_boundary() {
  log "installing the safety boundary"
  k apply -f "$ROOT/rbac/diagnoster-rbac.yaml" >/dev/null
  ok "identities, roles, scenario namespaces and access rules"
}

# Ask the API server what each identity may do; fail on any surprise.
verify_boundary() {
  local reader=system:serviceaccount:diagnoster-system:diagnoster-reader
  local remediator=system:serviceaccount:diagnoster-system:diagnoster-remediator
  local manager=system:serviceaccount:diagnoster-system:diagnoster-scenario-manager
  local failures=0

  expect() {
    local want=$1 who=$2 got
    shift 2
    got=$(k auth can-i "$@" --as="$who" 2>/dev/null || true)
    if [[ "$got" == "$want" ]]; then
      ok "${who##*:} can-i $* -> $got"
    else
      fail "${who##*:} can-i $* -> $got (expected $want)"
      failures=$((failures + 1))
    fi
  }

  log "verifying: agent reader is read-only, scenario namespaces only, no Secrets"
  expect yes "$reader" get pods -n shop
  expect yes "$reader" get pods --subresource=log -n shop
  expect yes "$reader" list events -n payments
  expect yes "$reader" get configmaps -n shop
  expect yes "$reader" list endpointslices.discovery.k8s.io -n catalog
  expect no "$reader" get secrets -n shop
  expect no "$reader" create pods --subresource=exec -n shop
  expect no "$reader" delete pods -n shop
  expect no "$reader" patch deployments -n payments
  expect no "$reader" get pods -n kube-system
  expect no "$reader" get pods -n diagnoster-system

  log "verifying: agent remediator may patch one named Deployment, nothing else"
  expect yes "$remediator" patch deployments/payments-api -n payments
  expect no "$remediator" patch deployments/other -n payments
  expect no "$remediator" delete deployments/payments-api -n payments
  expect no "$remediator" patch deployments/payments-api --subresource=scale -n payments
  expect no "$remediator" patch deployments/payments-api -n shop
  expect no "$remediator" get secrets -n payments

  log "verifying: UI scenario manager changes workloads, never permissions"
  expect yes "$manager" create deployments -n shop
  expect yes "$manager" delete configmaps -n payments
  expect yes "$manager" deletecollection pods -n catalog
  expect yes "$manager" deletecollection events -n payments
  expect no "$manager" create rolebindings -n shop
  expect no "$manager" get secrets -n shop
  expect no "$manager" delete namespaces
  expect no "$manager" create deployments -n kube-system
  expect no "$manager" create pods --subresource=exec -n shop

  if ((failures > 0)); then
    fail "$failures permission check(s) failed"
    exit 1
  fi
}

# Kubeconfigs with short-lived tokens:
# - .kube/<identity>.kubeconfig for this machine;
# - .kube/<container>/<identity>.kubeconfig for containers on the "kind" Docker
#   network, where the API server is reached by container name. Each container
#   mounts only its own folder, so it holds only the identities it uses.
container_of() {
  case "$1" in
    reader | remediator) echo agent ;;
    scenario-manager) echo ui ;;
  esac
}

write_kubeconfigs() {
  log "writing kubeconfigs (${TOKEN_DURATION} tokens) to .kube/"
  local out="$ROOT/.kube" server ca internal token dir
  mkdir -p "$out/agent" "$out/ui"
  # Earlier versions kept every container's kubeconfig side by side.
  rm -f "$out"/*.internal.kubeconfig
  server=$(k config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')
  ca=$(k config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
  internal="https://${CLUSTER}-control-plane:6443"
  for id in "${IDENTITIES[@]}"; do
    token=$(k -n diagnoster-system create token "diagnoster-${id}" --duration="$TOKEN_DURATION")
    dir=$(container_of "$id")
    kubeconfig "$server" "$id" "$token" >"$out/${id}.kubeconfig"
    kubeconfig "$internal" "$id" "$token" >"$out/${dir}/${id}.kubeconfig"
    chmod 600 "$out/${id}.kubeconfig" "$out/${dir}/${id}.kubeconfig"
    ok ".kube/${id}.kubeconfig (+ .kube/${dir}/ for the ${dir} container)"
  done
}

kubeconfig() {
  cat <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER}
    cluster:
      server: $1
      certificate-authority-data: ${ca}
users:
  - name: diagnoster-$2
    user:
      token: $3
contexts:
  - name: diagnoster-$2@${CLUSTER}
    context:
      cluster: ${CLUSTER}
      user: diagnoster-$2
current-context: diagnoster-$2@${CLUSTER}
EOF
}

up() {
  require docker kind kubectl
  create_cluster
  preload_images
  install_boundary
  verify_boundary
  write_kubeconfigs
  log "cluster '$CLUSTER' is ready; the tokens last ${TOKEN_DURATION}, run ./cluster.sh --up again to renew them"
}

down() {
  require kind
  log "deleting kind cluster '$CLUSTER'"
  kind delete cluster --name "$CLUSTER"
  # Delete the files but keep the folders: compose.yaml mounts them into the
  # containers, and a running container keeps seeing a deleted folder, so the
  # next --up would write somewhere the container can't see.
  rm -f "$ROOT"/.kube/*.kubeconfig "$ROOT"/.kube/*/*.kubeconfig
  ok "cluster and kubeconfigs removed"
}

case "${1:-}" in
  --up) up ;;
  --down) down ;;
  *)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    [[ "${1:-}" == --help || "${1:-}" == -h ]] || exit 1
    ;;
esac
