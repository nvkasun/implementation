#!/usr/bin/env bash

# Phase 5A live validation bundle
# Run only after these three workflows are green:
#   1) goldengate-eks-app.yaml -> gg-oracle-payments-01
#   2) goldengate-eks-app.yaml -> gg-postgresql-payments-01
#   3) goldengate-monitor.yaml -> deploy=true, enable_cloudwatch_publication=true

set -u

RUNTIME_NAMESPACE="goldengate-dev"
MONITOR_NAMESPACE="goldengate-monitoring"
ARGOCD_NAMESPACE="argocd"

ORACLE_STS="gg-oracle-payments-01"
POSTGRES_STS="gg-postgresql-payments-01"
MONITOR_DEPLOYMENT="gg-monitor"

ORACLE_ARGO_APP="goldengate-dev-oracle-payments-01"
POSTGRES_ARGO_APP="goldengate-dev-postgresql-payments-01"
MONITOR_ARGO_APP="goldengate-monitor"

LEGACY_ARGO_APP="goldengate-payments-ora-to-pg-001"
LEGACY_NAMESPACE="gg-dev-payments-ora-to-pg-001"

FAILURES=0

pass() {
  printf 'PASS: %s\n' "$*"
}

fail() {
  printf 'FAIL: %s\n' "$*"
  FAILURES=$((FAILURES + 1))
}

section() {
  printf '\n============================================================\n'
  printf '%s\n' "$*"
  printf '============================================================\n'
}

check_rollout() {
  local kind="$1"
  local name="$2"
  local namespace="$3"

  if kubectl rollout status "${kind}/${name}" \
      -n "$namespace" \
      --timeout=5m; then
    pass "${kind}/${name} rollout completed"
  else
    fail "${kind}/${name} rollout failed or timed out"
  fi
}

check_argocd_application() {
  local app="$1"

  local status
  status="$(
    kubectl get application "$app" \
      -n "$ARGOCD_NAMESPACE" \
      -o jsonpath='{.status.sync.status}{"|"}{.status.health.status}' \
      2>/dev/null || true
  )"

  printf '%s: %s\n' "$app" "${status:-NOT_FOUND}"

  if [ "$status" = "Synced|Healthy" ]; then
    pass "${app} is Synced and Healthy"
  else
    fail "${app} is not Synced and Healthy"
  fi
}

check_runtime_statefulset() {
  local sts="$1"
  local expected_container="$2"

  section "Runtime validation: ${sts}"

  if ! kubectl get statefulset "$sts" \
      -n "$RUNTIME_NAMESPACE" >/dev/null 2>&1; then
    fail "StatefulSet ${sts} was not found"
    return
  fi

  local containers
  containers="$(
    kubectl get statefulset "$sts" \
      -n "$RUNTIME_NAMESPACE" \
      -o jsonpath='{.spec.template.spec.containers[*].name}' \
      2>/dev/null
  )"

  printf 'Application containers: %s\n' "${containers:-<none>}"

  if [ "$containers" = "$expected_container" ]; then
    pass "${sts} has exactly one expected GoldenGate container"
  else
    fail "${sts} application-container set is unexpected: ${containers:-<none>}"
  fi

  local init_containers
  init_containers="$(
    kubectl get statefulset "$sts" \
      -n "$RUNTIME_NAMESPACE" \
      -o jsonpath='{.spec.template.spec.initContainers[*].name}' \
      2>/dev/null
  )"

  printf 'Init containers: %s\n' "${init_containers:-<none>}"

  if [ "$init_containers" = "prepare-u02-permissions" ]; then
    pass "${sts} has exactly the expected init container"
  else
    fail "${sts} init-container set is unexpected: ${init_containers:-<none>}"
  fi

  local init_command
  init_command="$(
    kubectl get statefulset "$sts" \
      -n "$RUNTIME_NAMESPACE" \
      -o jsonpath='{.spec.template.spec.initContainers[?(@.name=="prepare-u02-permissions")].command[2]}' \
      2>/dev/null
  )"

  if printf '%s' "$init_command" |
      grep -Fq 'ServiceManager/var/run/ServiceManager.pid'; then
    pass "${sts} retains the ServiceManager.pid safeguard"
  else
    fail "${sts} does not reference the ServiceManager.pid safeguard"
  fi

  if printf '%s' "$init_command" |
      grep -Fq 'rm -f -- "$SERVICE_MANAGER_PID_FILE"'; then
    pass "${sts} retains the exact stale-PID removal command"
  else
    fail "${sts} exact stale-PID removal command is missing"
  fi

  local manifest
  manifest="$(mktemp)"

  if kubectl get statefulset "$sts" \
      -n "$RUNTIME_NAMESPACE" \
      -o yaml >"$manifest"; then

    local forbidden
    forbidden="$(
      grep -Ei \
        'goldengate-observer|observer-enabled|utility-sidecar|fluent-bit|LEGACY_FALLBACK_ENABLED' \
        "$manifest" || true
    )"

    if [ -z "$forbidden" ]; then
      pass "${sts} contains no retired observer or forbidden sidecar references"
    else
      fail "${sts} contains retired or forbidden references"
      printf '%s\n' "$forbidden"
    fi
  else
    fail "Unable to retrieve ${sts} manifest"
  fi

  rm -f "$manifest"

  local services
  services="$(
    kubectl get services \
      -n "$RUNTIME_NAMESPACE" \
      -l "app.kubernetes.io/instance=${sts}" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.spec.clusterIP}{"|"}{.spec.type}{"\n"}{end}' \
      2>/dev/null
  )"

  printf 'Services:\n%s\n' "${services:-<none>}"

  local headless_count normal_count
  headless_count="$(
    printf '%s\n' "$services" |
      awk -F'|' 'NF >= 3 && $2 == "None" {count++} END {print count+0}'
  )"

  normal_count="$(
    printf '%s\n' "$services" |
      awk -F'|' \
        'NF >= 3 && $2 != "None" && ($3 == "ClusterIP" || $3 == "") {
           count++
         }
         END {print count+0}'
  )"

  if [ "$headless_count" -eq 1 ] && [ "$normal_count" -eq 1 ]; then
    pass "${sts} has exactly one normal Service and one headless Service"
  else
    fail "${sts} Service contract is invalid: normal=${normal_count}, headless=${headless_count}"
  fi

  local pod_containers
  pod_containers="$(
    kubectl get pods \
      -n "$RUNTIME_NAMESPACE" \
      -l "app.kubernetes.io/instance=${sts}" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.phase}{"|"}{.spec.containers[*].name}{"|"}{.spec.initContainers[*].name}{"\n"}{end}' \
      2>/dev/null
  )"

  printf 'Pods:\n%s\n' "${pod_containers:-<none>}"

  if [ -z "$pod_containers" ]; then
    fail "No pod found for ${sts}"
  elif printf '%s\n' "$pod_containers" |
      grep -Eiq 'goldengate-observer|utility-sidecar|fluent-bit'; then
    fail "A live ${sts} pod contains a retired or forbidden sidecar"
  else
    pass "Live ${sts} pod contains no observer, utility or Fluent Bit sidecar"
  fi
}

section "1. Kubernetes API connectivity"

if kubectl cluster-info >/dev/null 2>&1; then
  pass "Kubernetes API is reachable"
else
  fail "Kubernetes API is not reachable"
fi

section "2. Argo CD Application status"

check_argocd_application "$ORACLE_ARGO_APP"
check_argocd_application "$POSTGRES_ARGO_APP"
check_argocd_application "$MONITOR_ARGO_APP"

section "3. Runtime and monitor rollouts"

check_rollout statefulset "$ORACLE_STS" "$RUNTIME_NAMESPACE"
check_rollout statefulset "$POSTGRES_STS" "$RUNTIME_NAMESPACE"
check_rollout deployment "$MONITOR_DEPLOYMENT" "$MONITOR_NAMESPACE"

section "4. Canonical runtime pod model"

check_runtime_statefulset "$ORACLE_STS" "ogg-oracle"
check_runtime_statefulset "$POSTGRES_STS" "ogg-postgresql"

section "5. Current runtime and monitor resources"

kubectl get statefulsets,pods,services,pvc \
  -n "$RUNTIME_NAMESPACE" \
  -o wide || fail "Unable to list runtime resources"

kubectl get deployment,pods,service,ingress \
  -n "$MONITOR_NAMESPACE" \
  -o wide || fail "Unable to list monitor resources"

section "6. Shared runtime service accounts"

kubectl get serviceaccount \
  gg-oracle-sa \
  gg-postgresql-sa \
  -n "$RUNTIME_NAMESPACE" \
  -o custom-columns='NAME:.metadata.name,IRSA_ROLE:.metadata.annotations.eks\.amazonaws\.com/role-arn' \
  || fail "Unable to retrieve runtime service accounts"

section "7. Monitor deployment configuration"

MONITOR_IMAGE="$(
  kubectl get deployment "$MONITOR_DEPLOYMENT" \
    -n "$MONITOR_NAMESPACE" \
    -o jsonpath='{.spec.template.spec.containers[0].image}' \
    2>/dev/null
)"

CLOUDWATCH_SWITCH="$(
  kubectl get deployment "$MONITOR_DEPLOYMENT" \
    -n "$MONITOR_NAMESPACE" \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="CLOUDWATCH_PUBLISH_ENABLED")].value}' \
    2>/dev/null
)"

LEGACY_SWITCH="$(
  kubectl get deployment "$MONITOR_DEPLOYMENT" \
    -n "$MONITOR_NAMESPACE" \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="LEGACY_FALLBACK_ENABLED")].value}' \
    2>/dev/null
)"

MONITOR_CONTAINERS="$(
  kubectl get deployment "$MONITOR_DEPLOYMENT" \
    -n "$MONITOR_NAMESPACE" \
    -o jsonpath='{.spec.template.spec.containers[*].name}' \
    2>/dev/null
)"

printf 'Monitor image: %s\n' "${MONITOR_IMAGE:-<missing>}"
printf 'Monitor containers: %s\n' "${MONITOR_CONTAINERS:-<missing>}"
printf 'CLOUDWATCH_PUBLISH_ENABLED=%s\n' "${CLOUDWATCH_SWITCH:-<missing>}"
printf 'LEGACY_FALLBACK_ENABLED=%s\n' "${LEGACY_SWITCH:-<absent>}"

if [ "$MONITOR_CONTAINERS" = "gg-monitor" ]; then
  pass "Monitor Deployment has exactly one gg-monitor container"
else
  fail "Monitor Deployment container set is unexpected: ${MONITOR_CONTAINERS:-<missing>}"
fi

if [ "$CLOUDWATCH_SWITCH" = "true" ]; then
  pass "CloudWatch publication remains enabled"
else
  fail "CLOUDWATCH_PUBLISH_ENABLED is not true"
fi

if [ -z "$LEGACY_SWITCH" ]; then
  pass "LEGACY_FALLBACK_ENABLED is absent"
else
  fail "LEGACY_FALLBACK_ENABLED still exists"
fi

section "8. Select a Running and Ready monitor pod"

MONITOR_POD="$(
  kubectl get pods \
    -n "$MONITOR_NAMESPACE" \
    -l app.kubernetes.io/name=gg-monitor \
    --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp \
    -o name 2>/dev/null |
    tail -n 1 |
    sed 's#^pod/##'
)"

if [ -z "$MONITOR_POD" ]; then
  fail "No Running gg-monitor pod was found"
else
  printf 'Selected monitor pod: %s\n' "$MONITOR_POD"

  if kubectl wait \
      -n "$MONITOR_NAMESPACE" \
      --for=condition=Ready \
      "pod/${MONITOR_POD}" \
      --timeout=2m; then
    pass "${MONITOR_POD} is Ready"
  else
    fail "${MONITOR_POD} is not Ready"
  fi

  DELETION_TIMESTAMP="$(
    kubectl get pod "$MONITOR_POD" \
      -n "$MONITOR_NAMESPACE" \
      -o jsonpath='{.metadata.deletionTimestamp}' \
      2>/dev/null
  )"

  if [ -z "$DELETION_TIMESTAMP" ]; then
    pass "${MONITOR_POD} is not terminating"
  else
    fail "${MONITOR_POD} is terminating"
  fi
fi

section "9. Monitor health, readiness and canonical-only APIs"

if [ -n "${MONITOR_POD:-}" ]; then
  if kubectl exec -i \
      -n "$MONITOR_NAMESPACE" \
      "$MONITOR_POD" \
      -- python3 - <<'PY'
import json
import urllib.request

BASE = "http://127.0.0.1:8080"

def get_json(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return json.load(response)

health = get_json("/healthz")
ready = get_json("/readyz")
status = get_json("/api/status")
processes_payload = get_json("/api/processes")

print("healthz =", health)
print("readyz  =", ready)

assert health.get("status") == "ok", "/healthz is not ok"
assert ready.get("status") == "ready", "/readyz is not ready"

expected = {
    "gg-oracle-payments-01": {
        "type": "oracle",
        "role": "source",
        "services": {"adminsrvr", "distsrvr", "recvsrvr"},
    },
    "gg-postgresql-payments-01": {
        "type": "postgresql",
        "role": "target",
        "services": {"adminsrvr", "distsrvr", "recvsrvr"},
    },
}

seen = {}

for logical_pipeline in status.get("logicalPipelines", []):
    for runtime in logical_pipeline.get("runtimes", []):
        name = runtime.get("deploymentName")
        if name not in expected:
            continue

        critical = runtime.get("criticalServices") or {}
        seen[name] = runtime

        print(
            "runtime",
            name,
            "role=", runtime.get("role"),
            "type=", runtime.get("deploymentType"),
            "status=", runtime.get("effectiveStatus"),
            "fresh=", runtime.get("fresh"),
            "source=", runtime.get("dataSource"),
            "metricsEnabled=", runtime.get("metricsEnabled"),
            "alertsEnabled=", runtime.get("alertsEnabled"),
            "services=", critical,
            "processCount=", len(runtime.get("processes") or []),
        )

        assert runtime.get("role") == expected[name]["role"]
        assert runtime.get("deploymentType") == expected[name]["type"]
        assert runtime.get("effectiveStatus") == "UP"
        assert runtime.get("fresh") is True
        assert runtime.get("dataSource") == "canonical-monitor"
        assert runtime.get("metricsEnabled") is True
        assert runtime.get("alertsEnabled") is False
        assert set(critical) == expected[name]["services"]
        assert all(value is True for value in critical.values())

assert set(seen) == set(expected), (
    f"Canonical runtime set mismatch: expected={sorted(expected)}, "
    f"seen={sorted(seen)}"
)

serialized_status = json.dumps(status, sort_keys=True).lower()

for forbidden in (
    "legacy-observer-fallback",
    "gg-payments-ora-to-pg-001-source",
    "gg-payments-ora-to-pg-001-target",
    "legacy_fallback_enabled",
):
    assert forbidden not in serialized_status, (
        f"Legacy monitoring reference found: {forbidden}"
    )

process_deployments = {
    item.get("deploymentName"): item
    for item in processes_payload.get("deployments", [])
}

assert set(process_deployments) == set(expected), (
    "Process API canonical deployment set mismatch: "
    f"{sorted(process_deployments)}"
)

for name, item in sorted(process_deployments.items()):
    print(
        "process-api",
        name,
        "status=", item.get("effectiveStatus"),
        "processCount=", len(item.get("processes") or []),
        "services=", item.get("criticalServices"),
    )

print("Canonical-only monitor API validation passed.")
PY
  then
    pass "Monitor health, readiness and canonical-only API validation passed"
  else
    fail "Monitor health/readiness/API validation failed"
  fi
fi

section "10. Monitor error and retired-fallback log validation"

MONITOR_ERRORS="$(
  kubectl logs \
    -n "$MONITOR_NAMESPACE" \
    deployment/"$MONITOR_DEPLOYMENT" \
    --since=30m 2>&1 |
    grep -E \
      'cloudwatch_client_creation_failed|cloudwatch_put_metric_data_failed|tick failed|legacy-observer-fallback|LEGACY_FALLBACK_ENABLED' \
    || true
)"

if [ -z "$MONITOR_ERRORS" ]; then
  pass "No CloudWatch, polling or retired-fallback errors were found"
else
  fail "Monitor error or retired-fallback log entries were found"
  printf '%s\n' "$MONITOR_ERRORS"
fi

section "11. Legacy resources remain non-destructively retained"

if kubectl get application "$LEGACY_ARGO_APP" \
    -n "$ARGOCD_NAMESPACE" >/dev/null 2>&1; then
  pass "Legacy Argo CD Application remains present"
  kubectl get application "$LEGACY_ARGO_APP" \
    -n "$ARGOCD_NAMESPACE" \
    -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
else
  fail "Legacy Argo CD Application is unexpectedly absent"
fi

if kubectl get namespace "$LEGACY_NAMESPACE" >/dev/null 2>&1; then
  pass "Legacy namespace remains present"
  kubectl get pods,statefulsets,pvc \
    -n "$LEGACY_NAMESPACE" \
    -o wide || true
else
  fail "Legacy namespace is unexpectedly absent"
fi

section "12. Final result"

if [ "$FAILURES" -eq 0 ]; then
  printf '\nALL PHASE 5A LIVE VALIDATIONS PASSED\n'
  exit 0
fi

printf '\nPHASE 5A VALIDATION COMPLETED WITH %s FAILURE(S)\n' "$FAILURES"
exit 1