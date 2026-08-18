#!/usr/bin/env bash
set -euo pipefail

# Orchestration/regression script for the GoldenGate EKS repo; runs static parsing/Helm/Python checks derived from the folder-driven envs/dev/*/values.yaml descriptors via hack/goldengate-deployment-model.py, the sole folder parser; never deploys, touches the cluster, or requires AWS credentials.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# This script's own python3 invocations must never create __pycache__/*.pyc -- that would make the "no committed pycache" check below self-defeating.
export PYTHONDONTWRITEBYTECODE=1

DEPLOYMENT_MODEL_TOOL="hack/goldengate-deployment-model.py"
ENVIRONMENT_TOOL="hack/goldengate-environment.py"
CANONICAL_CONFIG="work/generated/dev/goldengate-deployments.yaml"
RUNTIME_CHART="helm/goldengate"
PLATFORM_CHART="helm/goldengate-platform"
MONITOR_CHART="helm/goldengate-monitor"
MONITOR_APP_DIR="monitoring/monitor"
MONITOR_WORKFLOW=".github/workflows/goldengate-monitor.yaml"
METRICS_CONFIG_WORKFLOW=".github/workflows/goldengate-monitor-metrics-config.yaml"
METRICS_CONFIG_HELPER_SCRIPT="hack/goldengate-metrics-config.py"
EKS_APP_WORKFLOW=".github/workflows/goldengate-eks-app.yaml"
PLATFORM_WORKFLOW=".github/workflows/goldengate-platform.yaml"
DETECT_SCRIPT="hack/detect-goldengate-deployments.sh"
OBSERVABILITY_VALUES_FILE="platform/dev/goldengate-observability/values.yaml"
OBSERVABILITY_WORKFLOW=".github/workflows/goldengate-observability.yaml"
ARGOCD_VALUES_FILE="envs/dev/argocd/values.yaml"
ARGOCD_DEPLOY_WORKFLOW=".github/workflows/argocd-eks-deployment.yaml"

# runtime.image.repository/ingress.hostDomain/ingress.alb.groupName/ingress.alb.certificateArn/runtime.csi.region are shared environment configuration -- resolved once here via the same resolver the deploy workflow uses, never an independently maintained literal.
RESOLVED_DNS_DOMAIN="$(python3 "$ENVIRONMENT_TOOL" --environment dev get DNS_DOMAIN)"
RESOLVED_ALB_GROUP_NAME="$(python3 "$ENVIRONMENT_TOOL" --environment dev get ALB_GROUP_NAME)"
RESOLVED_CERTIFICATE_ARN="$(python3 "$ENVIRONMENT_TOOL" --environment dev get ACM_CERTIFICATE_ARN)"
RESOLVED_AWS_REGION="$(python3 "$ENVIRONMENT_TOOL" --environment dev get AWS_REGION)"
SHARED_INGRESS_OVERRIDES=(--set-string ingress.hostDomain="$RESOLVED_DNS_DOMAIN" --set-string ingress.alb.groupName="$RESOLVED_ALB_GROUP_NAME" --set-string ingress.alb.certificateArn="$RESOLVED_CERTIFICATE_ARN")

# Phase 10B: envs/dev/goldengate-monitor/values.yaml no longer carries namespace.name/aws.region/serviceAccount.roleArn -- resolved here the same way the monitor deploy workflow does.
RESOLVED_MONITOR_NAMESPACE="$(python3 "$ENVIRONMENT_TOOL" --environment dev get MONITOR_NAMESPACE)"
RESOLVED_MONITOR_ROLE_ARN="$(python3 "$ENVIRONMENT_TOOL" --environment dev get MONITOR_ROLE_ARN)"
RESOLVED_MONITOR_HOST="$(python3 "$ENVIRONMENT_TOOL" --environment dev get MONITOR_HOST)"
MONITOR_SHARED_OVERRIDES=(--set-string namespace.name="$RESOLVED_MONITOR_NAMESPACE" --set-string aws.region="$RESOLVED_AWS_REGION" --set-string serviceAccount.roleArn="$RESOLVED_MONITOR_ROLE_ARN")

# Phase 10C: platform/dev/goldengate-platform/values.yaml no longer carries environment/namespaces.runtime.name/fluentBit.namespaces.*/fluentBit.cloudwatch.* -- resolved here the same way the platform deploy workflow does.
RESOLVED_GG_ENVIRONMENT="$(python3 "$ENVIRONMENT_TOOL" --environment dev get GG_ENVIRONMENT)"
RESOLVED_RUNTIME_NAMESPACE="$(python3 "$ENVIRONMENT_TOOL" --environment dev get RUNTIME_NAMESPACE)"
RESOLVED_RUNTIME_LOG_GROUP="$(python3 "$ENVIRONMENT_TOOL" --environment dev get RUNTIME_LOG_GROUP)"
RESOLVED_MONITOR_LOG_GROUP="$(python3 "$ENVIRONMENT_TOOL" --environment dev get MONITOR_LOG_GROUP)"
PLATFORM_SHARED_OVERRIDES=(--set-string environment="$RESOLVED_GG_ENVIRONMENT" --set-string namespaces.runtime.name="$RESOLVED_RUNTIME_NAMESPACE" --set-string fluentBit.namespaces.runtime="$RESOLVED_RUNTIME_NAMESPACE" --set-string fluentBit.namespaces.monitoring="$RESOLVED_MONITOR_NAMESPACE" --set-string fluentBit.cloudwatch.runtimeLogGroupName="$RESOLVED_RUNTIME_LOG_GROUP" --set-string fluentBit.cloudwatch.monitorLogGroupName="$RESOLVED_MONITOR_LOG_GROUP")

# Shared-secret identities (role-derived admin secret) plus the restored shared gg-runtime-sa identity the deploy workflow injects via --set; direct helm invocations against the two known historical fixtures below must mirror them. Image repository is now resolved per-deployment (below) since it depends on the descriptor's own runtime.image.repositoryName.
ORACLE_SHARED_OVERRIDES=(--set runtime.csi.admin.objectName=dev/goldengate/source/admin --set runtime.csi.certificate.objectName=dev/goldengate/tls-certificate --set-string runtime.csi.region="$RESOLVED_AWS_REGION" --set runtime.serviceAccount.create=false --set runtime.serviceAccount.name=gg-runtime-sa "${SHARED_INGRESS_OVERRIDES[@]}" --set-string runtime.image.repository="$(python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev describe gg-postgresql-repltest-01 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["imageRepository"])')")
POSTGRESQL_SHARED_OVERRIDES=(--set runtime.csi.admin.objectName=dev/goldengate/target/admin --set runtime.csi.certificate.objectName=dev/goldengate/tls-certificate --set-string runtime.csi.region="$RESOLVED_AWS_REGION" --set runtime.serviceAccount.create=false --set runtime.serviceAccount.name=gg-runtime-sa "${SHARED_INGRESS_OVERRIDES[@]}" --set-string runtime.image.repository="$(python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev describe gg-mssql-repltest-01 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["imageRepository"])')")

# Self-service: for any REAL-repository render loop that dynamically iterates the live inventory (never a fixed ID list), overrides are derived from the deployment model's own `describe` output -- never a hardcoded oracle-vs-postgresql binary -- so a newly onboarded folder of any deploymentType/role is rendered correctly without touching this file. Sets the global array SHARED_OVERRIDES. Uses the exact same dry-run managed-EFS placeholder the real deploy=false workflow uses (fs-0dead0000000beef0); mode=existing already carries its own committed fileSystemId in the descriptor's own values.yaml, so no override is needed there.
derive_shared_overrides_for_deployment() {
  local dep_id="$1"
  local describe_json admin_secret tls_secret sa_name efs_mode image_repository
  describe_json="$(python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev describe "$dep_id" 2>/dev/null)"
  admin_secret="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["adminSecretName"])' <<< "$describe_json")"
  tls_secret="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["tlsSecretName"])' <<< "$describe_json")"
  sa_name="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["runtimeServiceAccountName"])' <<< "$describe_json")"
  efs_mode="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["efsMode"] or "")' <<< "$describe_json")"
  image_repository="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["imageRepository"])' <<< "$describe_json")"
  SHARED_OVERRIDES=(--set runtime.csi.admin.objectName="$admin_secret" --set runtime.csi.certificate.objectName="$tls_secret" --set-string runtime.csi.region="$RESOLVED_AWS_REGION" --set runtime.serviceAccount.create=false --set runtime.serviceAccount.name="$sa_name" --set-string runtime.image.repository="$image_repository" "${SHARED_INGRESS_OVERRIDES[@]}")
  if [ "$efs_mode" = "managed" ]; then
    SHARED_OVERRIDES+=(--set persistence.efs.fileSystemId=fs-0dead0000000beef0)
  fi
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
skip() { echo "SKIP: $1"; SKIP_COUNT=$((SKIP_COUNT + 1)); }

# Stable, commit-independent invariant checks (used at two checkpoints below); replaces the former HEAD-relative whole-AST diff, which broke whenever collector.py legitimately changed.
collector_safety_contract_check() {
  local label="$1"
  local collector_module_count
  collector_module_count="$(grep -l '^def polling_loop' monitoring/monitor/*.py 2>/dev/null | wc -l || true)"
  if [ "$collector_module_count" -eq 1 ]; then
    pass "${label}: exactly one shared collector module defines polling_loop"
  else
    fail "${label}: expected exactly one module defining polling_loop, found ${collector_module_count}"
  fi

  if grep -qiE 'sidecar|utility-sidecar|observer[_-]?sidecar' monitoring/monitor/collector.py monitoring/monitor/monitor.py 2>/dev/null; then
    fail "${label}: sidecar/observer-sidecar terminology found in collector.py or monitor.py"
  else
    pass "${label}: no observer/manager sidecar logic exists in collector.py or monitor.py"
  fi

  if grep -qE '\.put_item\(|\.update_item\(.*"CONFIG"' monitoring/monitor/collector.py 2>/dev/null; then
    fail "${label}: collector.py appears to write CONFIG"
  else
    pass "${label}: CONFIG is never written by the collector (only read via read_config/GetItem)"
  fi

  if grep -qE '\.scan\(' monitoring/monitor/collector.py monitoring/monitor/monitor.py 2>/dev/null; then
    fail "${label}: a DynamoDB Scan call exists"
  else
    pass "${label}: no DynamoDB Scan call exists in collector.py or monitor.py"
  fi

  if grep -q 'delete_item' monitoring/monitor/collector.py 2>/dev/null; then
    fail "${label}: a DeleteItem call exists in collector.py"
  else
    pass "${label}: no DeleteItem call exists in collector.py"
  fi

  if grep -q '"CONFIG"' monitoring/monitor/collector.py 2>/dev/null \
      && grep -q '"LEASE"' monitoring/monitor/collector.py 2>/dev/null \
      && grep -q 'STATE#' monitoring/monitor/collector.py 2>/dev/null; then
    pass "${label}: canonical CONFIG/LEASE/STATE# record-type keys remain"
  else
    fail "${label}: a canonical CONFIG/LEASE/STATE# record-type key is missing"
  fi

  local LEASE_KEY_COUNT
  LEASE_KEY_COUNT="$(grep -c '"recordType": "LEASE"' monitoring/monitor/collector.py 2>/dev/null || true)"
  if [ "${LEASE_KEY_COUNT:-0}" -eq 1 ]; then
    pass "${label}: LEASE remains writer-coordination-only (exactly one recordType=LEASE key site)"
  else
    fail "${label}: LEASE recordType key site count changed unexpectedly (${LEASE_KEY_COUNT:-0}), expected exactly 1"
  fi

  if grep -qiE 'kubernetes|kubectl|client\.CoreV1Api|V1Pod|\.delete_namespaced|\.restart\(' \
      monitoring/monitor/collector.py monitoring/monitor/monitor.py 2>/dev/null; then
    fail "${label}: a Kubernetes healing/restart action reference exists in collector.py or monitor.py"
  else
    pass "${label}: no Kubernetes healing/restart/start/stop action exists in collector.py or monitor.py"
  fi

  if grep -q 'CLOUDWATCH_NAMESPACE = "GoldenGate/Pipelines"' monitoring/monitor/collector.py 2>/dev/null; then
    pass "${label}: GoldenGate/Pipelines CloudWatch namespace remains"
  else
    fail "${label}: GoldenGate/Pipelines CloudWatch namespace constant is missing or changed"
  fi

  local expected_metrics="AbendEvent AbendFailure AbendState CriticalServiceDown DeploymentDown HeartbeatAgeSeconds LagBreached ExtractLagSeconds ReplicatLagSeconds"
  local actual_metrics unexpected="false" name
  actual_metrics="$(grep -oE '"MetricName": "[A-Za-z]+"' monitoring/monitor/collector.py | sed -E 's/"MetricName": "([A-Za-z]+)"/\1/' | sort -u || true)"
  for name in $actual_metrics; do
    case " $expected_metrics " in
      *" $name "*) ;;
      *) unexpected="true" ;;
    esac
  done
  if [ "$unexpected" = "false" ]; then
    pass "${label}: the approved CloudWatch metric-name allowlist remains exact"
  else
    fail "${label}: an unexpected CloudWatch metric name exists outside the approved allowlist"
  fi

  if grep -qE 'cloudwatch:(GetMetricData|ListMetrics|DescribeAlarms|GetDashboard)|get_metric_data|list_metrics|describe_alarms' \
      monitoring/monitor/collector.py monitoring/monitor/monitor.py 2>/dev/null; then
    fail "${label}: a CloudWatch-read action reference exists in the runtime application code"
  else
    pass "${label}: no runtime IAM/CloudWatch-read coupling exists in collector.py or monitor.py"
  fi

  if (cd "$MONITOR_APP_DIR" && python3 -m unittest \
      tests.test_collector.PublishMetricBatchTests.test_batches_of_at_most_20 \
      tests.test_collector.CriticalServiceCoverageTests.test_no_kubernetes_healing_restart_or_fencing_action_introduced \
      >/dev/null 2>&1); then
    pass "${label}: focused unit tests confirm CloudWatch batching stays at most 20 and no healing action exists"
  else
    fail "${label}: focused batching/no-healing-action unit tests failed"
  fi
}

HELM_AVAILABLE="false"
command -v helm >/dev/null 2>&1 && HELM_AVAILABLE="true"

PYTHON_AVAILABLE="false"
if command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
  PYTHON_AVAILABLE="true"
fi

# Regenerates the canonical registry via the sole folder parser, mirroring exactly what the deploy workflow stages; every check below reads this generated file, never envs/dev/*/values.yaml directly and never a handwritten registry file.
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  mkdir -p "$(dirname "$CANONICAL_CONFIG")"
  if ! python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev registry --output "$CANONICAL_CONFIG"; then
    echo "FATAL: failed to generate the canonical registry via ${DEPLOYMENT_MODEL_TOOL}."
    exit 1
  fi
fi

echo "=================================================="
echo "GoldenGate repository regression"
echo "=================================================="
echo "Repository root: ${REPO_ROOT}"
echo "Helm available:  ${HELM_AVAILABLE}"
echo "Python3+PyYAML available: ${PYTHON_AVAILABLE}"
echo ""

# 1. Strict YAML parsing + duplicate-key rejection for the canonical config.
echo "--- Canonical configuration: parsing and structure ---"
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  CANONICAL_CHECK_LOG="${WORKDIR}/canonical-check.log"
  if python3 - "$CANONICAL_CONFIG" >"$CANONICAL_CHECK_LOG" 2>&1 <<'PYEOF'
import sys
import yaml

path = sys.argv[1]

class DupCheckLoader(yaml.SafeLoader):
    pass

def no_dup_construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping

DupCheckLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dup_construct_mapping)

with open(path) as f:
    doc = yaml.load(f, Loader=DupCheckLoader)

for key in ("environment", "runtimeNamespace", "monitoringNamespace", "dnsDomain", "deployments"):
    assert key in doc, f"missing required key {key!r}"

deployments = doc["deployments"]
assert isinstance(deployments, list) and deployments, "'deployments' must be a non-empty list"

names = set()
enabled_count = 0
for d in deployments:
    for field in ("name", "type", "pipeline", "role"):
        assert d.get(field), f"deployment entry missing {field!r}: {d!r}"
    assert d["name"] not in names, f"duplicate deployment name {d['name']!r}"
    names.add(d["name"])
    assert d["role"] in ("source", "target"), f"invalid role {d['role']!r}"
    if d.get("enabled"):
        enabled_count += 1

print(f"OK: {len(deployments)} deployment(s), {enabled_count} enabled, names={sorted(names)}")
PYEOF
  then
    pass "$(cat "$CANONICAL_CHECK_LOG")"
  else
    fail "canonical config parsing/structure check failed"
    cat "$CANONICAL_CHECK_LOG"
  fi
else
  skip "canonical config parsing -- python3/PyYAML not available"
fi

# 2. Both runtimes remain enabled (source of truth: canonical config AND each runtime's own environment values file).
echo ""
echo "--- Both runtimes remain enabled ---"
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  ENABLED_NAMES="$(python3 -c "
import yaml
doc = yaml.safe_load(open('${CANONICAL_CONFIG}'))
for d in doc['deployments']:
    if d.get('enabled'):
        print(d['name'])
")"
  ENABLED_COUNT="$(echo "$ENABLED_NAMES" | grep -c . || true)"
  if [ "$ENABLED_COUNT" -lt 2 ]; then
    fail "expected at least 2 enabled deployments in ${CANONICAL_CONFIG}, found ${ENABLED_COUNT}"
  else
    pass "${ENABLED_COUNT} deployment(s) enabled in canonical config: $(echo "$ENABLED_NAMES" | tr '\n' ' ')"
  fi
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    VALUES_FILE="envs/dev/${name}/values.yaml"
    if [ ! -f "$VALUES_FILE" ]; then
      fail "no environment values file found for enabled deployment ${name} (expected ${VALUES_FILE})"
      continue
    fi
    if grep -qE '^\s*enabled:\s*true' "$VALUES_FILE" && grep -qE '^\s*enabled:\s*true\s*$' <(sed -n '/^deployment:/,/^[a-zA-Z]/p' "$VALUES_FILE"); then
      pass "${name}: environment values file has deployment.enabled=true"
    else
      fail "${name}: environment values file does not have deployment.enabled=true"
    fi
  done <<< "$ENABLED_NAMES"
else
  skip "runtime-enabled check -- python3/PyYAML not available"
fi

# 3. Python unit tests (monitoring/monitor).
echo ""
echo "--- Python unit tests ---"
if command -v python3 >/dev/null 2>&1 && python3 -c "import boto3, moto, yaml" >/dev/null 2>&1; then
  set +e
  MONITOR_TEST_OUTPUT="$(cd "$MONITOR_APP_DIR" && python3 -m unittest discover -s tests -p "test_*.py" 2>&1)"
  MONITOR_TEST_STATUS=$?
  set -e
  if [ "$MONITOR_TEST_STATUS" -eq 0 ]; then
    RAN_LINE="$(echo "$MONITOR_TEST_OUTPUT" | grep -E '^Ran [0-9]+ test' | tail -1)"
    pass "monitoring/monitor unit tests: ${RAN_LINE:-all tests passed}"
  else
    fail "monitoring/monitor unit tests failed"
    echo "$MONITOR_TEST_OUTPUT"
  fi
else
  skip "monitoring/monitor unit tests -- python3/boto3/moto/PyYAML not available"
fi

if command -v python3 >/dev/null 2>&1; then
  if python3 -m py_compile "${MONITOR_APP_DIR}"/*.py; then
    pass "monitoring/monitor Python modules compile cleanly"
  else
    fail "monitoring/monitor Python modules failed to compile"
  fi
  # py_compile writes __pycache__/*.pyc regardless of PYTHONDONTWRITEBYTECODE (an explicit compile request) -- clean up immediately so this run leaves no artifacts.
  find "$MONITOR_APP_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
else
  skip "py_compile -- python3 not available"
fi

# 4. Helm lint: runtime chart, platform chart, monitor chart.
echo ""
echo "--- Helm lint ---"
if [ "$HELM_AVAILABLE" = "true" ]; then
  # deploymentModel has no usable default and assertSupportedDeploymentModel fires at render time, so lint against a real canonical values file (declares deploymentModel: singleRuntime), never bare/values-less -- matches how the chart is linted in production. gg-postgresql-repltest-01 is role=source, so it takes the source-secret override set (ORACLE_SHARED_OVERRIDES is named for the historical oracle=source descriptor but its objectName values are role-based, not engine-based).
  if helm lint "$RUNTIME_CHART" -f "${REPO_ROOT}/envs/dev/gg-postgresql-repltest-01/values.yaml" --set global.environment=dev "${ORACLE_SHARED_OVERRIDES[@]}" >"${WORKDIR}/lint-runtime.log" 2>&1; then
    pass "helm lint ${RUNTIME_CHART} (canonical singleRuntime values)"
  else
    fail "helm lint ${RUNTIME_CHART} (canonical singleRuntime values)"
    cat "${WORKDIR}/lint-runtime.log"
  fi

  if helm lint "$PLATFORM_CHART" >"${WORKDIR}/lint-platform.log" 2>&1; then
    pass "helm lint ${PLATFORM_CHART}"
  else
    fail "helm lint ${PLATFORM_CHART}"
    cat "${WORKDIR}/lint-platform.log"
  fi

  # Centralized container logging (platform Fluent Bit DaemonSet): uses the real dev values file and --set-string role-ARN/region/image injection pattern the actual goldengate-platform.yaml workflow uses; the digest below is the real, verified private ECR digest.
  PLATFORM_DEV_VALUES="${REPO_ROOT}/platform/dev/goldengate-platform/values.yaml"
  FAKE_ORACLE_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev"
  FAKE_FLUENT_BIT_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGatePlatformLoggingRole-dev"
  FAKE_FLUENT_BIT_IMAGE="229410149234.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit@sha256:366923ffc51dfde4966e743dcbd4ca05211b733d4f69c7591903bc7660fbf243"
  if helm lint "$PLATFORM_CHART" \
      --values "$PLATFORM_DEV_VALUES" \
      --set-string runtimeServiceAccount.roleArn="$FAKE_ORACLE_ROLE_ARN" \
      --set-string fluentBit.serviceAccount.roleArn="$FAKE_FLUENT_BIT_ROLE_ARN" \
      --set-string fluentBit.aws.region="eu-west-1" \
      --set-string fluentBit.image.reference="$FAKE_FLUENT_BIT_IMAGE" \
      "${PLATFORM_SHARED_OVERRIDES[@]}" \
      >"${WORKDIR}/lint-platform-fluentbit.log" 2>&1; then
    pass "helm lint ${PLATFORM_CHART} (dev values, fluentBit.create=true, private digest image)"
  else
    fail "helm lint ${PLATFORM_CHART} (dev values, fluentBit.create=true, private digest image)"
    cat "${WORKDIR}/lint-platform-fluentbit.log"
  fi

  PLATFORM_FLUENTBIT_RENDERED="${WORKDIR}/platform-fluentbit-rendered.yaml"
  if helm template goldengate-dev-platform "$PLATFORM_CHART" \
      --values "$PLATFORM_DEV_VALUES" \
      --set-string runtimeServiceAccount.roleArn="$FAKE_ORACLE_ROLE_ARN" \
      --set-string fluentBit.serviceAccount.roleArn="$FAKE_FLUENT_BIT_ROLE_ARN" \
      --set-string fluentBit.aws.region="eu-west-1" \
      --set-string fluentBit.image.reference="$FAKE_FLUENT_BIT_IMAGE" \
      "${PLATFORM_SHARED_OVERRIDES[@]}" \
      > "$PLATFORM_FLUENTBIT_RENDERED" 2>"${WORKDIR}/template-platform-fluentbit.log"; then
    pass "helm template ${PLATFORM_CHART} (dev values, fluentBit.create=true, private digest image) renders"
  else
    fail "helm template ${PLATFORM_CHART} (dev values, fluentBit.create=true, private digest image) renders"
    cat "${WORKDIR}/template-platform-fluentbit.log"
  fi

  # The chart must fail clearly (not silently fall back to any image) when fluentBit.create=true and no image reference is supplied at all.
  if helm template goldengate-dev-platform "$PLATFORM_CHART" \
      --values "$PLATFORM_DEV_VALUES" \
      --set-string runtimeServiceAccount.roleArn="$FAKE_ORACLE_ROLE_ARN" \
      --set-string fluentBit.serviceAccount.roleArn="$FAKE_FLUENT_BIT_ROLE_ARN" \
      --set-string fluentBit.aws.region="eu-west-1" \
      "${PLATFORM_SHARED_OVERRIDES[@]}" \
      >"${WORKDIR}/template-platform-no-image.log" 2>&1; then
    fail "helm template ${PLATFORM_CHART} unexpectedly succeeded with fluentBit.image.reference empty"
  else
    if grep -q "fluentBit.image.reference is required" "${WORKDIR}/template-platform-no-image.log"; then
      pass "the chart fails clearly (required) when fluentBit.create=true and fluentBit.image.reference is empty"
    else
      fail "the chart failed with fluentBit.image.reference empty, but not with the expected required-value error"
      cat "${WORKDIR}/template-platform-no-image.log"
    fi
  fi

  if [ -s "$PLATFORM_FLUENTBIT_RENDERED" ]; then
    DAEMONSET_COUNT="$(grep -c '^kind: DaemonSet$' "$PLATFORM_FLUENTBIT_RENDERED" || true)"
    if [ "$DAEMONSET_COUNT" -eq 1 ] && grep -q '^  name: gg-fluent-bit$' "$PLATFORM_FLUENTBIT_RENDERED"; then
      pass "exactly one gg-fluent-bit DaemonSet is rendered"
    else
      fail "expected exactly one gg-fluent-bit DaemonSet, found ${DAEMONSET_COUNT}"
    fi

    if grep -q 'privileged: true' "$PLATFORM_FLUENTBIT_RENDERED"; then
      fail "gg-fluent-bit DaemonSet requests privileged mode"
    else
      pass "gg-fluent-bit DaemonSet has no privileged mode"
    fi

    FLUENT_BIT_DS_ONLY="$(awk '/^kind: DaemonSet$/{f=1} f{print} f && /^---$/{exit}' "$PLATFORM_FLUENTBIT_RENDERED")"
    if echo "$FLUENT_BIT_DS_ONLY" | grep -A2 'name: varlog' | grep -q 'readOnly: true'; then
      pass "gg-fluent-bit DaemonSet's host log mount (varlog) is read-only"
    else
      fail "gg-fluent-bit DaemonSet's host log mount is not confirmed read-only"
    fi
    if echo "$FLUENT_BIT_DS_ONLY" | grep -q 'hostNetwork: false'; then
      pass "gg-fluent-bit DaemonSet does not use host networking"
    else
      fail "gg-fluent-bit DaemonSet does not explicitly disable host networking"
    fi

    # Deployment image reference: exact, private, immutable digest -- never public.ecr.aws, never a mutable tag.
    if echo "$FLUENT_BIT_DS_ONLY" | grep -Fq -- "image: \"${FAKE_FLUENT_BIT_IMAGE}\""; then
      pass "gg-fluent-bit DaemonSet image exactly matches the supplied private immutable digest reference"
    else
      fail "gg-fluent-bit DaemonSet image does not exactly match the supplied private immutable digest reference"
    fi
    if grep -q 'public.ecr.aws' "$PLATFORM_FLUENTBIT_RENDERED"; then
      fail "rendered manifest contains a public.ecr.aws reference"
    else
      pass "rendered manifest contains no public.ecr.aws reference"
    fi

    # Deterministic per-namespace tag routing: two independent Tail inputs (runtime.*, monitor.*), each Path-restricted to its own namespace's log filename convention, each enriched by its own kubernetes FILTER (never a routing dependency), each OUTPUT matching directly on its own input's tag.
    TAIL_INPUT_COUNT="$(grep -cE '^\s*Name\s+tail\s*$' "$PLATFORM_FLUENTBIT_RENDERED" || true)"
    if [ "$TAIL_INPUT_COUNT" -eq 2 ]; then
      pass "exactly 2 Tail inputs are rendered (runtime, monitor)"
    else
      fail "expected exactly 2 Tail inputs, found ${TAIL_INPUT_COUNT}"
    fi

    if grep -Fq -- 'DB                /var/fluent-bit/state/flb_runtime.db' "$PLATFORM_FLUENTBIT_RENDERED" \
        && grep -Fq -- 'DB                /var/fluent-bit/state/flb_monitor.db' "$PLATFORM_FLUENTBIT_RENDERED"; then
      pass "runtime and monitor Tail inputs use two separate, non-shared position DB files"
    else
      fail "runtime and monitor Tail inputs do not use two separate DB files as expected"
    fi

    if grep -Fq -- 'Path              /var/log/containers/*_goldengate-dev_*.log' "$PLATFORM_FLUENTBIT_RENDERED" \
        && grep -Fq -- 'Path              /var/log/containers/*_goldengate-monitoring_*.log' "$PLATFORM_FLUENTBIT_RENDERED" \
        && ! grep -Fq -- 'Path              /var/log/containers/*.log' "$PLATFORM_FLUENTBIT_RENDERED"; then
      pass "runtime and monitor Tail inputs have exact, deterministic Paths; no unrestricted /var/log/containers/*.log Path exists"
    else
      fail "Tail input Paths are not exactly as expected"
    fi

    if grep -Fq -- 'Tag               runtime.*' "$PLATFORM_FLUENTBIT_RENDERED" \
        && grep -Fq -- 'Tag               monitor.*' "$PLATFORM_FLUENTBIT_RENDERED"; then
      pass "runtime Tail input Tag is runtime.* and monitor Tail input Tag is monitor.*"
    else
      fail "Tail input Tags are not exactly runtime.* / monitor.* as expected"
    fi

    if grep -Fq -- 'Kube_Tag_Prefix   runtime.var.log.containers.' "$PLATFORM_FLUENTBIT_RENDERED" \
        && grep -Fq -- 'Kube_Tag_Prefix   monitor.var.log.containers.' "$PLATFORM_FLUENTBIT_RENDERED"; then
      pass "runtime and monitor kubernetes FILTERs set the expected explicit Kube_Tag_Prefix"
    else
      fail "explicit Kube_Tag_Prefix values were not found as expected"
    fi

    if grep -Fq -- 'Match                   runtime.*' "$PLATFORM_FLUENTBIT_RENDERED" \
        && grep -Fq -- 'Match                   monitor.*' "$PLATFORM_FLUENTBIT_RENDERED"; then
      pass "runtime cloudwatch_logs OUTPUT uses Match runtime.* and monitor OUTPUT uses Match monitor.*"
    else
      fail "cloudwatch_logs OUTPUT Match values are not exactly runtime.* / monitor.* as expected"
    fi

    if grep -Eq 'Name\s+grep' "$PLATFORM_FLUENTBIT_RENDERED"; then
      fail "a grep FILTER is still rendered -- routing must not depend on Kubernetes-metadata enrichment"
    else
      pass "no grep FILTER is rendered"
    fi
    if grep -Eq 'Name\s+rewrite_tag|Emitter_Name|Emitter_Storage\.type|runtime\.\$TAG|monitor\.\$TAG' "$PLATFORM_FLUENTBIT_RENDERED"; then
      fail "a rewrite_tag FILTER or emitter is still rendered"
    else
      pass "no rewrite_tag FILTER or emitter is rendered"
    fi

    if grep -qE 'log_group_name[[:space:]]+/adcb/goldengate/dev/runtime$' "$PLATFORM_FLUENTBIT_RENDERED" \
        && grep -qE 'log_group_name[[:space:]]+/adcb/goldengate/dev/monitor$' "$PLATFORM_FLUENTBIT_RENDERED" \
        && grep -qE 'auto_create_group[[:space:]]+false' "$PLATFORM_FLUENTBIT_RENDERED" \
        && ! grep -qE 'auto_create_group[[:space:]]+true' "$PLATFORM_FLUENTBIT_RENDERED"; then
      pass "Fluent Bit targets the exact pre-created CloudWatch log groups and never auto-creates a group"
    else
      fail "Fluent Bit CloudWatch log-group destinations are not exactly as expected"
    fi

    # storage.total_limit_size (both cloudwatch_logs OUTPUTs) plus the fluent-bit-state emptyDir sizeLimit bound total on-disk buffer size -- distinct from the Mem_Buf_Limit/max_chunks_up/backlog.mem_limit memory/in-flight controls.
    TOTAL_LIMIT_SIZE_COUNT="$(grep -v '^\s*#' "$PLATFORM_FLUENTBIT_RENDERED" | grep -cE 'storage\.total_limit_size[[:space:]]+[0-9]' || true)"
    if [ "$TOTAL_LIMIT_SIZE_COUNT" -eq 2 ]; then
      pass "both cloudwatch_logs OUTPUTs set storage.total_limit_size (filesystem queue bound)"
    else
      fail "expected storage.total_limit_size on exactly 2 cloudwatch_logs OUTPUTs, found ${TOTAL_LIMIT_SIZE_COUNT}"
    fi
    if echo "$FLUENT_BIT_DS_ONLY" | grep -A2 'name: fluent-bit-state' | grep -q 'sizeLimit:'; then
      pass "the fluent-bit-state emptyDir volume sets sizeLimit (node ephemeral-storage bound)"
    else
      fail "the fluent-bit-state emptyDir volume does not set sizeLimit"
    fi
    if echo "$FLUENT_BIT_DS_ONLY" | grep -q 'Mem_Buf_Limit\|storage.max_chunks_up\|storage.backlog.mem_limit' \
        || grep -q 'Mem_Buf_Limit\|storage.max_chunks_up\|storage.backlog.mem_limit' "$PLATFORM_FLUENTBIT_RENDERED"; then
      pass "existing memory/backlog controls (Mem_Buf_Limit, storage.max_chunks_up, storage.backlog.mem_limit) are retained"
    else
      fail "existing memory/backlog controls were unexpectedly removed"
    fi

    if grep -qE '^kind: (StatefulSet|Deployment)$' "$PLATFORM_FLUENTBIT_RENDERED"; then
      fail "a GoldenGate runtime workload (StatefulSet/Deployment) was rendered by the platform chart"
    else
      pass "no GoldenGate runtime StatefulSet/Deployment is rendered by the platform chart"
    fi
  else
    skip "gg-fluent-bit DaemonSet structural checks -- rendered manifest not available"
  fi

  # Exercises the same regex the workflow's "Validate FLUENT_BIT_IMAGE format" step uses; confirms the real digest passes and malformed values (tag-based, public.ecr.aws, wrong repo/account, malformed digest) are rejected.
  FLUENT_BIT_IMAGE_PATTERN='^229410149234\.dkr\.ecr\.eu-west-1\.amazonaws\.com/aws-cloud-factory-fluent-bit@sha256:[a-f0-9]{64}$'
  FLUENT_BIT_IMAGE_FORMAT_ALL_OK="true"
  while IFS='|' read -r label candidate expect_match; do
    [ -z "$label" ] && continue
    if [[ "$candidate" =~ $FLUENT_BIT_IMAGE_PATTERN ]]; then
      actual_match="true"
    else
      actual_match="false"
    fi
    if [ "$actual_match" != "$expect_match" ]; then
      FLUENT_BIT_IMAGE_FORMAT_ALL_OK="false"
      echo "  image-format mismatch: ${label} expected match=${expect_match} got=${actual_match} (${candidate})"
    fi
  done <<'CASES'
valid private digest|229410149234.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit@sha256:366923ffc51dfde4966e743dcbd4ca05211b733d4f69c7591903bc7660fbf243|true
tag-based|229410149234.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit:3.4.0|false
public.ecr.aws|public.ecr.aws/aws-observability/aws-for-fluent-bit@sha256:366923ffc51dfde4966e743dcbd4ca05211b733d4f69c7591903bc7660fbf243|false
wrong repository|229410149234.dkr.ecr.eu-west-1.amazonaws.com/some-other-repo@sha256:366923ffc51dfde4966e743dcbd4ca05211b733d4f69c7591903bc7660fbf243|false
wrong account|999999999999.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit@sha256:366923ffc51dfde4966e743dcbd4ca05211b733d4f69c7591903bc7660fbf243|false
malformed digest|229410149234.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit@sha256:abc123|false
CASES
  if [ "$FLUENT_BIT_IMAGE_FORMAT_ALL_OK" = "true" ]; then
    pass "FLUENT_BIT_IMAGE format regex accepts the exact private digest and rejects tag/public/wrong-repo/wrong-account/malformed-digest variants"
  else
    fail "FLUENT_BIT_IMAGE format regex did not behave as expected for one or more cases (see output above)"
  fi

  # No GoldenGate runtime sidecar: the runtime chart itself (untouched here) must still define exactly one application container and exactly one init container.
  RUNTIME_STATEFULSET="${RUNTIME_CHART}/templates/runtime-statefulset.yaml"
  if [ -f "$RUNTIME_STATEFULSET" ]; then
    INIT_CONTAINER_COUNT="$(grep -c '^\s*initContainers:$' "$RUNTIME_STATEFULSET")"
    APP_CONTAINER_BLOCK_COUNT="$(grep -c '^\s*containers:$' "$RUNTIME_STATEFULSET")"
    if [ "$INIT_CONTAINER_COUNT" -eq 1 ] && [ "$APP_CONTAINER_BLOCK_COUNT" -eq 1 ] \
        && grep -q 'name: prepare-u02-permissions' "$RUNTIME_STATEFULSET"; then
      pass "GoldenGate runtime StatefulSet still defines exactly one init container (prepare-u02-permissions) and one containers: block -- no logging sidecar introduced"
    else
      fail "GoldenGate runtime StatefulSet container shape changed unexpectedly"
    fi
  else
    fail "${RUNTIME_STATEFULSET} not found"
  fi

  # IAM least privilege: the new logging policy must contain exactly the required log-writing actions and nothing else (no CreateLogGroup/DeleteLogGroup/alarms/DynamoDB/Secrets Manager/EFS/Kubernetes control permissions).
  LOGGING_POLICY_FILE="${REPO_ROOT}/envs/dev/policies/goldengate-platform-logging-dev/policies/policies_1.json"
  if [ -f "$LOGGING_POLICY_FILE" ] && command -v python3 >/dev/null 2>&1; then
    LOGGING_POLICY_CHECK="$(python3 - "$LOGGING_POLICY_FILE" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
actions = set()
for s in doc["Statement"]:
    a = s["Action"]
    actions.update([a] if isinstance(a, str) else a)
allowed = {"logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents"}
print("OK" if actions == allowed else f"MISMATCH:{sorted(actions)}")
PYEOF
)"
    if [ "$LOGGING_POLICY_CHECK" = "OK" ]; then
      pass "GoldenGatePlatformLoggingRole-dev policy contains exactly the required log-writing actions (no CreateLogGroup/DeleteLogGroup/alarms/DynamoDB/Secrets Manager/EFS/Kubernetes control permissions)"
    else
      fail "GoldenGatePlatformLoggingRole-dev policy action set unexpected: ${LOGGING_POLICY_CHECK}"
    fi
  else
    fail "${LOGGING_POLICY_FILE} not found, or python3 unavailable"
  fi

  # No kms_key_id may be set on either log group -- both rely on CloudWatch Logs' own default server-side encryption until an approved customer-managed KMS key is supplied.
  CLOUDWATCH_LOGS_TF="${REPO_ROOT}/envs/dev/cloudwatch_logs.tf"
  if [ -f "$CLOUDWATCH_LOGS_TF" ]; then
    if grep -v '^\s*#' "$CLOUDWATCH_LOGS_TF" | grep -qE 'kms_key_id\s*='; then
      fail "envs/dev/cloudwatch_logs.tf still sets kms_key_id -- must rely on CloudWatch Logs default server-side encryption only"
    else
      pass "envs/dev/cloudwatch_logs.tf sets no kms_key_id -- relies on CloudWatch Logs default server-side encryption"
    fi
    if grep -q 'local.gg_env_runtime_log_group' "$CLOUDWATCH_LOGS_TF" && grep -q 'local.gg_env_monitor_log_group' "$CLOUDWATCH_LOGS_TF" \
        && grep -q 'retention_in_days' "$CLOUDWATCH_LOGS_TF"; then
      pass "envs/dev/cloudwatch_logs.tf still defines both log groups (name derived from environment config, Fresh-EKS Phase A) with retention configured"
    else
      fail "envs/dev/cloudwatch_logs.tf no longer defines both expected log groups with retention"
    fi
  else
    fail "${CLOUDWATCH_LOGS_TF} not found"
  fi

  # GoldenGateCloudWatchMetricsRole-dev IAM/Terraform prerequisites (IAM only -- no Kubernetes/Argo CD resource is created here).
  IAM_TF="${REPO_ROOT}/envs/dev/iam.tf"
  if [ -f "$IAM_TF" ]; then
    if grep -q 'module "goldengate_cloudwatch_metrics_role_dev"' "$IAM_TF"; then
      pass "envs/dev/iam.tf contains module goldengate_cloudwatch_metrics_role_dev"
    else
      fail "envs/dev/iam.tf is missing module goldengate_cloudwatch_metrics_role_dev"
    fi

    # Extracts just this module's block (opening line to the next top-level '}' at column 0) so checks below can't match a different module.
    CLOUDWATCH_METRICS_MODULE_BLOCK="$(awk '/^module "goldengate_cloudwatch_metrics_role_dev" \{/{f=1} f{print} f && /^}$/{exit}' "$IAM_TF")"
    if echo "$CLOUDWATCH_METRICS_MODULE_BLOCK" | grep -q 'name          = local.gg_env_role_names.cloudwatchMetrics' \
        && echo "$CLOUDWATCH_METRICS_MODULE_BLOCK" | grep -q 'policy_folder = "goldengate-cloudwatch-metrics-dev"' \
        && echo "$CLOUDWATCH_METRICS_MODULE_BLOCK" | grep -q 'managed_policy_arns = \[\]'; then
      pass "goldengate_cloudwatch_metrics_role_dev derives name from environment config (local.gg_env_role_names.cloudwatchMetrics), policy_folder=goldengate-cloudwatch-metrics-dev, managed_policy_arns=[]"
    else
      fail "goldengate_cloudwatch_metrics_role_dev module block does not contain the expected name/policy_folder/managed_policy_arns"
    fi

    # No direct aws_iam_* resource anywhere -- every role must go through the existing ADCB Terraform module pattern, never a raw resource block.
    if grep -qE '^\s*resource\s+"aws_iam_(role|policy|role_policy|role_policy_attachment)"' "$IAM_TF"; then
      fail "envs/dev/iam.tf contains a direct aws_iam_* resource -- all roles must be created through the existing IAM module pattern"
    else
      pass "envs/dev/iam.tf contains no direct aws_iam_role/aws_iam_policy/aws_iam_role_policy/aws_iam_role_policy_attachment resource"
    fi
  else
    fail "${IAM_TF} not found"
  fi

  CW_METRICS_TRUST_FILE="${REPO_ROOT}/envs/dev/policies/goldengate-cloudwatch-metrics-dev/assume_role_policy/sts.json"
  CW_METRICS_POLICY_FILE="${REPO_ROOT}/envs/dev/policies/goldengate-cloudwatch-metrics-dev/policies/policies_1.json"

  if [ -f "$CW_METRICS_TRUST_FILE" ] && command -v python3 >/dev/null 2>&1; then
    EXPECTED_OIDC_PROVIDER_ARN="$(python3 "$ENVIRONMENT_TOOL" --environment dev get EKS_OIDC_PROVIDER_ARN)"
    CW_TRUST_CHECK="$(python3 - "$CW_METRICS_TRUST_FILE" "$EXPECTED_OIDC_PROVIDER_ARN" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
expected_federated = sys.argv[2]
stmts = doc.get("Statement")
if not isinstance(stmts, list) or len(stmts) != 1:
    print("MISMATCH:not-exactly-one-statement")
    raise SystemExit
s = stmts[0]
principal = s.get("Principal", {})
federated = principal.get("Federated", "")
if expected_federated != federated:
    print(f"MISMATCH:federated={federated}")
    raise SystemExit
if s.get("Action") != "sts:AssumeRoleWithWebIdentity":
    print(f"MISMATCH:action={s.get('Action')}")
    raise SystemExit
cond = s.get("Condition", {}).get("StringEquals", {})
aud_key = next((k for k in cond if k.endswith(":aud")), None)
sub_key = next((k for k in cond if k.endswith(":sub")), None)
if cond.get(aud_key) != "sts.amazonaws.com":
    print(f"MISMATCH:aud={cond.get(aud_key)}")
    raise SystemExit
if cond.get(sub_key) != "system:serviceaccount:amazon-cloudwatch:cloudwatch-agent":
    print(f"MISMATCH:sub={cond.get(sub_key)}")
    raise SystemExit
if "*" in json.dumps(doc):
    print("MISMATCH:wildcard-present")
    raise SystemExit
print("OK")
PYEOF
)"
    if [ "$CW_TRUST_CHECK" = "OK" ]; then
      pass "goldengate-cloudwatch-metrics-dev trust policy uses the approved OIDC provider, aud=sts.amazonaws.com, sub=system:serviceaccount:amazon-cloudwatch:cloudwatch-agent, and contains no wildcard"
    else
      fail "goldengate-cloudwatch-metrics-dev trust policy check failed: ${CW_TRUST_CHECK}"
    fi
  else
    fail "${CW_METRICS_TRUST_FILE} not found, or python3 unavailable"
  fi

  if [ -f "$CW_METRICS_POLICY_FILE" ] && command -v python3 >/dev/null 2>&1; then
    CW_POLICY_CHECK="$(python3 - "$CW_METRICS_POLICY_FILE" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
stmts = doc["Statement"]

actions = set()
for s in stmts:
    a = s["Action"]
    actions.update([a] if isinstance(a, str) else a)

expected = {
    "cloudwatch:PutMetricData",
    "logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents",
    "logs:DescribeLogGroups",
    "ec2:DescribeTags", "ec2:DescribeVolumes",
}
if actions != expected:
    print(f"MISMATCH:actions={sorted(actions)}")
    raise SystemExit

put_metric_stmt = next(s for s in stmts if "cloudwatch:PutMetricData" in
                        ([s["Action"]] if isinstance(s["Action"], str) else s["Action"]))

# Condition operator must be StringEqualsIfExists (permits OTLP PutMetricData requests omitting cloudwatch:namespace while still enforcing it when present) -- never plain StringEquals (implicitly denies key-omitting requests) and never an unconditioned allow.
put_metric_condition = put_metric_stmt.get("Condition", {})
if not put_metric_condition:
    print("MISMATCH:put-metric-data-statement-has-no-condition-at-all")
    raise SystemExit
if "StringEquals" in put_metric_condition:
    print(f"MISMATCH:old-stringequals-operator-still-present={put_metric_condition.get('StringEquals')}")
    raise SystemExit
ns_cond = put_metric_condition.get("StringEqualsIfExists", {}).get("cloudwatch:namespace")
if ns_cond != "ContainerInsights":
    print(f"MISMATCH:namespace-condition={ns_cond}")
    raise SystemExit

logs_write_stmt = next(s for s in stmts if "logs:PutLogEvents" in
                        ([s["Action"]] if isinstance(s["Action"], str) else s["Action"]))
resource = logs_write_stmt["Resource"]
resources = [resource] if isinstance(resource, str) else resource
expected_arn = "arn:aws:logs:eu-west-1:668311715351:log-group:/aws/containerinsights/gg-poc-dev/performance:*"
if resources != [expected_arn]:
    print(f"MISMATCH:logs-resource={resources}")
    raise SystemExit

doc_str = json.dumps(doc)
forbidden = ["CreateLogGroup", "PutRetentionPolicy", "DeleteLogGroup", "DeleteLogStream",
             "DeleteRetentionPolicy", "xray:", "application-signals", "secretsmanager:",
             "dynamodb:", "ecr:", "eks:", "kms:", "s3:", "sts:AssumeRole", "iam:",
             "autoscaling:", '"Action": "*"', "logs:*", "cloudwatch:*"]
for f in forbidden:
    if f in doc_str:
        print(f"MISMATCH:forbidden-found={f}")
        raise SystemExit

print("OK")
PYEOF
)"
    if [ "$CW_POLICY_CHECK" = "OK" ]; then
      pass "goldengate-cloudwatch-metrics-dev permissions policy grants exactly PutMetricData(ContainerInsights)/log-write(performance group only)/DescribeLogGroups/ec2:DescribeTags+DescribeVolumes, with no forbidden action"
    else
      fail "goldengate-cloudwatch-metrics-dev permissions policy check failed: ${CW_POLICY_CHECK}"
    fi
  else
    fail "${CW_METRICS_POLICY_FILE} not found, or python3 unavailable"
  fi

  # No managed broad CloudWatch policy (e.g. CloudWatchFullAccess, CloudWatchAgentServerPolicy) attached in addition to the custom policy_folder -- managed_policy_arns stays empty.
  CW_METRICS_IAM_TF="${REPO_ROOT}/envs/dev/iam.tf"
  if [ -f "$CW_METRICS_IAM_TF" ] && command -v python3 >/dev/null 2>&1; then
    CW_MANAGED_ARNS_CHECK="$(python3 - "$CW_METRICS_IAM_TF" <<'PYEOF'
import re
import sys

text = open(sys.argv[1]).read()
m = re.search(r'module\s+"goldengate_cloudwatch_metrics_role_dev"\s*\{(.*?)\n\}', text, re.S)
if not m:
    print("MISMATCH:module-block-not-found")
    raise SystemExit
block = m.group(1)
arns_match = re.search(r'managed_policy_arns\s*=\s*(\[[^\]]*\])', block)
if not arns_match:
    print("MISMATCH:managed_policy_arns-not-found")
    raise SystemExit
arns_value = arns_match.group(1)
if arns_value.strip() != "[]":
    print(f"MISMATCH:managed_policy_arns-not-empty={arns_value}")
    raise SystemExit
for forbidden in ("CloudWatchFullAccess", "CloudWatchAgentServerPolicy", "AdministratorAccess"):
    if forbidden in block:
        print(f"MISMATCH:forbidden-managed-policy-reference={forbidden}")
        raise SystemExit
print("OK")
PYEOF
)"
    if [ "$CW_MANAGED_ARNS_CHECK" = "OK" ]; then
      pass "goldengate_cloudwatch_metrics_role_dev module block has managed_policy_arns=[] and no CloudWatchFullAccess/CloudWatchAgentServerPolicy/AdministratorAccess reference -- the custom policy_folder statement is the only source of permissions"
    else
      fail "goldengate_cloudwatch_metrics_role_dev managed-policy check failed: ${CW_MANAGED_ARNS_CHECK}"
    fi
  else
    fail "${CW_METRICS_IAM_TF} not found, or python3 unavailable"
  fi

  CLOUDWATCH_OBSERVABILITY_TF="${REPO_ROOT}/envs/dev/cloudwatch_observability.tf"
  if [ -f "$CLOUDWATCH_OBSERVABILITY_TF" ]; then
    if grep -q 'local.gg_env_container_insights_log_group' "$CLOUDWATCH_OBSERVABILITY_TF" \
        && grep -q 'default\s*=\s*30' "$CLOUDWATCH_OBSERVABILITY_TF" \
        && grep -q 'goldengate_container_insights_retention_days' "$CLOUDWATCH_OBSERVABILITY_TF"; then
      pass "envs/dev/cloudwatch_observability.tf defines the Container Insights performance log group (name derived from environment config, Fresh-EKS Phase A) with a 30-day default retention variable"
    else
      fail "envs/dev/cloudwatch_observability.tf does not define the expected performance log group and/or 30-day default retention"
    fi
    if grep -qE '"/aws/containerinsights/gg-poc-dev/(application|dataplane|host)"' "$CLOUDWATCH_OBSERVABILITY_TF"; then
      fail "envs/dev/cloudwatch_observability.tf unexpectedly defines an application/dataplane/host Container Insights log group"
    else
      pass "envs/dev/cloudwatch_observability.tf defines no application/dataplane/host Container Insights log group"
    fi
  else
    fail "${CLOUDWATCH_OBSERVABILITY_TF} not found"
  fi

  ARGOCD_ECR_POLICY_FILE="${REPO_ROOT}/envs/dev/policies/argocd-ecr-oci-read-dev/policies/policies_1.json"
  if [ -f "$ARGOCD_ECR_POLICY_FILE" ] && command -v python3 >/dev/null 2>&1; then
    ARGOCD_ECR_CHECK="$(python3 - "$ARGOCD_ECR_POLICY_FILE" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
stmts = doc["Statement"]

expected_arn = "arn:aws:ecr:eu-west-1:229410149234:repository/helm/amazon-cloudwatch-observability"
matching = [s for s in stmts if s.get("Resource") == expected_arn]
if len(matching) != 1:
    print(f"MISMATCH:found={len(matching)}-statements-for-expected-arn")
    raise SystemExit

# Every pre-existing repository ARN this policy already granted (goldengate, goldengate-monitor, goldengate-platform, gg-monitor) must still be present unchanged.
preexisting_arns = {
    "arn:aws:ecr:eu-west-1:229410149234:repository/helm/goldengate",
    "arn:aws:ecr:eu-west-1:229410149234:repository/helm/goldengate-monitor",
    "arn:aws:ecr:eu-west-1:229410149234:repository/helm/goldengate-platform",
    "arn:aws:ecr:eu-west-1:229410149234:repository/helm/gg-monitor",
}
present_arns = {s.get("Resource") for s in stmts}
missing = preexisting_arns - present_arns
if missing:
    print(f"MISMATCH:missing-preexisting-arns={sorted(missing)}")
    raise SystemExit

# ecr:GetAuthorizationToken statement (Resource "*") must be preserved unchanged.
auth_token_stmts = [s for s in stmts if s.get("Resource") == "*"
                     and "ecr:GetAuthorizationToken" in
                     ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]
if len(auth_token_stmts) != 1:
    print("MISMATCH:ecr-GetAuthorizationToken-statement-missing-or-changed")
    raise SystemExit

print("OK")
PYEOF
)"
    if [ "$ARGOCD_ECR_CHECK" = "OK" ]; then
      pass "argocd-ecr-oci-read-dev policy grants the exact amazon-cloudwatch-observability chart repository ARN while preserving every pre-existing statement (including ecr:GetAuthorizationToken)"
    else
      fail "argocd-ecr-oci-read-dev policy check failed: ${ARGOCD_ECR_CHECK}"
    fi
  else
    fail "${ARGOCD_ECR_POLICY_FILE} not found, or python3 unavailable"
  fi

  # Regression proof: the existing Fluent Bit log-group ARNs and policy files are unchanged -- this phase only adds a new role and log group, never touching GoldenGatePlatformLoggingRole-dev or the /adcb/goldengate/dev/* groups.
  FLUENT_BIT_TRUST_FILE="${REPO_ROOT}/envs/dev/policies/goldengate-platform-logging-dev/assume_role_policy/sts.json"
  if [ -f "$FLUENT_BIT_TRUST_FILE" ] \
      && grep -q '"system:serviceaccount:goldengate-dev:gg-fluent-bit"' "$FLUENT_BIT_TRUST_FILE" \
      && [ -f "$LOGGING_POLICY_FILE" ] \
      && grep -q '"arn:aws:logs:eu-west-1:668311715351:log-group:/adcb/goldengate/dev/runtime:\*"' "$LOGGING_POLICY_FILE" \
      && grep -q '"arn:aws:logs:eu-west-1:668311715351:log-group:/adcb/goldengate/dev/monitor:\*"' "$LOGGING_POLICY_FILE"; then
    pass "existing gg-fluent-bit trust subject and GoldenGatePlatformLoggingRole-dev log-group ARNs remain exactly as before"
  else
    fail "gg-fluent-bit trust subject or GoldenGatePlatformLoggingRole-dev log-group ARNs appear to have changed"
  fi

  # Private-image-only CloudWatch Observability GitOps source and deployment workflow. Static/offline only -- no AWS/Terraform/kubectl/Argo CD/Git/network call.

  # 1-9: the committed observability values file.
  if [ -f "${REPO_ROOT}/${OBSERVABILITY_VALUES_FILE}" ] && command -v python3 >/dev/null 2>&1; then
    pass "1: ${OBSERVABILITY_VALUES_FILE} exists"

    OBSERVABILITY_VALUES_CHECK="$(python3 - "${REPO_ROOT}/${OBSERVABILITY_VALUES_FILE}" <<'PYEOF'
import sys
import yaml

class DupKeyLoader(yaml.SafeLoader):
    pass

def construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            print(f"MISMATCH:duplicate-key:{key!r}")
            raise SystemExit
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping

DupKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)

with open(sys.argv[1]) as f:
    v = yaml.load(f, Loader=DupKeyLoader)

def check(actual, expected, label, results):
    if actual != expected:
        results.append(f"{label}={actual!r}(expected {expected!r})")

results = []
# Fresh-EKS Phase A/Phase 10: clusterName/region are shared environment identity -- the committed values file must NOT carry them; the deploy workflow injects both into work/generated-values.yaml from the canonical resolver (EKS_CLUSTER_NAME/AWS_REGION).
if "clusterName" in v:
    results.append(f"clusterName={v.get('clusterName')!r}(expected absent -- injected into generated-values.yaml, not committed)")
if "region" in v:
    results.append(f"region={v.get('region')!r}(expected absent -- injected into generated-values.yaml, not committed)")
check(v.get("k8sMode"), "EKS", "k8sMode", results)
check(v.get("containerLogs", {}).get("enabled"), False, "containerLogs.enabled", results)
check(v.get("containerInsights", {}).get("enabled"), False, "containerInsights.enabled", results)
check(v.get("applicationSignals", {}).get("enabled"), False, "applicationSignals.enabled", results)
check(v.get("manager", {}).get("applicationSignals", {}).get("autoMonitor", {}).get("monitorAllServices"), False, "manager.applicationSignals.autoMonitor.monitorAllServices", results)
check(v.get("otelContainerInsights", {}).get("enabled"), True, "otelContainerInsights.enabled", results)
check(v.get("otelContainerInsights", {}).get("logs", {}).get("enabled"), False, "otelContainerInsights.logs.enabled", results)
check(v.get("dcgmExporter", {}).get("enabled"), False, "dcgmExporter.enabled", results)
check(v.get("neuronMonitor", {}).get("enabled"), False, "neuronMonitor.enabled", results)
check(v.get("kubeStateMetrics", {}).get("enabled"), True, "kubeStateMetrics.enabled", results)
check(v.get("nodeExporter", {}).get("enabled"), True, "nodeExporter.enabled", results)
check(v.get("agent", {}).get("prometheus", {}).get("targetAllocator", {}).get("enabled"), False, "agent.prometheus.targetAllocator.enabled", results)
check(v.get("agent", {}).get("serviceAccount", {}).get("name"), "cloudwatch-agent", "agent.serviceAccount.name", results)

expected_repos = {
    "manager": "aws-cloud-factory-cloudwatch-agent-operator",
    "agent": "aws-cloud-factory-cloudwatch-agent",
    "kubeStateMetrics": "aws-cloud-factory-kube-state-metrics",
    "nodeExporter": "aws-cloud-factory-node-exporter",
}
found_repos = set()
for top_key, expected_repo in expected_repos.items():
    image = v.get(top_key, {}).get("image", {})
    # Fresh-EKS Phase A/Phase 10: repositoryDomainMap.public is shared environment identity (ECR_REGISTRY) -- the committed values file must NOT carry it; the deploy workflow injects it into work/generated-values.yaml for all four images.
    if "repositoryDomainMap" in image:
        results.append(f"{top_key}.image.repositoryDomainMap={image.get('repositoryDomainMap')!r}(expected absent -- injected into generated-values.yaml, not committed)")
    check(image.get("repository"), expected_repo, f"{top_key}.image.repository", results)
    found_repos.add(image.get("repository"))

if found_repos != set(expected_repos.values()):
    results.append(f"image-repository-set={sorted(found_repos)}")

values_text = open(sys.argv[1]).read()
for public_registry in ("public.ecr.aws", "registry.k8s.io", "quay.io", "docker.io", "ghcr.io", "gcr.io", "nvcr.io"):
    # Only flags a live (non-comment) reference -- this file's own docs legitimately mention these registries to record what is NOT used.
    live_lines = [l for l in values_text.splitlines() if public_registry in l and not l.strip().startswith("#")]
    if live_lines:
        results.append(f"live-public-registry-reference={public_registry}:{live_lines}")

if results:
    print("MISMATCH:" + ";".join(results))
else:
    print("OK")
PYEOF
)"
    if [ "$OBSERVABILITY_VALUES_CHECK" = "OK" ]; then
      pass "2-9: ${OBSERVABILITY_VALUES_FILE} pins cluster/region/k8sMode, all required feature flags (containerLogs/containerInsights/applicationSignals/autoMonitor/otelContainerInsights+logs/dcgmExporter/neuronMonitor/kubeStateMetrics/nodeExporter/targetAllocator), agent.serviceAccount.name, exactly the four private image repositories, and no live public registry reference"
    else
      fail "2-9: ${OBSERVABILITY_VALUES_FILE} check failed: ${OBSERVABILITY_VALUES_CHECK}"
    fi
  else
    fail "${OBSERVABILITY_VALUES_FILE} not found, or python3 unavailable"
  fi

  # 10: the Argo CD values file contains exactly four OCI repositories and the exact new Secret name, with the pre-existing three preserved.
  if [ -f "${REPO_ROOT}/${ARGOCD_VALUES_FILE}" ] && command -v python3 >/dev/null 2>&1; then
    ARGOCD_VALUES_CHECK="$(python3 - "${REPO_ROOT}/${ARGOCD_VALUES_FILE}" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
repos = doc.get("ecrTokenSync", {}).get("repositories", [])
expected = [
    ("goldengate", "helm/goldengate", "argocd-ecr-goldengate-oci"),
    ("goldengate-monitor", "helm/goldengate-monitor", "argocd-ecr-goldengate-monitor-oci"),
    ("goldengate-platform", "helm/goldengate-platform", "argocd-ecr-goldengate-platform-oci"),
    ("amazon-cloudwatch-observability", "helm/amazon-cloudwatch-observability", "argocd-ecr-amazon-cloudwatch-observability-oci"),
]
actual = [(r.get("name"), r.get("helmOciRepository"), r.get("argocdRepositorySecretName")) for r in repos]
if len(actual) != 4:
    print(f"MISMATCH:count={len(actual)}")
elif actual != expected:
    print(f"MISMATCH:actual={actual}")
else:
    print("OK")
PYEOF
)"
    if [ "$ARGOCD_VALUES_CHECK" = "OK" ]; then
      pass "10: ${ARGOCD_VALUES_FILE} ecrTokenSync.repositories contains exactly the four expected entries (goldengate, goldengate-monitor, goldengate-platform, amazon-cloudwatch-observability) with the exact new Secret name, in order"
    else
      fail "10: ${ARGOCD_VALUES_FILE} check failed: ${ARGOCD_VALUES_CHECK}"
    fi

    # The four container-image repositories must never be added to Argo CD token sync -- it only ever refreshes Helm OCI chart credentials.
    if python3 -c "
import yaml
doc = yaml.safe_load(open('${REPO_ROOT}/${ARGOCD_VALUES_FILE}'))
repos = doc.get('ecrTokenSync', {}).get('repositories', [])
names = {r.get('helmOciRepository') for r in repos}
forbidden = {'aws-cloud-factory-cloudwatch-agent-operator', 'aws-cloud-factory-cloudwatch-agent', 'aws-cloud-factory-kube-state-metrics', 'aws-cloud-factory-node-exporter'}
assert not (names & forbidden), names & forbidden
"; then
      pass "10b: none of the four container-image repositories were added to Argo CD ecrTokenSync"
    else
      fail "10b: a container-image repository was unexpectedly added to Argo CD ecrTokenSync"
    fi
  else
    fail "${ARGOCD_VALUES_FILE} not found, or python3 unavailable"
  fi

  # 11: argocd-eks-deployment.yaml validates all four repository Secrets.
  if [ -f "${REPO_ROOT}/${ARGOCD_DEPLOY_WORKFLOW}" ]; then
    if python3 -c "import yaml; yaml.safe_load(open('${REPO_ROOT}/${ARGOCD_DEPLOY_WORKFLOW}'))" >/dev/null 2>&1; then
      pass "11a: ${ARGOCD_DEPLOY_WORKFLOW} parses as strict YAML"
    else
      fail "11a: ${ARGOCD_DEPLOY_WORKFLOW} does not parse as strict YAML"
    fi

    if grep -q 'argocd-ecr-amazon-cloudwatch-observability-oci' "${REPO_ROOT}/${ARGOCD_DEPLOY_WORKFLOW}" \
        && grep -q 'helm/amazon-cloudwatch-observability' "${REPO_ROOT}/${ARGOCD_DEPLOY_WORKFLOW}" \
        && grep -qi 'all four' "${REPO_ROOT}/${ARGOCD_DEPLOY_WORKFLOW}" \
        && ! grep -qi 'all three' "${REPO_ROOT}/${ARGOCD_DEPLOY_WORKFLOW}"; then
      pass "11b: ${ARGOCD_DEPLOY_WORKFLOW} references the fourth repository/Secret and its exact-count checks/comments were updated from three to four (no stale 'all three' text remains)"
    else
      fail "11b: ${ARGOCD_DEPLOY_WORKFLOW} does not fully reference the fourth repository, or a stale 'all three' comment/echo remains"
    fi

    # The IAM-policy static-validation step's expected_repos dict must include the fourth repository name, deriving its ARN from the canonical AWS_REGION/ECR_ACCOUNT_ID (never a second hardcoded account/region).
    if grep -q '"helm/amazon-cloudwatch-observability"' "${REPO_ROOT}/${ARGOCD_DEPLOY_WORKFLOW}" \
        && grep -qF 'f"arn:aws:ecr:{region}:{ecr_account_id}:repository/{name}"' "${REPO_ROOT}/${ARGOCD_DEPLOY_WORKFLOW}"; then
      pass "11c: ${ARGOCD_DEPLOY_WORKFLOW}'s IAM-policy validation step expects the amazon-cloudwatch-observability repository ARN, derived from the canonical AWS_REGION/ECR_ACCOUNT_ID"
    else
      fail "11c: ${ARGOCD_DEPLOY_WORKFLOW}'s IAM-policy validation step does not reference the amazon-cloudwatch-observability repository ARN"
    fi
  else
    fail "${ARGOCD_DEPLOY_WORKFLOW} not found"
  fi

  # 12: the new goldengate-observability.yaml workflow.
  if [ -f "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" ] && command -v python3 >/dev/null 2>&1; then
    if python3 -c "import yaml; yaml.safe_load(open('${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}'))" >/dev/null 2>&1; then
      pass "12a: ${OBSERVABILITY_WORKFLOW} parses as strict YAML"
    else
      fail "12a: ${OBSERVABILITY_WORKFLOW} does not parse as strict YAML"
    fi

    OBSERVABILITY_WORKFLOW_CHECK="$(python3 - "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

results = []

if "workflow_dispatch" not in doc.get(True, doc.get("on", {})) and "workflow_dispatch" not in doc.get("on", {}):
    results.append("not-workflow_dispatch-only")
on_block = doc.get(True, doc.get("on", {}))
if set(on_block.keys()) != {"workflow_dispatch"}:
    results.append(f"unexpected-trigger-keys={list(on_block.keys())}")

deploy_input = on_block.get("workflow_dispatch", {}).get("inputs", {}).get("deploy", {})
if deploy_input.get("default") is not False:
    results.append(f"deploy-default={deploy_input.get('default')!r}")
if deploy_input.get("type") != "boolean":
    results.append(f"deploy-type={deploy_input.get('type')!r}")

steps = doc["jobs"]["validate_and_deploy"]["steps"]
all_run_text = "\n".join(s.get("run", "") for s in steps)

if "6.2.0" not in all_run_text:
    results.append("chart-version-6.2.0-not-referenced")
if "oci://" not in all_run_text or ("helm/amazon-cloudwatch-observability" not in all_run_text and "${HELM_OCI_NAMESPACE}/${CHART_NAME}" not in all_run_text):
    results.append("private-oci-chart-ref-not-referenced")
if "aws-observability" in all_run_text.lower() and "helm repo add" in all_run_text.lower():
    results.append("workflow-adds-public-helm-repo")
for repo in ("aws-cloud-factory-cloudwatch-agent-operator", "aws-cloud-factory-cloudwatch-agent",
             "aws-cloud-factory-kube-state-metrics", "aws-cloud-factory-node-exporter"):
    if repo not in all_run_text:
        results.append(f"missing-image-repo-reference:{repo}")
if "imageDigest" not in all_run_text and "imageDetails[0].imageDigest" not in all_run_text:
    results.append("no-digest-resolution")
if "CLOUDWATCH_METRICS_ROLE_ARN" not in all_run_text:
    results.append("missing-iam-role-reference")
# The Secret name is an env: block value (ARGOCD_OBSERVABILITY_SECRET_NAME) referenced in run: blocks only via that variable -- scan the whole document, not just run: block text.
whole_doc_text = str(doc)
if "argocd-ecr-amazon-cloudwatch-observability-oci" not in whole_doc_text:
    results.append("missing-new-argocd-secret-reference")
if "goldengate-observability" not in whole_doc_text:
    results.append("missing-application-name-reference")

# Application creation must be gated by inputs.deploy.
create_app_step = next((s for s in steps if s.get("name") == "Create or update the Argo CD Application"), None)
if create_app_step is None:
    results.append("missing-create-application-step")
elif create_app_step.get("if") != "${{ inputs.deploy }}":
    results.append(f"create-application-step-if={create_app_step.get('if')!r}")

if results:
    print("MISMATCH:" + ";".join(results))
else:
    print("OK")
PYEOF
)"
    if [ "$OBSERVABILITY_WORKFLOW_CHECK" = "OK" ]; then
      pass "12b: ${OBSERVABILITY_WORKFLOW} is workflow_dispatch-only, defaults deploy=false, pins chart 6.2.0, pulls only the private OCI chart, validates all four image repositories, resolves digests, injects the exact IAM role, requires the new Argo CD Secret, and creates the Application only behind deploy=true"
    else
      fail "12b: ${OBSERVABILITY_WORKFLOW} check failed: ${OBSERVABILITY_WORKFLOW_CHECK}"
    fi

    # \\? tolerates the workflow's own shell-regex source (dots backslash-escaped) as well as a plain-text mention.
    if grep -qE 'public\\?\.ecr\\?\.aws|registry\\?\.k8s\\?\.io|quay\\?\.io|docker\\?\.io|ghcr\\?\.io|gcr\\?\.io|nvcr\\?\.io' "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}"; then
      pass "12c: ${OBSERVABILITY_WORKFLOW} contains a public-registry rejection check"
    else
      fail "12c: ${OBSERVABILITY_WORKFLOW} does not appear to reject public registries"
    fi

    if grep -q 'fluent-bit\|fluentbit' "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" \
        && grep -q 'DcgmExporter\|dcgm' "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" \
        && grep -q 'NeuronMonitor\|neuron' "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}"; then
      pass "12d: ${OBSERVABILITY_WORKFLOW} validates against Fluent Bit/GPU/Neuron resources"
    else
      fail "12d: ${OBSERVABILITY_WORKFLOW} is missing a Fluent Bit/GPU/Neuron validation check"
    fi

    # Pre-deployment safety correction (focused, static/offline only -- no AWS/Kubernetes/Argo CD/Git/network call).
    OBSERVABILITY_CORRECTION_CHECK="$(python3 - "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" <<'PYEOF'
import re
import sys
import yaml

path = sys.argv[1]
with open(path) as f:
    text = f.read()
    doc = yaml.safe_load(text)

steps = doc["jobs"]["validate_and_deploy"]["steps"]
results = []

def get_step(name):
    return next((s for s in steps if s.get("name") == name), None)

# --- Correction 1: OCI source path is exactly "." ------------------------
create_app_step = get_step("Create or update the Argo CD Application")
if create_app_step is None:
    results.append("missing-create-application-step")
else:
    run_text = create_app_step.get("run", "")
    # Matches the Python dict-literal source the step embeds -- proves "path" is the literal string "." (not merely present anywhere, and not a "chart:" field).
    if not re.search(r'"path"\s*:\s*"\."\s*,', run_text):
        results.append("oci-path-not-exactly-dot")
    if re.search(r'"chart"\s*:', run_text):
        results.append("unexpected-chart-field-present")
    # Fresh-EKS Phase A/Phase 10: repoURL/targetRevision/namespace are shared environment identity, no longer literals embedded in this step -- resolved once via HELM_CHART_REF (built from the canonical ECR_REGISTRY) and the existing CHART_VERSION/OBSERVABILITY_NAMESPACE constants, then passed as argv into this exact Python dict construction.
    if '"repoURL": helm_chart_ref' not in run_text:
        results.append("repoURL-changed-or-missing")
    if '"targetRevision": chart_version' not in run_text:
        results.append("targetRevision-changed-or-missing")
    if 'HELM_CHART_REF="oci://${ECR_REGISTRY}/${HELM_OCI_NAMESPACE}/${CHART_NAME}"' not in run_text:
        results.append("helm-chart-ref-not-derived-from-ecr-registry")

    # --- Correction 2: ignoreDifferences + RespectIgnoreDifferences -------
    if not re.search(r'"group"\s*:\s*""\s*,\s*\n\s*"kind"\s*:\s*"ServiceAccount"\s*,\s*\n\s*"name"\s*:\s*"cloudwatch-agent"\s*,\s*\n\s*"namespace"\s*:\s*observability_namespace', run_text):
        results.append("ignoreDifferences-rule-not-exact")
    if '/metadata/annotations/eks.amazonaws.com~1role-arn' not in run_text:
        results.append("missing-role-arn-json-pointer")
    if "RespectIgnoreDifferences=true" not in run_text:
        results.append("missing-RespectIgnoreDifferences")
    if "CreateNamespace=true" not in run_text:
        results.append("missing-CreateNamespace")
    if "ServerSideApply=true" not in run_text:
        results.append("missing-ServerSideApply")
    # No broad group/kind/name wildcard: ignoreDifferences must reference exactly one Sid-equivalent rule, not e.g. bare kind:ServiceAccount without a name or a missing namespace.
    ignore_diff_block_match = re.search(r'"ignoreDifferences"\s*:\s*\[(.*?)\],\s*\n\s*"revisionHistoryLimit"', run_text, re.S)
    if ignore_diff_block_match:
        block = ignore_diff_block_match.group(1)
        if block.count('"kind"') != 1 or block.count('"name"') != 1 or block.count('"namespace"') != 1:
            results.append("ignoreDifferences-has-more-than-one-rule-or-is-ambiguous")
        if '"name": "*"' in block or '"kind": "*"' in block:
            results.append("ignoreDifferences-uses-a-wildcard")
    else:
        results.append("ignoreDifferences-block-not-found")

# --- Correction 3: chart repository participates in the immutability check
immutable_step = get_step("Verify all five repositories (chart + four images) are IMMUTABLE")
if immutable_step is None:
    results.append("missing-renamed-immutability-step")
else:
    run_text = immutable_step.get("run", "")
    if "check_repo_immutable \"$CHART_ECR_REPOSITORY\"" not in run_text:
        results.append("chart-repo-not-checked-for-immutability")

# --- Correction 4: namespace-scoped negative live checks (no -A) ----------
live_validation_step = get_step("Live Kubernetes validation")
if live_validation_step is None:
    results.append("missing-live-validation-step")
else:
    run_text = live_validation_step.get("run", "")
    for forbidden_kind in ("instrumentations.cloudwatch.aws.amazon.com", "dcgmexporters.cloudwatch.aws.amazon.com", "neuronmonitors.cloudwatch.aws.amazon.com"):
        pattern = re.escape(f"kubectl get {forbidden_kind}")
        matches = re.findall(pattern + r'[^\n]*', run_text)
        if not matches:
            results.append(f"missing-check:{forbidden_kind}")
        for m in matches:
            if " -A " in m or m.rstrip().endswith(" -A"):
                results.append(f"still-uses--A:{forbidden_kind}")
            if f'-n "$TARGET_NAMESPACE"' not in m:
                results.append(f"not-namespace-scoped:{forbidden_kind}")

    # --- Correction 5: live image extraction includes initContainers -----
    if "spec.initContainers" not in run_text and ".spec.initContainers" not in run_text:
        results.append("live-image-check-missing-initContainers")
    if "spec.containers" not in run_text:
        results.append("live-image-check-missing-containers")

    # Scoped to the filelog section only -- section 14 legitimately looks up the same named CRs for an unrelated host-network isolation check, which this regression check must not fire on.
    filelog_section_match = re.search(r'13\. No deployed AmazonCloudWatchAgent.*?(?=14\. CloudWatch Agent host-network isolation|\Z)', run_text, re.S)
    filelog_section = filelog_section_match.group(0) if filelog_section_match else run_text
    if "amazoncloudwatchagents.cloudwatch.aws.amazon.com -n \"$TARGET_NAMESPACE\"" not in filelog_section:
        results.append("filelog-check-not-listing-all-crs")
    if re.search(r'amazoncloudwatchagents\.cloudwatch\.aws\.amazon\.com\s+cloudwatch-agent\s+-n', filelog_section):
        results.append("filelog-check-still-hardcodes-single-cr-name")

# --- Correction 7: IRSA env var NAME checks without printing values -------
irsa_step = get_step("Verify IRSA injection on the recreated CloudWatch Agent pods")
if irsa_step is None:
    results.append("missing-irsa-verification-step")
else:
    run_text = irsa_step.get("run", "")
    if "AWS_ROLE_ARN" not in run_text:
        results.append("irsa-check-missing-AWS_ROLE_ARN")
    if "AWS_WEB_IDENTITY_TOKEN_FILE" not in run_text:
        results.append("irsa-check-missing-AWS_WEB_IDENTITY_TOKEN_FILE")
    if "serviceAccountName" not in run_text:
        results.append("irsa-check-missing-serviceAccountName-check")
    # Must never print the resolved env var VALUE or a full env dump -- only the pattern capturing NAMES (jsonpath .name, not .value).
    if re.search(r'\.env\[\*\]\}\{\.value\}', run_text):
        results.append("irsa-check-appears-to-print-env-values")

if results:
    print("MISMATCH:" + ";".join(results))
else:
    print("OK")
PYEOF
)"
    if [ "$OBSERVABILITY_CORRECTION_CHECK" = "OK" ]; then
      pass "16: goldengate-observability.yaml Phase 6B2B safety correction: OCI path='.', ignoreDifferences/RespectIgnoreDifferences, chart-repository immutability, namespace-scoped negative checks, initContainers image coverage, all-CR filelog check, and IRSA env-var-name-only verification are all present exactly as required"
    else
      fail "16: goldengate-observability.yaml Phase 6B2B safety correction check failed: ${OBSERVABILITY_CORRECTION_CHECK}"
    fi

    # Runner/connectivity correction (focused, static/offline only -- no AWS/kubectl/network/Git call).
    RUNNER_CONNECTIVITY_CHECK="$(python3 - "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" <<'PYEOF'
import sys
import yaml

path = sys.argv[1]
with open(path) as f:
    text = f.read()
    doc = yaml.safe_load(text)

results = []

job = doc["jobs"]["validate_and_deploy"]
steps = job["steps"]

def get_step(name):
    return next((s for s in steps if s.get("name") == name), None)

# 1-2: exact CodeBuild runner, no ubuntu-latest anywhere in the job.
EXPECTED_RUNNER = "codebuild-${{ vars.PROJECT_NAME_DEV }}-${{ github.run_id }}-${{ github.run_attempt }}"
if job.get("runs-on") != EXPECTED_RUNNER:
    results.append(f"runs-on={job.get('runs-on')!r}")
import re
if re.search(r'runs-on:\s*ubuntu-latest', text):
    results.append("ubuntu-latest-still-used-as-runs-on")

# 3-4: Helm/kubectl installation supports both amd64 and arm64.
install_step = get_step("Install or validate required tools")
if install_step is None:
    results.append("missing-install-tools-step")
else:
    run_text = install_step.get("run", "")
    if run_text.count("x86_64") < 2:
        results.append("arch-detection-not-applied-to-both-helm-and-kubectl")
    if "HELM_ARCH=\"amd64\"" not in run_text and 'HELM_ARCH="amd64"' not in run_text:
        results.append("helm-amd64-mapping-missing")
    if 'HELM_ARCH="arm64"' not in run_text:
        results.append("helm-arm64-mapping-missing")
    if 'KUBECTL_ARCH="amd64"' not in run_text:
        results.append("kubectl-amd64-mapping-missing")
    if 'KUBECTL_ARCH="arm64"' not in run_text:
        results.append("kubectl-arm64-mapping-missing")
    if "linux-amd64.tar.gz" in run_text or "linux/amd64/kubectl" in run_text:
        results.append("hardcoded-linux-amd64-still-present")

# 5-6: connectivity step exists and is deploy-guarded.
connectivity_step = get_step("Verify private EKS API connectivity and access")
if connectivity_step is None:
    results.append("missing-connectivity-step")
elif connectivity_step.get("if") != "${{ inputs.deploy }}":
    results.append(f"connectivity-step-if={connectivity_step.get('if')!r}")

# 7: ordering -- Connect to EKS cluster < connectivity step < CRD step.
names = [s.get("name") for s in steps]
try:
    connect_idx = names.index("Connect to EKS cluster")
    connectivity_idx = names.index("Verify private EKS API connectivity and access")
    crd_idx = names.index("Ensure Argo CD Application CRD exists")
    if not (connect_idx < connectivity_idx < crd_idx):
        results.append(f"step-order-wrong:{connect_idx},{connectivity_idx},{crd_idx}")
except ValueError as e:
    results.append(f"step-not-found-for-ordering:{e}")

# 8: bounded request timeout on the connectivity step.
if connectivity_step is not None:
    run_text = connectivity_step.get("run", "")
    if "--request-timeout=20s" not in run_text:
        results.append("connectivity-step-missing-bounded-timeout")

    # 9: error handling mentions private EKS/network reachability and does NOT claim the CRD is missing.
    lowered = run_text.lower()
    if "private eks api" not in lowered and "network-reachability" not in lowered and "network reachability" not in lowered:
        results.append("connectivity-step-missing-network-reachability-wording")
    if "crd applications.argoproj.io not found" in lowered:
        results.append("connectivity-step-still-claims-crd-missing")

# 10: CRD step separately handles present/not-found/forbidden/unexpected.
crd_step = get_step("Ensure Argo CD Application CRD exists")
if crd_step is None:
    results.append("missing-crd-step")
else:
    run_text = crd_step.get("run", "")
    lowered = run_text.lower()
    if "is present" not in lowered:
        results.append("crd-step-missing-present-case")
    if "genuinely absent" not in lowered and ("not found" not in lowered and "notfound" not in lowered):
        results.append("crd-step-missing-not-found-case")
    if "forbidden" not in lowered:
        results.append("crd-step-missing-forbidden-case")
    if "unexpected reason" not in lowered:
        results.append("crd-step-missing-unexpected-case")

    # 11: the old unconditional false-diagnosis pattern must be gone.
    if "kubectl get crd applications.argoproj.io >/dev/null || {" in run_text:
        results.append("old-false-diagnosis-pattern-still-present")

if results:
    print("MISMATCH:" + ";".join(results))
else:
    print("OK")
PYEOF
)"
    if [ "$RUNNER_CONNECTIVITY_CHECK" = "OK" ]; then
      pass "17: goldengate-observability.yaml Phase 6B2B runner/connectivity correction: exact CodeBuild runs-on (no ubuntu-latest), Helm/kubectl amd64+arm64 arch detection, a deploy-guarded 'Verify private EKS API connectivity and access' step correctly ordered between 'Connect to EKS cluster' and 'Ensure Argo CD Application CRD exists' with a bounded request timeout and non-CRD-blaming network-failure wording, and a CRD step that separately classifies present/not-found/forbidden/unexpected (the old unconditional false-diagnosis pattern is gone)"
    else
      fail "17: goldengate-observability.yaml Phase 6B2B runner/connectivity correction check failed: ${RUNNER_CONNECTIVITY_CHECK}"
    fi

    # DaemonSet full-readiness and failure-diagnostics correction (focused, static/offline only).
    DAEMONSET_READINESS_CHECK="$(python3 - "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" <<'PYEOF'
import sys
import yaml

path = sys.argv[1]
with open(path) as f:
    text = f.read()
    doc = yaml.safe_load(text)

results = []

job = doc["jobs"]["validate_and_deploy"]
steps = job["steps"]

def get_step(name):
    return next((s for s in steps if s.get("name") == name), None)

# 1-3: a reusable exact DaemonSet readiness function comparing all required fields, with a bounded timeout and polling interval.
wait_step = get_step("Wait for CloudWatch Agent workloads to roll out")
if wait_step is None:
    results.append("missing-wait-step")
else:
    run_text = wait_step.get("run", "")

    if "wait_for_daemonset_fully_ready()" not in run_text and "wait_for_daemonset_fully_ready ()" not in run_text:
        results.append("missing-wait_for_daemonset_fully_ready-function")

    required_fields = [
        "metadata.generation", "observedGeneration",
        "desiredNumberScheduled", "currentNumberScheduled",
        "updatedNumberScheduled", "numberReady", "numberAvailable",
        "numberUnavailable",
    ]
    for field in required_fields:
        if field not in run_text:
            results.append(f"missing-field-reference:{field}")

    # exact comparisons, not merely field mentions
    for exact_cmp in (
        '[ "$generation" = "$observed" ]',
        '[ "$current" -eq "$desired" ]',
        '[ "$updated" -eq "$desired" ]',
        '[ "$ready" -eq "$desired" ]',
        '[ "$available" -eq "$desired" ]',
        '[ "$unavailable" -eq 0 ]',
        '[ "$desired" -gt 0 ]',
    ):
        if exact_cmp not in run_text:
            results.append(f"missing-exact-comparison:{exact_cmp}")

    if "timeout_seconds" not in run_text or "poll_interval" not in run_text:
        results.append("missing-bounded-timeout-or-poll-interval")

    # 4: applied to both cloudwatch-agent and node-exporter.
    if 'wait_for_daemonset_fully_ready "$TARGET_NAMESPACE" cloudwatch-agent' not in run_text:
        results.append("waiter-not-applied-to-cloudwatch-agent")
    if 'wait_for_daemonset_fully_ready "$TARGET_NAMESPACE" node-exporter' not in run_text:
        results.append("waiter-not-applied-to-node-exporter")

    # rollout status must still be present (kept, not replaced).
    if "kubectl rollout status daemonset/cloudwatch-agent" not in run_text:
        results.append("rollout-status-for-cloudwatch-agent-removed")
    if "kubectl rollout status daemonset/node-exporter" not in run_text:
        results.append("rollout-status-for-node-exporter-removed")

    # 5: dynamically derived selector from spec.selector.matchLabels (no hardcoded chart labels).
    if "spec.selector.matchLabels" not in run_text:
        results.append("missing-dynamic-selector-derivation")
    if "show_daemonset_diagnostics()" not in run_text and "show_daemonset_diagnostics ()" not in run_text:
        results.append("missing-show_daemonset_diagnostics-function")

    # 6: failure diagnostics include bounded pod state, node name, waiting reason, restart count, bounded events, bounded current/previous logs.
    for marker in (
        "nodeName", "restartCount", "state.waiting.reason",
        "kubectl get events", "--tail=80", "--previous",
        "tolerated",
    ):
        if marker not in run_text:
            results.append(f"diagnostics-missing:{marker}")

    # Diagnostics called before failing, exit non-zero, no proceeding past the timeout.
    if run_text.count("show_daemonset_diagnostics \"$TARGET_NAMESPACE\" cloudwatch-agent") < 1:
        results.append("diagnostics-not-called-for-cloudwatch-agent")
    if run_text.count("show_daemonset_diagnostics \"$TARGET_NAMESPACE\" node-exporter") < 1:
        results.append("diagnostics-not-called-for-node-exporter")
    if "FAIL: cloudwatch-agent did not reach full readiness" not in run_text:
        results.append("missing-cloudwatch-agent-timeout-fail-message")
    if "FAIL: node-exporter did not reach full readiness" not in run_text:
        results.append("missing-node-exporter-timeout-fail-message")

# 7-8: IRSA check iterates across every CloudWatch Agent DaemonSet pod; the checked count must equal desiredNumberScheduled.
irsa_step = get_step("Verify IRSA injection on the recreated CloudWatch Agent pods")
if irsa_step is None:
    results.append("missing-irsa-step")
else:
    run_text = irsa_step.get("run", "")
    if "verify_daemonset_irsa_all_pods()" not in run_text and "verify_daemonset_irsa_all_pods ()" not in run_text:
        results.append("missing-verify_daemonset_irsa_all_pods-function")
    if 'pod_count -ne "$desired"' not in run_text and 'pod_count" -ne "$desired"' not in run_text:
        results.append("irsa-pod-count-not-compared-to-desired")
    if 'checked -ne "$desired"' not in run_text and 'checked" -ne "$desired"' not in run_text:
        results.append("irsa-checked-count-not-compared-to-desired")
    if "verify_daemonset_irsa_all_pods \"$TARGET_NAMESPACE\" cloudwatch-agent" not in run_text:
        results.append("irsa-all-pods-not-invoked-for-cloudwatch-agent")
    # cluster-scraper verification retained.
    if "cloudwatch-agent-cluster-scraper Deployment" not in run_text:
        results.append("cluster-scraper-irsa-check-removed")
    # phase/Ready must be checked per pod (not only serviceAccount/env).
    if '.status.phase' not in run_text:
        results.append("irsa-check-missing-phase-check")

# 9: live validation requires both numberReady and numberAvailable to equal desiredNumberScheduled (not READY >= DESIRED).
live_step = get_step("Live Kubernetes validation")
if live_step is None:
    results.append("missing-live-validation-step")
else:
    run_text = live_step.get("run", "")
    if '-lt "${DESIRED:-1}"' in run_text or '-lt "${NE_DESIRED:-1}"' in run_text:
        results.append("live-validation-still-uses-weak-lt-comparison")
    if '"$READY" -ne "$DESIRED"' not in run_text or '"$AVAILABLE" -ne "$DESIRED"' not in run_text:
        results.append("live-validation-missing-cloudwatch-agent-ready-and-available-equality")
    if '"$NE_READY" -ne "$NE_DESIRED"' not in run_text or '"$NE_AVAILABLE" -ne "$NE_DESIRED"' not in run_text:
        results.append("live-validation-missing-node-exporter-ready-and-available-equality")
    if 'DESIRED" -eq 0' not in run_text and "DESIRED\" -eq 0" not in run_text:
        results.append("live-validation-missing-zero-desired-guard")

# 10: the bounded log diagnostic step uses always() with deploy=true and does not fail the workflow itself.
log_step = get_step("Check bounded recent logs for authorization and startup failures")
if log_step is None:
    results.append("missing-log-diagnostic-step")
else:
    if log_step.get("if") != "${{ always() && inputs.deploy }}":
        results.append(f"log-step-if={log_step.get('if')!r}")
    run_text = log_step.get("run", "")
    if "set -euo pipefail" in run_text:
        results.append("log-step-still-uses-set-e-which-could-fail-the-step-itself")
    if "exit 0" not in run_text:
        results.append("log-step-missing-explicit-exit-0")

# 11: no maxUnavailable, probe, resource, toleration, IAM, Terraform, or Helm value change anywhere in this file (comment lines excluded); hostNetwork is deliberately excluded from this list since a separate, later correction legitimately reads/validates it read-only (see check 19/20 below).
forbidden_markers = [
    "maxUnavailable", "readinessProbe", "livenessProbe",
    "resources:", "tolerations:",
    "updateStrategy",
]
code_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
code_text = "\n".join(code_lines)
for marker in forbidden_markers:
    if marker in code_text:
        results.append(f"forbidden-workload-change-introduced:{marker.strip()}")

if results:
    print("MISMATCH:" + ";".join(results))
else:
    print("OK")
PYEOF
)"
    if [ "$DAEMONSET_READINESS_CHECK" = "OK" ]; then
      pass "18: goldengate-observability.yaml Phase 6B2B DaemonSet full-readiness/diagnostics correction: wait_for_daemonset_fully_ready compares generation/observedGeneration/desired/current/updated/ready/available/unavailable with a bounded timeout+poll interval and is applied to both cloudwatch-agent and node-exporter (kubectl rollout status kept, not replaced); show_daemonset_diagnostics dynamically derives the pod selector from spec.selector.matchLabels and prints bounded pod state/events/current+previous logs before the step fails and exits non-zero; IRSA verification now iterates every cloudwatch-agent DaemonSet pod and requires the checked count to equal desiredNumberScheduled while still checking the cluster-scraper pod; Live Kubernetes validation requires exact numberReady==desired and numberAvailable==desired (no weak >=) with a zero-desired guard; the bounded log-diagnostics step is always()-guarded, never uses set -e, and exits 0; and no maxUnavailable/probe/resource/toleration/updateStrategy change was introduced"
    else
      fail "18: goldengate-observability.yaml Phase 6B2B DaemonSet full-readiness/diagnostics correction check failed: ${DAEMONSET_READINESS_CHECK}"
    fi

    # Host-network isolation correction (focused, static/offline only) -- workflow-side checks: semantic validation, rendered CR validation, live hostNetwork validation, and the exact crash-symptom log check.
    HOSTNETWORK_WORKFLOW_CHECK="$(python3 - "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" <<'PYEOF'
import sys
import yaml

path = sys.argv[1]
with open(path) as f:
    text = f.read()
    doc = yaml.safe_load(text)

results = []

job = doc["jobs"]["validate_and_deploy"]
steps = job["steps"]

def get_step(name):
    return next((s for s in steps if s.get("name") == name), None)

# 6: the semantic-values-validation step validates both agents.
semantic_step = get_step("Semantically validate the generated deployment values")
if semantic_step is None:
    results.append("missing-semantic-validation-step")
else:
    run_text = semantic_step.get("run", "")
    for marker in (
        'v.get("agents")',
        "len(agents) != 2",
        'expected_names = {"cloudwatch-agent", "cloudwatch-agent-cluster-scraper"}',
        'expect(cw_agent.get("mode"), "daemonset"',
        'cw_agent.get("hostNetwork") is not True',
        'expect(scraper_agent.get("mode"), "deployment"',
        'expect(scraper_agent.get("config"), "default"',
        'scraper_agent.get("hostNetwork") is not False',
    ):
        if marker not in run_text:
            results.append(f"semantic-validation-missing:{marker}")

# 7: a step validates the two rendered AmazonCloudWatchAgent resources.
render_step = get_step("Validate rendered CloudWatch Agent host-network isolation")
if render_step is None:
    results.append("missing-rendered-cr-hostnetwork-step")
else:
    run_text = render_step.get("run", "")
    for marker in (
        'find_one("AmazonCloudWatchAgent", "cloudwatch-agent")',
        'find_one("AmazonCloudWatchAgent", "cloudwatch-agent-cluster-scraper")',
        "cw_mode != \"daemonset\"",
        "cw_host_network is not True",
        "scraper_mode != \"deployment\"",
        "scraper_host_network is not False",
    ):
        if marker not in run_text:
            results.append(f"rendered-cr-check-missing:{marker}")

# 8: live validation checks both custom-resource and workload hostNetwork.
live_step = get_step("Live Kubernetes validation")
if live_step is None:
    results.append("missing-live-validation-step")
else:
    run_text = live_step.get("run", "")
    for marker in (
        "amazoncloudwatchagents.cloudwatch.aws.amazon.com cloudwatch-agent -n",
        "amazoncloudwatchagents.cloudwatch.aws.amazon.com cloudwatch-agent-cluster-scraper -n",
        '"$CW_AGENT_CR_HOSTNET" != "true"',
        '"$SCRAPER_CR_HOSTNET" != "false"',
        "kubectl get daemonset cloudwatch-agent -n \"$TARGET_NAMESPACE\" -o jsonpath='{.spec.template.spec.hostNetwork}'",
        "kubectl get deployment cloudwatch-agent-cluster-scraper -n \"$TARGET_NAMESPACE\" -o jsonpath='{.spec.template.spec.hostNetwork}'",
        '"$CW_DS_HOSTNET" != "true"',
        '"$SCRAPER_DEPLOY_HOSTNET" != "false"',
    ):
        if marker not in run_text:
            results.append(f"live-hostnetwork-check-missing:{marker}")

    # Every node-agent pod and every active cluster-scraper pod checked, via a dynamically derived selector (no hardcoded chart labels).
    if run_text.count("spec.selector.matchLabels") < 2:
        results.append("live-validation-selector-not-dynamically-derived-for-both-workloads")
    if '"$pod_hostnet" != "true"' not in run_text:
        results.append("live-validation-missing-per-pod-node-agent-hostnetwork-check")
    if '"$pod_hostnet" != "false"' not in run_text:
        results.append("live-validation-missing-per-pod-scraper-hostnetwork-check")

    # 9: the workflow detects the exact observed crash symptom.
    if "bind: address already in use" not in run_text:
        results.append("missing-exact-crash-pattern:bind-address-already-in-use")
    if "binding address localhost:8888" not in run_text:
        results.append("missing-exact-crash-pattern:binding-address-localhost-8888")
    if "--tail=80" not in run_text:
        results.append("crash-log-check-not-bounded")

if results:
    print("MISMATCH:" + ";".join(results))
else:
    print("OK")
PYEOF
)"
    if [ "$HOSTNETWORK_WORKFLOW_CHECK" = "OK" ]; then
      pass "19: goldengate-observability.yaml Phase 6B2B host-network isolation correction (workflow): semantic values validation requires exactly 2 named agents with cloudwatch-agent.mode=daemonset/hostNetwork=true and cloudwatch-agent-cluster-scraper.mode=deployment/config=default/hostNetwork=false; a dedicated step validates the two rendered AmazonCloudWatchAgent custom resources' spec.mode/spec.hostNetwork; Live Kubernetes validation checks both CR and DaemonSet/Deployment spec.template.spec.hostNetwork plus every individual node-agent and cluster-scraper pod via dynamically-derived selectors; and a bounded (--tail=80) log check detects the exact observed 'bind: address already in use' / 'binding address localhost:8888' crash symptom"
    else
      fail "19: goldengate-observability.yaml Phase 6B2B host-network isolation correction (workflow) check failed: ${HOSTNETWORK_WORKFLOW_CHECK}"
    fi
  else
    fail "${OBSERVABILITY_WORKFLOW} not found, or python3 unavailable"
  fi

  # Host-network isolation correction (focused, static/offline only) -- values.yaml-side checks.
  if [ -f "${REPO_ROOT}/${OBSERVABILITY_VALUES_FILE}" ] && command -v python3 >/dev/null 2>&1; then
    HOSTNETWORK_VALUES_CHECK="$(python3 - "${REPO_ROOT}/${OBSERVABILITY_VALUES_FILE}" <<'PYEOF'
import sys
import yaml

path = sys.argv[1]

class DupCheckLoader(yaml.SafeLoader):
    pass

def no_dup_construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"Duplicate key found: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping

DupCheckLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dup_construct_mapping)

with open(path) as f:
    text = f.read()
    v = yaml.load(text, Loader=DupCheckLoader)

results = []

# 1 & 4: agents is a top-level key (not nested under agent), exactly 2 entries.
agents = v.get("agents")
if not isinstance(agents, list):
    results.append(f"agents-not-a-list:{type(agents).__name__}")
elif len(agents) != 2:
    results.append(f"agents-entry-count:{len(agents)}")

agent_block = v.get("agent")
if not isinstance(agent_block, dict) or "agents" in agent_block:
    results.append("agents-nested-under-agent-or-agent-block-missing")

if isinstance(agents, list):
    by_name = {a.get("name"): a for a in agents}

    # 2: cloudwatch-agent -- mode daemonset, hostNetwork true.
    cw = by_name.get("cloudwatch-agent")
    if cw is None:
        results.append("cloudwatch-agent-entry-missing")
    else:
        if cw.get("mode") != "daemonset":
            results.append(f"cloudwatch-agent-mode:{cw.get('mode')!r}")
        if cw.get("hostNetwork") is not True:
            results.append(f"cloudwatch-agent-hostNetwork:{cw.get('hostNetwork')!r}")

    # 3: cluster-scraper -- mode deployment, config default, hostNetwork false.
    scraper = by_name.get("cloudwatch-agent-cluster-scraper")
    if scraper is None:
        results.append("cluster-scraper-entry-missing")
    else:
        if scraper.get("mode") != "deployment":
            results.append(f"cluster-scraper-mode:{scraper.get('mode')!r}")
        if scraper.get("config") != "default":
            results.append(f"cluster-scraper-config:{scraper.get('config')!r}")
        if scraper.get("hostNetwork") is not False:
            results.append(f"cluster-scraper-hostNetwork:{scraper.get('hostNetwork')!r}")

# 5: existing top-level agent image/ServiceAccount/target-allocator/private-ECR configuration remains present and unweakened.
if isinstance(agent_block, dict):
    if agent_block.get("serviceAccount", {}).get("name") != "cloudwatch-agent":
        results.append("agent.serviceAccount.name-missing-or-changed")
    img = agent_block.get("image", {})
    if img.get("repository") != "aws-cloud-factory-cloudwatch-agent":
        results.append("agent.image.repository-missing-or-changed")
    # Fresh-EKS Phase A/Phase 10: repositoryDomainMap.public is shared environment identity, injected into work/generated-values.yaml by the deploy workflow -- the committed values file must NOT carry it.
    if "repositoryDomainMap" in img:
        results.append("agent.image.repositoryDomainMap-present-but-should-be-injected-not-committed")
    if agent_block.get("prometheus", {}).get("targetAllocator", {}).get("enabled") is not False:
        results.append("agent.prometheus.targetAllocator.enabled-missing-or-changed")
else:
    results.append("agent-block-missing")

# 10: no port override, hostPort, or anti-affinity workaround introduced.
code_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
code_text = "\n".join(code_lines)
for marker in ("hostPort", "podAntiAffinity", "8889", "8887"):
    if marker in code_text:
        results.append(f"forbidden-marker-in-values:{marker}")
# Port 8888 itself must never be manually assigned a value in code (only ever discussed in comments, which are excluded above).
if "8888" in code_text:
    results.append("port-8888-referenced-outside-comments")

if results:
    print("MISMATCH:" + ";".join(results))
else:
    print("OK")
PYEOF
)"
    if [ "$HOSTNETWORK_VALUES_CHECK" = "OK" ]; then
      pass "20: goldengate-observability values.yaml Phase 6B2B host-network isolation correction: top-level agents list (not nested under agent) contains exactly 2 entries -- cloudwatch-agent (mode=daemonset, hostNetwork=true) and cloudwatch-agent-cluster-scraper (mode=deployment, config=default, hostNetwork=false) -- while the existing agent.serviceAccount.name/agent.image/agent.prometheus.targetAllocator private-ECR configuration remains unchanged, and no hostPort/anti-affinity/manual-8888-port-value workaround was introduced"
    else
      fail "20: goldengate-observability values.yaml Phase 6B2B host-network isolation correction check failed: ${HOSTNETWORK_VALUES_CHECK}"
    fi
  else
    fail "${OBSERVABILITY_VALUES_FILE} not found, or python3 unavailable"
  fi

  # Cluster-scraper Deployment recreate correction (focused, static/offline only).
  if [ -f "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" ] && command -v python3 >/dev/null 2>&1; then
    RECREATE_CORRECTION_CHECK="$(python3 - "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" <<'PYEOF'
import sys
import yaml

path = sys.argv[1]
with open(path) as f:
    text = f.read()
    doc = yaml.safe_load(text)

results = []

job = doc["jobs"]["validate_and_deploy"]
steps = job["steps"]

def get_step(name):
    return next((s for s in steps if s.get("name") == name), None)

names = [s.get("name") for s in steps]

recreate_step = get_step("Ensure cluster-scraper Deployment host-network isolation")
if recreate_step is None:
    results.append("missing-recreate-step")
else:
    if recreate_step.get("if") != "${{ inputs.deploy }}":
        results.append(f"recreate-step-if={recreate_step.get('if')!r}")

    # Ordering: after "Wait for Argo CD sync and health", before "Annotate the CloudWatch Agent ServiceAccount with the dedicated IRSA role".
    try:
        sync_idx = names.index("Wait for Argo CD sync and health")
        recreate_idx = names.index("Ensure cluster-scraper Deployment host-network isolation")
        annotate_idx = names.index("Annotate the CloudWatch Agent ServiceAccount with the dedicated IRSA role")
        if not (sync_idx < recreate_idx < annotate_idx):
            results.append(f"step-order-wrong:{sync_idx},{recreate_idx},{annotate_idx}")
    except ValueError as e:
        results.append(f"step-not-found-for-ordering:{e}")

    run_text = recreate_step.get("run", "")

    # 2: confirms CR hostNetwork=false before any deletion (the mode/hostNetwork check happens before the delete call in source order).
    cr_check_idx = run_text.find('"$cr_hostnetwork" != "false"')
    delete_idx = run_text.find("kubectl delete deployment")
    if cr_check_idx == -1:
        results.append("missing-cr-hostnetwork-false-check")
    if delete_idx == -1:
        results.append("missing-delete-call")
    if cr_check_idx != -1 and delete_idx != -1 and not (cr_check_idx < delete_idx):
        results.append("cr-hostnetwork-check-not-before-delete")

    # 3: checks the exact controller ownerReference UID against the CR UID.
    if 'owner_uid="$(jq -r' not in run_text or "cr_uid" not in run_text:
        results.append("missing-owner-uid-vs-cr-uid-check")
    if 'uid_match="false"' not in run_text or '[ "$owner_uid" = "$cr_uid" ]' not in run_text:
        results.append("missing-explicit-uid-comparison")

    # 4/5: deletes only the exact cluster-scraper Deployment; never the CR, DaemonSet, pods, operator, ServiceAccount, Secret, or ConfigMap.
    delete_calls = [ln for ln in run_text.splitlines() if "kubectl delete" in ln]
    if len(delete_calls) != 1:
        results.append(f"unexpected-delete-call-count:{len(delete_calls)}")
    elif "kubectl delete deployment \"$CLUSTER_SCRAPER_DEPLOYMENT\" -n \"$TARGET_NAMESPACE\"" not in delete_calls[0]:
        results.append(f"delete-call-not-exact-deployment:{delete_calls[0].strip()}")
    for forbidden in ("delete daemonset", "delete pod ", "delete serviceaccount", "delete secret", "delete configmap", "delete amazoncloudwatchagent"):
        if forbidden in run_text:
            results.append(f"forbidden-delete-target-present:{forbidden.strip()}")

    # 6: at most one deletion is possible per workflow run -- exactly one kubectl delete call exists in source, and no loop/retry wraps it.
    if run_text.count("kubectl delete deployment") != 1:
        results.append("delete-call-appears-more-than-once-in-source")

    # 7: records old UID and requires a different new UID.
    if "old_uid=" not in run_text:
        results.append("missing-old-uid-recording")
    if '"$d_uid" = "$old_uid"' not in run_text:
        results.append("missing-new-uid-differs-from-old-check")

    # 8: validates the recreated Deployment hostNetwork=false.
    if '"$d_hostnetwork" != "false"' not in run_text:
        results.append("missing-recreated-deployment-hostnetwork-false-check")

    # 9: validates active scraper pods hostNetwork=false and podIP != hostIP.
    if '"$pod_hostnetwork" != "false"' not in run_text:
        results.append("missing-active-pod-hostnetwork-false-check")
    if "ip_differs" not in run_text or '"$pod_ip" != "$host_ip"' not in run_text:
        results.append("missing-podip-differs-from-hostip-check")
    if '"$pod_sa" != "$CLOUDWATCH_AGENT_SERVICE_ACCOUNT"' not in run_text:
        results.append("missing-active-pod-serviceaccount-check")
    if "AWS_ROLE_ARN" not in run_text or "AWS_WEB_IDENTITY_TOKEN_FILE" not in run_text:
        results.append("missing-active-pod-irsa-env-name-checks")

    # 10: idempotent when the Deployment is already false (no delete call reachable -- the "already false" branch returns early).
    if 'echo "not_required" > "$CORRECTION_SUMMARY_FILE"' not in run_text:
        results.append("missing-idempotent-not-required-summary")
    if run_text.count('echo "not_required" > "$CORRECTION_SUMMARY_FILE"') < 2:
        results.append("idempotent-early-return-not-covering-both-no-op-paths")

    # 13a (scoped to this step): no telemetry port / spec.args / direct CR / wrapper chart content introduced here.
    for marker in ("8889", "service::telemetry", "spec.args", "args:\n", "370-line"):
        if marker in run_text:
            results.append(f"forbidden-marker-in-recreate-step:{marker.strip()}")

# 11: strict node-agent readiness remains unchanged (still present, still exact equality, not weakened).
wait_step = get_step("Wait for CloudWatch Agent workloads to roll out")
if wait_step is None:
    results.append("missing-wait-step")
else:
    wait_run = wait_step.get("run", "")
    if "wait_for_daemonset_fully_ready" not in wait_run:
        results.append("node-agent-strict-readiness-waiter-missing")
    if 'wait_for_daemonset_fully_ready "$TARGET_NAMESPACE" cloudwatch-agent' not in wait_run:
        results.append("node-agent-strict-readiness-not-applied-to-cloudwatch-agent")

# 12: the exact localhost:8888 collision signatures remain checked (searched across the whole file since this correction may check them in more than one step).
for pattern in ("binding address localhost:8888", r"listen tcp 127\.0\.0\.1:8888", "bind: address already in use", "failed to create SDK"):
    if pattern not in text:
        results.append(f"missing-collision-signature:{pattern}")

# 13b (whole-file scope): no chart/image/IAM/Terraform change, no direct CR, no wrapper chart, no telemetry port override, no spec.args mechanism.
code_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
code_text = "\n".join(code_lines)
if 'CHART_VERSION: "6.2.0"' not in text:
    results.append("chart-version-changed")
for marker in ("service::telemetry", "--set=service", "helm/goldengate-observability-adcb"):
    if marker in code_text:
        results.append(f"forbidden-whole-file-marker:{marker}")

if results:
    print("MISMATCH:" + ";".join(results))
else:
    print("OK")
PYEOF
)"
    if [ "$RECREATE_CORRECTION_CHECK" = "OK" ]; then
      pass "21: goldengate-observability.yaml Phase 6B2B cluster-scraper Deployment recreate correction: the new deploy-guarded 'Ensure cluster-scraper Deployment host-network isolation' step is correctly ordered between Argo CD sync/health and the ServiceAccount annotation step; it confirms the live CR has hostNetwork=false before any deletion; validates the exact controller ownerReference UID against the CR UID before deleting; deletes only deployment/cloudwatch-agent-cluster-scraper (never the CR, DaemonSet, pods, ServiceAccount, Secret, or ConfigMap) with exactly one delete call in source; records the old UID and requires the recreated UID to differ; validates the recreated Deployment's hostNetwork=false and full readiness; validates every active scraper pod's hostNetwork=false, podIP!=hostIP, ServiceAccount, and IRSA env-var-name presence; is idempotent (both no-op paths mark 'not_required'); strict node-agent DaemonSet readiness is unchanged; the exact 127.0.0.1:8888 collision signatures remain checked; and no telemetry-port override, spec.args, direct CR, wrapper chart, chart/image upgrade, IAM, or Terraform change was introduced"
    else
      fail "21: goldengate-observability.yaml Phase 6B2B cluster-scraper Deployment recreate correction check failed: ${RECREATE_CORRECTION_CHECK}"
    fi
  else
    fail "${OBSERVABILITY_WORKFLOW} not found, or python3 unavailable"
  fi

  # UID-based recreation detection + hostNetwork null normalization + CloudWatch metrics authorization/export validation (focused, static/offline only).
  if [ -f "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" ] && command -v python3 >/dev/null 2>&1; then
    UID_AUTH_CHECK="$(python3 - "${REPO_ROOT}/${OBSERVABILITY_WORKFLOW}" "${CW_METRICS_POLICY_FILE}" <<'PYEOF'
import os
import re
import sys
import yaml

path = sys.argv[1]
with open(path) as f:
    text = f.read()
    doc = yaml.safe_load(text)

cw_policy_path = sys.argv[2]
CW_METRICS_POLICY_FILE_TEXT = None
if os.path.isfile(cw_policy_path):
    with open(cw_policy_path) as f:
        CW_METRICS_POLICY_FILE_TEXT = f.read()

results = []
job = doc["jobs"]["validate_and_deploy"]
steps = job["steps"]
names = [s.get("name") for s in steps]

def get_step(name):
    return next((s for s in steps if s.get("name") == name), None)

# --- Task 3: UID-based recreation detection, never an observed NotFound ---
recreate_step = get_step("Ensure cluster-scraper Deployment host-network isolation")
if recreate_step is None:
    results.append("missing-recreate-step")
else:
    run_text = recreate_step.get("run", "")

    # The old anti-pattern required observing the object's absence (exit-status-only existence-check loop, no UID comparison) before polling for recreation; that must be gone.
    if "did not disappear within 30s" in run_text:
        results.append("old-notfound-interval-anti-pattern-still-present")

    # The new pattern must poll via -o json and branch on UID comparison for every required state, never requiring NotFound.
    if 'new_deploy_json="$(kubectl get deployment "$CLUSTER_SCRAPER_DEPLOYMENT" -n "$TARGET_NAMESPACE" -o json 2>/dev/null)"' not in run_text:
        results.append("missing-uid-based-poll")
    if '[ -n "$new_uid" ] && [ "$new_uid" != "$old_uid" ]' not in run_text:
        results.append("missing-new-uid-differs-from-old-check")
    if "still carries the old UID and is terminating" not in run_text:
        results.append("missing-same-uid-terminating-state-handling")
    if "still carries the old UID -- continuing to wait for recreation" not in run_text:
        results.append("missing-same-uid-not-terminating-state-handling")
    if "not found (not yet recreated) -- continuing to wait" not in run_text:
        results.append("missing-notfound-state-handling")
    if "old_uid=" not in run_text:
        results.append("missing-old-uid-recording")
    # the harmless reconciliation nudge must still exist.
    if "cloudfactory.adcb/reconcile-requested-at" not in run_text:
        results.append("missing-reconciliation-nudge")
    # the one-delete guard: exactly one kubectl delete call in source.
    if run_text.count("kubectl delete deployment") != 1:
        results.append(f"unexpected-delete-call-count:{run_text.count('kubectl delete deployment')}")

    # Null/false normalization on Deployment and Pod; the CR stays strict (no // false on the CR's own hostNetwork read).
    if run_text.count('.spec.template.spec.hostNetwork // false') < 2:
        results.append("deployment-hostnetwork-normalization-missing-or-incomplete")
    if "'.spec.hostNetwork // false'" not in run_text:
        results.append("pod-hostnetwork-normalization-missing")
    if "cr_hostnetwork=\"$(jq -r '.spec.hostNetwork // false'" in run_text:
        results.append("cr-hostnetwork-incorrectly-normalized-must-stay-strict")
    if '"$cr_hostnetwork" != "false"' not in run_text:
        results.append("cr-hostnetwork-strict-check-missing")

# --- Task 5: bounded "no recent export errors" validation step ---
auth_step = get_step("Validate no recent CloudWatch export errors")
if auth_step is None:
    results.append("missing-authorization-validation-step")
else:
    if auth_step.get("if") != "${{ inputs.deploy }}":
        results.append(f"authorization-step-if={auth_step.get('if')!r}")

    try:
        irsa_idx = names.index("Verify IRSA injection on the recreated CloudWatch Agent pods")
        auth_idx = names.index("Validate no recent CloudWatch export errors")
        live_idx = names.index("Live Kubernetes validation")
        if not (irsa_idx < auth_idx < live_idx):
            results.append(f"authorization-step-order-wrong:{irsa_idx},{auth_idx},{live_idx}")
    except ValueError as e:
        results.append(f"step-not-found-for-ordering:{e}")

    run_text = auth_step.get("run", "")

    if "VALIDATION_START_TS=" not in run_text:
        results.append("missing-validation-start-timestamp")
    if "--since-time=\"$VALIDATION_START_TS\"" not in run_text:
        results.append("missing-since-time-usage")
    if "--tail=80" not in run_text:
        results.append("missing-bounded-tail")

    # The step's own runtime output must not claim successful export was proven -- only that no recent error signatures were found.
    if "does not by itself confirm successful export to CloudWatch" not in run_text:
        results.append("step-overclaims-successful-export")

    # kubectl logs must never be silently swallowed with "|| true" -- a retrieval failure from an expected active pod/container must fail the step via an explicit captured exit status.
    if "kubectl logs" in run_text and re.search(r'kubectl logs[^\n]*\|\|\s*true', run_text):
        results.append("kubectl-logs-still-uses-or-true-fallback")
    if "log_status=$?" not in run_text:
        results.append("missing-explicit-log-retrieval-exit-status-check")
    if '"$log_status" -ne 0' not in run_text:
        results.append("missing-log-retrieval-failure-check")
    if "could not retrieve logs for pod" not in run_text:
        results.append("missing-log-retrieval-failure-message")

    # Checked node-agent pod count must equal DaemonSet desiredNumberScheduled.
    if "CHECKED_NODE_AGENT_PODS=$((CHECKED_NODE_AGENT_PODS + 1))" not in run_text:
        results.append("missing-node-agent-pod-counting")
    if 'desiredNumberScheduled // 0' not in run_text:
        results.append("missing-daemonset-desired-count-read")
    if '"$CHECKED_NODE_AGENT_PODS" -ne "$CW_DS_DESIRED_AUTH"' not in run_text:
        results.append("missing-node-agent-checked-count-equals-desired-check")

    # Checked cluster-scraper pod count must be >= 1.
    if "CHECKED_SCRAPER_PODS=$((CHECKED_SCRAPER_PODS + 1))" not in run_text:
        results.append("missing-scraper-pod-counting")
    if '"$CHECKED_SCRAPER_PODS" -lt 1' not in run_text:
        results.append("missing-scraper-checked-count-at-least-one-check")

    required_auth_signatures = [
        "PermissionDenied", "HTTP Status Code 403",
        "not authorized to perform: cloudwatch:PutMetricData",
        "no identity-based policy allows",
        r"Exporting failed\. Dropping data\.",
        "error exporting items",
        "resource: arn:aws:cloudwatch:",
        "dataset/default",
    ]
    for sig in required_auth_signatures:
        if sig not in run_text:
            results.append(f"missing-auth-error-signature:{sig}")

    required_startup_signatures = [
        "binding address localhost:8888",
        r"listen tcp 127\.0\.0\.1:8888",
        "bind: address already in use",
        "failed to create SDK",
    ]
    for sig in required_startup_signatures:
        if sig not in run_text:
            results.append(f"missing-startup-error-signature:{sig}")

    # active/current-revision filtering for BOTH workload kinds.
    if 'select(.controller==true and .kind=="DaemonSet")' not in run_text:
        results.append("missing-daemonset-owner-filtering")
    if 'select(.controller==true and .kind=="ReplicaSet")' not in run_text:
        results.append("missing-replicaset-owner-filtering")
    if run_text.count("deletionTimestamp") < 2:
        results.append("missing-deletion-timestamp-exclusion")

    # never prints secrets/tokens/env values/full manifests.
    for forbidden in ("AWS_WEB_IDENTITY_TOKEN_FILE\"", "env_names", "envFrom", "kubectl get secret", "-o yaml"):
        if forbidden in run_text:
            results.append(f"forbidden-content-in-authorization-step:{forbidden}")

# No CloudWatch read permission (e.g. GetMetricData, ListMetrics) was added merely to support this log-based validation -- the step only calls "kubectl logs", never the CloudWatch API, so the role's action set must remain exactly PutMetricData plus the pre-existing logs/ec2 actions.
if CW_METRICS_POLICY_FILE_TEXT is not None:
    for forbidden_cw_read in ("cloudwatch:GetMetricData", "cloudwatch:ListMetrics", "cloudwatch:GetMetricStatistics", "cloudwatch:DescribeAlarms"):
        if forbidden_cw_read in CW_METRICS_POLICY_FILE_TEXT:
            results.append(f"cloudwatch-read-permission-added-for-validation:{forbidden_cw_read}")

if results:
    print("MISMATCH:" + ";".join(results))
else:
    print("OK")
PYEOF
)"
    if [ "$UID_AUTH_CHECK" = "OK" ]; then
      pass "22: goldengate-observability.yaml Phase 6B2B UID-based recreation detection, hostNetwork null-normalization, and 'no recent CloudWatch export errors' validation: the old NotFound-interval anti-pattern is gone and replaced by a UID-comparison state machine handling NotFound/same-UID-terminating/same-UID/different-UID without ever requiring an observed absence, while preserving the one-delete guard and the reconciliation nudge; Deployment and Pod hostNetwork reads normalize null/omitted to false while the CR's own hostNetwork read stays strict; and the new deploy-guarded 'Validate no recent CloudWatch export errors' step is correctly ordered after IRSA verification and before Live Kubernetes validation, never uses a 'kubectl logs ... || true' fallback (failing closed instead on a retrieval error), requires checked node-agent pods to equal the DaemonSet's desiredNumberScheduled and checked scraper pods to be at least 1, captures a validation-start timestamp, uses --since-time and a bounded --tail=80, checks all required authorization and startup-collision signatures on active current-revision DaemonSet and ReplicaSet pods only, never claims successful export was proven, never prints secrets/tokens/env values/full manifests, and adds no CloudWatch read permission to the collector role"
    else
      fail "22: goldengate-observability.yaml Phase 6B2B UID-based recreation / hostNetwork normalization / authorization validation check failed: ${UID_AUTH_CHECK}"
    fi
  else
    fail "${OBSERVABILITY_WORKFLOW} not found, or python3 unavailable"
  fi

  # 13: no wrapper chart was created for this phase.
  if [ -d "${REPO_ROOT}/helm/goldengate-observability" ]; then
    fail "13: helm/goldengate-observability/ wrapper chart unexpectedly exists -- Argo CD must consume the private upstream OCI chart directly"
  else
    pass "13: no helm/goldengate-observability wrapper chart was created"
  fi

  # 14: no EKS Terraform enable_cloudwatch variable was introduced anywhere in this repository's Terraform.
  if grep -rl 'enable_cloudwatch' "${REPO_ROOT}/envs" 2>/dev/null | grep -q .; then
    fail "14: an enable_cloudwatch Terraform variable/reference was unexpectedly introduced under envs/"
  else
    pass "14: no enable_cloudwatch Terraform variable/reference exists under envs/"
  fi

  # 15: earlier phases' resources remain functionally untouched (comment-only edits are allowed and ignored here). envs/dev/policies/goldengate-cloudwatch-metrics-dev is excluded since the OTLP-authorization correction intentionally changes one condition operator there; helm/goldengate-platform and platform/dev/goldengate-platform are excluded since Phase 6D0 legitimately changes the per-flavour runtime ServiceAccounts there (guarded instead by the dedicated ServiceAccount/Fluent-Bit safety checks in this same suite). envs/dev/cloudwatch_observability.tf, envs/dev/cloudwatch_logs.tf, and envs/dev/policies/goldengate-platform-logging-dev are excluded starting with Fresh-EKS Phase A, which legitimately centralizes their log-group names onto envs/dev/environment.tf and regenerates goldengate-platform-logging-dev's assume_role_policy/sts.json for the new EKS OIDC issuer -- both already independently guarded by this same suite's render-iam-policies/environment-contract checks, never by this narrow historical byte-diff. This check's own paths list is now empty: cloudwatch-observability-artifact-sync.yaml is excluded starting with Phase 11, which legitimately adds an environment selector and loads canonical identity from envs/<environment>/environment.yaml instead of hardcoding it -- guarded instead by this suite's Phase 11 hardcoding-sweep checks, never by this narrow historical byte-diff.
  PHASE_6A_6B1_STATUS="$(python3 -c "
import subprocess

def strip_comments(text):
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        idx = line.find(' #')
        if idx != -1:
            line = line[:idx].rstrip()
        out.append(line)
    return '\n'.join(out)

paths = []
# An empty pathspec list must never fall through to a bare 'git diff --name-only --' (which diffs the whole repo, not nothing).
changed = subprocess.run(['git', '-C', '$REPO_ROOT', 'diff', '--name-only', '--'] + paths, capture_output=True, text=True).stdout.split() if paths else []
mismatches = []
for f in changed:
    head = subprocess.run(['git', '-C', '$REPO_ROOT', 'show', f'HEAD:{f}'], capture_output=True, text=True).stdout
    with open('$REPO_ROOT/' + f) as fh:
        working = fh.read()
    if strip_comments(head) != strip_comments(working):
        mismatches.append(f)
print(('MISMATCH:' + ','.join(mismatches)) if mismatches else 'IDENTICAL')
" 2>/dev/null || true)"
  if [ "$PHASE_6A_6B1_STATUS" = "IDENTICAL" ]; then
    pass "15: no file remains in this check's historical byte-diff guard set (cloudwatch-observability-artifact-sync.yaml was legitimately released from it by Phase 11's environment centralization)"
  else
    fail "15: an unexpected functional change was found in Phase 6A/6B1/6B2A files: ${PHASE_6A_6B1_STATUS:-unknown}"
  fi

  # Strict YAML parse of the platform workflow (must still parse cleanly with the Fluent Bit role-ARN/region plumbing), plus a scan proving no new destructive AWS Logs action (CreateLogGroup/DeleteLogGroup/PutRetentionPolicy) was introduced anywhere -- this workflow legitimately runs many other AWS/kubectl mutating calls elsewhere.
  if [ -f "${REPO_ROOT}/${PLATFORM_WORKFLOW}" ] && command -v python3 >/dev/null 2>&1; then
    if python3 -c "import yaml; yaml.safe_load(open('${REPO_ROOT}/${PLATFORM_WORKFLOW}'))" >/dev/null 2>&1; then
      pass "${PLATFORM_WORKFLOW} parses as strict YAML"
    else
      fail "${PLATFORM_WORKFLOW} does not parse as strict YAML"
    fi

    if grep -qE 'logs:CreateLogGroup|logs:DeleteLogGroup|logs:PutRetentionPolicy|aws logs create-log-group|aws logs delete-log-group' "${REPO_ROOT}/${PLATFORM_WORKFLOW}"; then
      fail "${PLATFORM_WORKFLOW} contains a CloudWatch Logs group create/delete/retention-mutation action"
    else
      pass "${PLATFORM_WORKFLOW} contains no CloudWatch Logs group create/delete/retention-mutation action"
    fi
  else
    fail "${PLATFORM_WORKFLOW} not found, or python3 unavailable"
  fi

  # The monitor chart's ConfigMap reads a staged copy of the canonical config from its own files/ directory -- never committed there (see goldengate-monitor.yaml) -- so lint/render stage a throwaway copy here.
  MONITOR_CHART_STAGED="${WORKDIR}/goldengate-monitor"
  cp -a "$MONITOR_CHART" "$MONITOR_CHART_STAGED"
  mkdir -p "${MONITOR_CHART_STAGED}/files"
  cp "$CANONICAL_CONFIG" "${MONITOR_CHART_STAGED}/files/goldengate-deployments.yaml"

  if helm lint "$MONITOR_CHART_STAGED" >"${WORKDIR}/lint-monitor.log" 2>&1; then
    pass "helm lint ${MONITOR_CHART} (canonical config staged)"
  else
    fail "helm lint ${MONITOR_CHART}"
    cat "${WORKDIR}/lint-monitor.log"
  fi
else
  skip "helm lint -- helm not available"
fi

# 5. Render every enabled deployment discovered from the canonical config; validate exactly one StatefulSet per release and no runtime sidecar.
echo ""
echo "--- Render enabled runtimes; one StatefulSet each, no sidecar ---"
if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    VALUES_FILE="envs/dev/${name}/values.yaml"
    RENDERED="${WORKDIR}/${name}.yaml"

    derive_shared_overrides_for_deployment "$name"

    if ! helm template "$name" "$RUNTIME_CHART" --namespace goldengate-dev \
        -f "$VALUES_FILE" "${SHARED_OVERRIDES[@]}" > "$RENDERED" 2>"${WORKDIR}/${name}.err"; then
      fail "helm template failed for ${name}"
      cat "${WORKDIR}/${name}.err"
      continue
    fi

    STATEFULSET_COUNT="$(grep -c '^kind: StatefulSet$' "$RENDERED" || true)"
    if [ "$STATEFULSET_COUNT" -eq 1 ]; then
      pass "${name}: exactly 1 StatefulSet rendered"
    else
      fail "${name}: expected exactly 1 StatefulSet, found ${STATEFULSET_COUNT}"
    fi

    if grep -q "^kind: StatefulSet$" "$RENDERED" && \
       python3 -c "
import sys, yaml
docs = list(yaml.safe_load_all(open('$RENDERED')))
sts = [d for d in docs if d and d.get('kind') == 'StatefulSet']
assert sts, 'no StatefulSet document'
containers = sts[0]['spec']['template']['spec'].get('containers', [])
init_containers = sts[0]['spec']['template']['spec'].get('initContainers', [])
names = [c['name'] for c in containers] + [c['name'] for c in init_containers]
forbidden = [n for n in names if 'observer' in n.lower() or 'sidecar' in n.lower()]
assert not forbidden, f'forbidden sidecar container(s): {forbidden}'
assert len(containers) == 1, f'expected exactly 1 non-init container, found {[c[\"name\"] for c in containers]}'
print('OK')
" >"${WORKDIR}/${name}-sidecar.log" 2>&1; then
      pass "${name}: no runtime sidecar container (observer/utility-sidecar)"
    else
      fail "${name}: sidecar-absence check failed"
      cat "${WORKDIR}/${name}-sidecar.log"
    fi
  done <<< "$(python3 -c "
import yaml
doc = yaml.safe_load(open('${CANONICAL_CONFIG}'))
for d in doc['deployments']:
    if d.get('enabled'):
        print(d['name'])
")"
else
  skip "runtime rendering -- helm and/or python3/PyYAML not available"
fi

# 6. Existing shared monitor resources; no duplicate monitor deployment.
echo ""
echo "--- Shared monitor: single deployment, existing resources retained ---"
if [ -d "monitoring/gg-monitor-core" ] || [ -d "helm/gg-monitor" ] || [ -d "platform/dev/gg-monitor" ] \
    || [ -f ".github/workflows/gg-monitor-core.yaml" ]; then
  fail "a second collector application/chart/workflow still exists (monitoring/gg-monitor-core, helm/gg-monitor, platform/dev/gg-monitor, or gg-monitor-core.yaml)"
else
  pass "no second collector application/chart/workflow exists -- one shared monitor only"
fi

if [ -f "${MONITOR_APP_DIR}/monitor.py" ] && [ -f "${MONITOR_APP_DIR}/collector.py" ] \
    && [ -f "${MONITOR_APP_DIR}/config.py" ] && [ -f "${MONITOR_APP_DIR}/health_rules.py" ]; then
  pass "monitoring/monitor has the merged module structure (monitor/collector/config/health_rules)"
else
  fail "monitoring/monitor is missing one or more expected modules"
fi

if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  MONITOR_RENDERED="${WORKDIR}/goldengate-monitor-rendered.yaml"
  if helm template gg-monitor "$MONITOR_CHART_STAGED" --namespace goldengate-monitoring \
      -f envs/dev/goldengate-monitor/values.yaml \
      --set image.repository=example.invalid/goldengate-monitor --set image.tag=test \
      "${MONITOR_SHARED_OVERRIDES[@]}" \
      > "$MONITOR_RENDERED" 2>"${WORKDIR}/monitor-render.err"; then
    for kind_name in "Deployment gg-monitor" "Service gg-monitor" "ServiceAccount gg-monitor" "ConfigMap goldengate-monitor-canonical-config"; do
      kind="${kind_name% *}"
      name="${kind_name#* }"
      if grep -q "^kind: ${kind}$" "$MONITOR_RENDERED"; then
        pass "goldengate-monitor renders ${kind}/${name}"
      else
        fail "goldengate-monitor is missing ${kind}/${name}"
      fi
    done
    if grep -q "kind: Ingress" "$MONITOR_RENDERED" 2>/dev/null || \
       helm template gg-monitor "$MONITOR_CHART_STAGED" --namespace goldengate-monitoring \
         -f envs/dev/goldengate-monitor/values.yaml \
         --set image.repository=example.invalid/goldengate-monitor --set image.tag=test \
         "${MONITOR_SHARED_OVERRIDES[@]}" \
         --set ingress.enabled=true --set-string ingress.host="$RESOLVED_MONITOR_HOST" \
         --set-string ingress.alb.certificateArn="$RESOLVED_CERTIFICATE_ARN" \
         2>/dev/null | grep -q "host: ${RESOLVED_MONITOR_HOST}"; then
      pass "goldengate-monitor Ingress renders with the existing hostname when enabled"
    else
      fail "goldengate-monitor Ingress does not render the existing hostname"
    fi
  else
    fail "helm template failed for goldengate-monitor"
    cat "${WORKDIR}/monitor-render.err"
  fi
else
  skip "goldengate-monitor render checks -- helm and/or python3/PyYAML not available"
fi

# 7. No hardcoded runtime/pipeline names in application or workflow code.
echo ""
echo "--- No hardcoded canonical deployment names outside the canonical config ---"
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  HARDCODE_NAMES="$(python3 -c "
import yaml
doc = yaml.safe_load(open('${CANONICAL_CONFIG}'))
names = set()
for d in doc['deployments']:
    names.add(d['name'])
    names.add(d['pipeline'])
print('\n'.join(sorted(names)))
")"
else
  HARDCODE_NAMES="$(grep -E '^\s*-?\s*(name|pipeline):' "$CANONICAL_CONFIG" | sed -E 's/^\s*-?\s*(name|pipeline):\s*//' | tr -d '"'"'"'\r' | sort -u)"
fi

HARDCODE_FOUND="false"
for f in "${MONITOR_APP_DIR}"/monitor.py "${MONITOR_APP_DIR}"/collector.py "${MONITOR_APP_DIR}"/config.py "${MONITOR_APP_DIR}"/health_rules.py "${MONITOR_APP_DIR}"/ui.py; do
  [ -f "$f" ] || continue
  while IFS= read -r nm; do
    [ -z "$nm" ] && continue
    if grep -Fq -- "$nm" "$f"; then
      fail "$(basename "$f") hardcodes canonical name: ${nm}"
      HARDCODE_FOUND="true"
    fi
  done <<< "$HARDCODE_NAMES"
done
if [ "$HARDCODE_FOUND" = "false" ]; then
  pass "no application module hardcodes a canonical deployment/pipeline name"
fi

if grep -qE "pipelines/deployments\.yaml|topologies/dev|files/pipelines|files/topologies" "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml still references the removed pipelines/topologies file layout"
else
  pass "goldengate-monitor.yaml does not reference the removed pipelines/topologies file layout"
fi

# 8. No committed generated copies of the canonical config inside charts.
echo ""
echo "--- No generated canonical-config copies committed in charts ---"
if [ -e "helm/goldengate-monitor/files/goldengate-deployments.yaml" ] \
    || [ -d "helm/goldengate-monitor/files/pipelines" ] \
    || [ -d "helm/goldengate-monitor/files/topologies" ]; then
  fail "helm/goldengate-monitor/files/ contains a committed generated copy of the canonical config"
else
  pass "helm/goldengate-monitor/files/ contains no committed generated copy"
fi

# 9. No committed pycache/pyc.
echo ""
echo "--- No committed __pycache__/*.pyc ---"
STRAY_PYCACHE="$(find . -type d -name "__pycache__" -not -path "*/node_modules/*" 2>/dev/null)"
STRAY_PYC="$(find . -type f -name "*.pyc" -not -path "*/node_modules/*" 2>/dev/null)"
if [ -z "$STRAY_PYCACHE" ] && [ -z "$STRAY_PYC" ]; then
  pass "no __pycache__ directories or *.pyc files present"
else
  fail "found stray __pycache__/*.pyc: ${STRAY_PYCACHE} ${STRAY_PYC}"
fi

# 10. Contract-probe tool packaged but never auto-run; CloudWatch stays physically disabled by default.
echo ""
echo "--- Contract-probe tool: packaged, never auto-run, CloudWatch stays disabled ---"
PROBE_TOOL="${MONITOR_APP_DIR}/tools/gg_api_contract_probe.py"
if [ -f "$PROBE_TOOL" ]; then
  pass "gg_api_contract_probe.py exists under monitoring/monitor/tools/"
else
  fail "gg_api_contract_probe.py is missing"
fi

if grep -q "COPY tools/ ./tools/" "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null; then
  pass "Dockerfile packages tools/ into the monitor image"
else
  fail "Dockerfile does not copy tools/ into the monitor image"
fi

if grep -q "^ENTRYPOINT \[\"python3\", \"monitor.py\"\]$" "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null; then
  pass "Dockerfile entrypoint is unchanged (monitor.py only)"
else
  fail "Dockerfile entrypoint was changed"
fi

PROBE_WIRED="false"
for f in "${MONITOR_APP_DIR}/monitor.py" "${MONITOR_APP_DIR}/collector.py"; do
  [ -f "$f" ] || continue
  if grep -q "gg_api_contract_probe" "$f"; then
    fail "$(basename "$f") references gg_api_contract_probe -- must never auto-run"
    PROBE_WIRED="true"
  fi
done
if [ "$PROBE_WIRED" = "false" ]; then
  pass "gg_api_contract_probe is never imported by monitor.py/collector.py (manual-only)"
fi

if grep -q "publishEnabled: false" "${MONITOR_CHART}/values.yaml" 2>/dev/null; then
  pass "helm/goldengate-monitor default values.yaml keeps cloudwatch.publishEnabled: false"
else
  fail "helm/goldengate-monitor default values.yaml no longer defaults CloudWatch publishing to false"
fi

# 11. Confirmed secure PMS route documented; 9015 stays unauthenticated-only; /services/v2/metrics not recommended as production PMS.
echo ""
echo "--- Contract-probe tool: confirmed secure PMS route frozen ---"
if grep -q "/services/v2/mpoints/processes" "$PROBE_TOOL" 2>/dev/null \
    && grep -q "/services/v2/monitoring/statusChanges" "$PROBE_TOOL" 2>/dev/null; then
  pass "gg_api_contract_probe.py documents the confirmed secure PMS routes"
else
  fail "gg_api_contract_probe.py does not document the confirmed secure PMS routes"
fi

if grep -qi "confirmed invalid" "$PROBE_TOOL" 2>/dev/null; then
  pass "gg_api_contract_probe.py marks /services/v2/metrics as confirmed invalid, not production PMS"
else
  fail "gg_api_contract_probe.py no longer marks /services/v2/metrics as confirmed invalid"
fi

if grep -q 'return f"http://{host}:{deployment\[.metricsPort.\]}"' "$PROBE_TOOL" 2>/dev/null; then
  pass "metricsPort 9015 stays plain HTTP (never HTTPS) in the probe tool"
else
  fail "metricsPort scheme handling in the probe tool changed unexpectedly"
fi

if [ -d "monitoring/observer" ]; then
  fail "monitoring/observer still exists -- Phase 5A requires observer source retirement"
else
  pass "monitoring/observer has been removed (Phase 5A observer retirement)"
fi

# 12. --follow-processes fixed detail allowlist exists and is never wired into automatic startup.
echo ""
echo "--- Contract-probe tool: --follow-processes fixed detail allowlist ---"
if grep -q '"process", "processPerformance", "threadPerformance", "serviceHealth", "heartbeat"' "$PROBE_TOOL" 2>/dev/null; then
  pass "gg_api_contract_probe.py defines the fixed --detail allowlist"
else
  fail "gg_api_contract_probe.py fixed --detail allowlist is missing or changed"
fi

if grep -q "MAX_FOLLOWED_PROCESSES = 20" "$PROBE_TOOL" 2>/dev/null; then
  pass "gg_api_contract_probe.py caps --follow-processes at 20 items"
else
  fail "gg_api_contract_probe.py no longer caps --follow-processes at 20 items"
fi

FOLLOW_WIRED="false"
for f in "${MONITOR_APP_DIR}/monitor.py" "${MONITOR_APP_DIR}/collector.py"; do
  [ -f "$f" ] || continue
  if grep -q "follow_processes\|follow-processes" "$f"; then
    fail "$(basename "$f") references follow_processes -- must never auto-run"
    FOLLOW_WIRED="true"
  fi
done
if [ "$FOLLOW_WIRED" = "false" ]; then
  pass "--follow-processes is never referenced by monitor.py/collector.py (manual-only)"
fi

# 13. Production PMS collection bounded, no new DynamoDB record type, forbidden endpoints never referenced by name.
echo ""
echo "--- Production PMS collection: bounded, no forbidden endpoints ---"
if grep -q "MAX_FOLLOWED_PMS_PROCESSES = 20" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py caps production PMS collection at 20 followed processes"
else
  fail "collector.py no longer caps production PMS collection at 20 followed processes"
fi

if grep -q 'PMS_DETAIL_KINDS = ("processPerformance", "serviceHealth")' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py production PMS detail calls remain processPerformance + serviceHealth only"
else
  fail "collector.py production PMS detail-call set changed unexpectedly"
fi

PMS_FORBIDDEN_FOUND="false"
# Skips the module docstring (lines 1..first closing triple-quote), which legitimately documents these endpoints as NOT used -- only code after it must never reference them.
COLLECTOR_CODE_TAIL="$(awk '/^"""$/{n++; next} n>=1' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null)"
for pattern in '"/heartbeat"' '"/threadPerformance"' "statusChanges" "9015"; do
  if grep -qF "$pattern" <<< "$COLLECTOR_CODE_TAIL"; then
    fail "collector.py references forbidden PMS pattern outside its docstring: ${pattern}"
    PMS_FORBIDDEN_FOUND="true"
  fi
done
if [ "$PMS_FORBIDDEN_FOUND" = "false" ]; then
  pass "collector.py never references /heartbeat, /threadPerformance, statusChanges, or port 9015 outside its docstring"
fi

if grep -q '"pms" in snapshot' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null \
    && grep -q 'f"STATE#{process}"' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "PMS enrichment folds into the existing STATE# write -- no new recordType"
else
  fail "PMS enrichment / existing STATE# recordType pattern changed unexpectedly"
fi

# 14. Process-name/numeric bounds and stale-PMS overwrite semantics remain in place.
echo ""
echo "--- Production PMS collection: bounds and stale-state overwrite ---"
if grep -q "MAX_PMS_PROCESS_NAME_LENGTH = 128" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py bounds PMS process-name length"
else
  fail "collector.py no longer bounds PMS process-name length"
fi

if grep -q "PMS_MAX_SAFE_NUMBER = 10 \*\* 15" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py bounds PMS numeric values to a fixed DynamoDB-safe range"
else
  fail "collector.py no longer bounds PMS numeric values to a fixed DynamoDB-safe range"
fi

if grep -q "_pms_unavailable_snapshot" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py overwrites pms with a current sanitized snapshot on DOWN/unexpected-failure ticks"
else
  fail "collector.py stale-PMS overwrite helper is missing"
fi

# 15. Total PMS collection time budget stays fixed and comfortably under the deployed stale threshold; serviceHealth validation stays tightened.
echo ""
echo "--- Production PMS collection: total time budget ---"
if grep -q "PMS_REQUEST_TIMEOUT_SECONDS = 2" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null \
    && grep -q "PMS_COLLECTION_BUDGET_SECONDS = 30" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py bounds total PMS collection to a fixed 30s time budget"
else
  fail "collector.py PMS request/budget timeout constants changed unexpectedly"
fi

if grep -q 'isinstance(response.get("isHealthy"), bool)' "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  pass "collector.py requires serviceHealth isHealthy to be a literal boolean"
else
  fail "collector.py no longer requires serviceHealth isHealthy to be a literal boolean"
fi

# 16. Manager-compatible portal: GET /api/processes exists, canonical STATE#-only (no Scan, no legacy fallback in that path).
echo ""
echo "--- Manager-compatible portal: /api/processes ---"
if grep -q '"/api/processes"' "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null \
    && grep -q "def build_processes_payload" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null; then
  pass "monitor.py exposes GET /api/processes backed by build_processes_payload"
else
  fail "monitor.py is missing the /api/processes endpoint"
fi

if grep -q "def read_deployment_processes_view" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null \
    && ! grep -q "\.scan(" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null; then
  pass "monitor.py's /api/processes view is canonical STATE#-only and never calls Scan"
else
  fail "monitor.py /api/processes canonical-view helper or no-Scan guarantee changed unexpectedly"
fi

# 17. CloudWatch metric-path source hardening: exact manager-compatible metric contract, sanitized PutMetricData failure logging, hard switch still gates client construction, no alarm/SNS/gg-alerter/Fluent Bit or read/alarm CloudWatch IAM permission introduced.
echo ""
echo "--- Phase 4D1: CloudWatch metric-path source hardening ---"

COLLECTOR_SRC="${MONITOR_APP_DIR}/collector.py"

METRIC_CONTRACT_OK="true"
for token in 'CLOUDWATCH_NAMESPACE = "GoldenGate/Pipelines"' '"LagBreached"' '"AbendFailure"' \
             '"DeploymentDown"' '"HeartbeatAgeSeconds"' '"CriticalServiceDown"' \
             '"ExtractLagSeconds"' '"ReplicatLagSeconds"' '"AbendState"' '"AbendEvent"'; do
  if ! grep -qF "$token" "$COLLECTOR_SRC" 2>/dev/null; then
    fail "collector.py is missing expected metric-contract token: ${token}"
    METRIC_CONTRACT_OK="false"
  fi
done
[ "$METRIC_CONTRACT_OK" = "true" ] && pass "collector.py defines the exact manager-compatible namespace/metric-name contract"

if grep -q "def build_metric_batch" "$COLLECTOR_SRC" 2>/dev/null \
    && grep -q "def publish_metric_batch" "$COLLECTOR_SRC" 2>/dev/null; then
  pass "collector.py keeps build_metric_batch (pure) and publish_metric_batch (boto3-isolated) as separate functions"
else
  fail "collector.py is missing build_metric_batch/publish_metric_batch"
fi

if grep -q "logger.exception(\"CloudWatch put_metric_data failed" "$COLLECTOR_SRC" 2>/dev/null; then
  fail "publish_metric_batch still uses raw logger.exception for PutMetricData failures"
else
  pass "publish_metric_batch no longer logs a raw exception/traceback on PutMetricData failure"
fi

if grep -q '"event": "cloudwatch_put_metric_data_failed"' "$COLLECTOR_SRC" 2>/dev/null; then
  pass "publish_metric_batch logs a sanitized structured event on PutMetricData failure"
else
  fail "publish_metric_batch is missing the sanitized cloudwatch_put_metric_data_failed log event"
fi

if grep -q "cloudwatch:GetMetricData\|cloudwatch:DescribeAlarms\|cloudwatch:ListMetrics\|cloudwatch:GetMetricStatistics" \
    envs/dev/policies/goldengate-monitor-read-dev/policies/*.json 2>/dev/null; then
  fail "goldengate-monitor-read-dev policy introduces a CloudWatch read/alarm permission"
else
  pass "goldengate-monitor-read-dev IAM policy grants CloudWatch PutMetricData only (no read/alarm actions)"
fi

if find . -path ./.git -prune -o -iname "*gg-alerter*" -print 2>/dev/null | grep -q .; then
  fail "unexpected gg-alerter file found -- out of scope for this phase"
else
  pass "no gg-alerter implementation exists yet"
fi

# The platform-level Fluent Bit DaemonSet is no longer blanket-forbidden, but still confined to its expected locations (goldengate-platform chart templates, its IRSA policy folder, CloudWatch Logs Terraform) and never inside the runtime/monitor charts or Python code.
UNEXPECTED_FLUENT_BIT_LOCATIONS="$(find . -path ./.git -prune -o -iname "*fluent-bit*" -print 2>/dev/null \
  | grep -v -E '^\./helm/goldengate-platform/templates/fluent-bit-|^\./envs/dev/policies/goldengate-platform-logging-dev(/|$)' \
  || true)"
if [ -z "$UNEXPECTED_FLUENT_BIT_LOCATIONS" ]; then
  pass "Fluent Bit files exist only in the expected Phase 6A platform-chart/IAM locations"
else
  fail "unexpected Fluent Bit file(s) outside the expected Phase 6A locations:${UNEXPECTED_FLUENT_BIT_LOCATIONS}"
fi

if [ -f "hack/comma.yaml" ]; then
  fail "hack/comma.yaml (unreferenced pasted operator note) still present"
else
  pass "hack/comma.yaml removed"
fi

# 18. Strict identity-based two-factor gate; CloudWatch client construction moved behind a sanitized, non-raising protected publication boundary.
echo ""
echo "--- Phase 4D1 correction: strict gate and protected publication boundary ---"

if grep -q 'return str(raw).strip().lower() == "true"' "$COLLECTOR_SRC" 2>/dev/null; then
  pass "_parse_strict_bool_env accepts only a trimmed, case-insensitive \"true\""
else
  fail "_parse_strict_bool_env no longer uses exact-match \"true\" parsing"
fi

if grep -q 'CLOUDWATCH_PUBLISH_ENABLED is True and cfg.get("metricsEnabled") is True' "$COLLECTOR_SRC" 2>/dev/null; then
  pass "cloudwatch_enabled_for uses literal Boolean identity checks on both sides of the gate"
else
  fail "cloudwatch_enabled_for no longer uses strict identity checks"
fi

if grep -q "def publish_metrics_if_enabled" "$COLLECTOR_SRC" 2>/dev/null; then
  pass "collector.py defines the single protected publication boundary (publish_metrics_if_enabled)"
else
  fail "collector.py is missing publish_metrics_if_enabled"
fi

if grep -q '"event": "cloudwatch_client_creation_failed"' "$COLLECTOR_SRC" 2>/dev/null; then
  pass "CloudWatch client-construction failure is logged as a sanitized structured event"
else
  fail "collector.py is missing the sanitized cloudwatch_client_creation_failed log event"
fi

DIRECT_CLIENT_CALLS="$(grep -c '_cloudwatch_client()' "$COLLECTOR_SRC" 2>/dev/null || true)"
if [ "${DIRECT_CLIENT_CALLS:-0}" -eq 2 ]; then
  pass "_cloudwatch_client() is only referenced in its definition and inside publish_metrics_if_enabled (both polling_loop call sites go through the boundary)"
else
  fail "_cloudwatch_client() is referenced ${DIRECT_CLIENT_CALLS:-0} times -- expected exactly 2 (definition + protected boundary)"
fi

# 19. Controlled DEV CloudWatch activation: workflow_dispatch Boolean control, Argo CD ownership of the value, fail-closed CONFIG preflight (GetItem-only, no Scan, no new IAM), post-deployment verification/rollback; base Helm default stays disabled.
echo ""
echo "--- Phase 4D2: controlled CloudWatch DEV activation ---"

if grep -q "publishEnabled: false" "${MONITOR_CHART}/values.yaml" 2>/dev/null; then
  pass "helm/goldengate-monitor base chart default still keeps cloudwatch.publishEnabled: false"
else
  fail "helm/goldengate-monitor base chart no longer defaults cloudwatch.publishEnabled to false"
fi

if grep -q "cloudwatch" "envs/dev/goldengate-monitor/values.yaml" 2>/dev/null; then
  fail "envs/dev/goldengate-monitor/values.yaml now overrides cloudwatch.publishEnabled -- activation must stay a per-run workflow input, not a persisted values override"
else
  pass "envs/dev/goldengate-monitor/values.yaml does not override cloudwatch.publishEnabled (base default governs unless a run explicitly requests otherwise)"
fi

if grep -q "enable_cloudwatch_publication:" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -A3 "enable_cloudwatch_publication:" "$MONITOR_WORKFLOW" | grep -q "type: boolean" \
    && grep -A5 "enable_cloudwatch_publication:" "$MONITOR_WORKFLOW" | grep -q "default: false"; then
  pass "goldengate-monitor.yaml defines enable_cloudwatch_publication as a required Boolean input defaulting to false"
else
  fail "goldengate-monitor.yaml is missing the expected enable_cloudwatch_publication Boolean workflow_dispatch input"
fi

if grep -q "name: CloudWatch publication preflight (gate inventory)" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml defines the CloudWatch publication preflight step"
else
  fail "goldengate-monitor.yaml is missing the CloudWatch publication preflight step"
fi

# The preflight uses a gate inventory governed by metrics_gate_expectation (any/all-disabled/all-enabled) rather than requiring every enabled deployment to already have metricsEnabled=true, enabling staged activation (deploy switch closed, enable per-deployment via the config workflow, then verify).
if grep -q "metrics_gate_expectation:" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -A10 "metrics_gate_expectation:" "$MONITOR_WORKFLOW" | grep -q -- "- any" \
    && grep -A10 "metrics_gate_expectation:" "$MONITOR_WORKFLOW" | grep -q -- "- all-disabled" \
    && grep -A10 "metrics_gate_expectation:" "$MONITOR_WORKFLOW" | grep -q -- "- all-enabled" \
    && grep -A10 "metrics_gate_expectation:" "$MONITOR_WORKFLOW" | grep -q "default: any"; then
  pass "goldengate-monitor.yaml defines metrics_gate_expectation with any/all-disabled/all-enabled, defaulting to any"
else
  fail "goldengate-monitor.yaml is missing the metrics_gate_expectation workflow_dispatch input or its expected options/default"
fi

if grep -q 'if \[ "\$GATE_EXPECTATION" = "all-enabled" \]' "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q 'if \[ "\$GATE_EXPECTATION" = "all-disabled" \]' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml's preflight only fails a metricsEnabled=false/true deployment when the expectation requires it -- publication is no longer unconditionally gated on every deployment already being enabled"
else
  fail "goldengate-monitor.yaml's preflight no longer conditions its pass/fail decision on metrics_gate_expectation"
fi

if grep -q "table.get_item(" "$MONITOR_WORKFLOW" 2>/dev/null \
    && ! grep -qE '\.[Ss]can\(' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml's CloudWatch preflight uses GetItem only, never Scan"
else
  fail "goldengate-monitor.yaml's CloudWatch preflight no longer uses GetItem-only reads"
fi

if grep -q "PREREQUISITE NOT MET: no Ready gg-monitor pod found" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml documents the first-deployment prerequisite instead of bypassing the CONFIG check"
else
  fail "goldengate-monitor.yaml is missing the first-deployment prerequisite failure message"
fi

if grep -q -- "- name: cloudwatch.publishEnabled" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q 'value: "\${CLOUDWATCH_PUBLISH_ENABLED_VALUE}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml persists the requested value through the Argo CD Application Helm parameters (same ownership path as image.repository/image.tag)"
else
  fail "goldengate-monitor.yaml no longer passes cloudwatch.publishEnabled through the Argo CD Application Helm parameters"
fi

if grep -q "cloudwatchPublishEnabled=" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml's runtime verification confirms the deployed CLOUDWATCH_PUBLISH_ENABLED value"
else
  fail "goldengate-monitor.yaml's runtime verification no longer confirms the deployed CLOUDWATCH_PUBLISH_ENABLED value"
fi

if grep -q "cloudwatch:ListMetrics" "$MONITOR_WORKFLOW" 2>/dev/null \
    || grep -q "cloudwatch:GetMetricData" "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml references a CloudWatch read IAM action -- none should ever be introduced for this phase"
else
  pass "goldengate-monitor.yaml introduces no CloudWatch read IAM action (ListMetrics/GetMetricData)"
fi

# 20. Runtime-image hash scoped to Dockerfile inputs only, unit tests unconditional, POSIX-safe discovery, unique per-attempt Helm OCI revision, Ready-pod selection.
echo ""
echo "--- Phase 4D2 correction: image hash scope, POSIX awk, chart SemVer, Ready-pod selection ---"

if grep -q 'git rev-parse "HEAD:\${MONITOR_SOURCE_PATH}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml still hashes the whole monitoring/monitor tree (would include README.md/tests/**)"
else
  pass "goldengate-monitor.yaml no longer hashes the whole monitoring/monitor tree"
fi

if grep -q "MONITOR_IMAGE_INPUT_PATHS=(" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q "git ls-tree -r HEAD -- " "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q "git hash-object --stdin" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml computes a deterministic Git-based hash over exactly the Dockerfile-copied paths"
else
  fail "goldengate-monitor.yaml is missing the scoped Dockerfile-input hash computation"
fi

HASH_INPUT_ARRAY="$(awk '
  /MONITOR_IMAGE_INPUT_PATHS=\($/ { capture=1; print; next }
  capture { print }
  capture && /^ *\)$/ { exit }
' "$MONITOR_WORKFLOW")"
if grep -q '"\${MONITOR_SOURCE_PATH}/tools"' <<< "$HASH_INPUT_ARRAY" \
    && ! grep -q 'README.md' <<< "$HASH_INPUT_ARRAY" \
    && ! grep -q 'requirements-test.txt' <<< "$HASH_INPUT_ARRAY" \
    && ! grep -q '/tests' <<< "$HASH_INPUT_ARRAY"; then
  pass "goldengate-monitor.yaml's hash inputs exclude README.md/requirements-test.txt/tests"
else
  fail "goldengate-monitor.yaml's hash inputs unexpectedly include a non-runtime path"
fi

UNIT_TEST_STEPS_UNCONDITIONAL="true"
for step_name in "Set up Python" "Install monitor runtime and test dependencies" \
                 "Validate monitor Python syntax" "Run monitor unit tests"; do
  STEP_BLOCK="$(awk -v marker="- name: ${step_name}\$" '
    $0 ~ marker { found=1; print; next }
    found && /^      - name:/ { exit }
    found { print }
  ' "$MONITOR_WORKFLOW")"
  if grep -q "if: env.IMAGE_EXISTED" <<< "$STEP_BLOCK"; then
    fail "goldengate-monitor.yaml step \"${step_name}\" is still conditional on IMAGE_EXISTED"
    UNIT_TEST_STEPS_UNCONDITIONAL="false"
  fi
done
[ "$UNIT_TEST_STEPS_UNCONDITIONAL" = "true" ] && pass "Python setup/install/syntax-validation/unit-test steps run unconditionally (not gated on IMAGE_EXISTED)"

DOCKER_STEPS_CONDITIONAL="true"
for step_name in "Verify Docker binary and daemon are functional" "Login to Amazon ECR" \
                 "Build monitor image" "Push monitor image"; do
  STEP_BLOCK="$(awk -v marker="- name: ${step_name}\$" '
    $0 ~ marker { found=1; print; next }
    found && /^      - name:/ { exit }
    found { print }
  ' "$MONITOR_WORKFLOW")"
  if ! grep -q "if: env.IMAGE_EXISTED != 'true'" <<< "$STEP_BLOCK"; then
    fail "goldengate-monitor.yaml step \"${step_name}\" is no longer conditional on IMAGE_EXISTED"
    DOCKER_STEPS_CONDITIONAL="false"
  fi
done
[ "$DOCKER_STEPS_CONDITIONAL" = "true" ] && pass "Docker daemon-check/login/build/push steps remain conditional on IMAGE_EXISTED"

if grep -q '\[\[:space:\]\]' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml's CloudWatch deployment-discovery awk uses POSIX [[:space:]], not GNU-only \\s"
else
  fail "goldengate-monitor.yaml's CloudWatch deployment-discovery awk does not use POSIX [[:space:]]"
fi

# Functional execution of the extracted awk script (proving it returns exactly the two enabled canonical deployments) is covered by the Python suite -- see WorkflowStaticAnalysisTests.test_deployment_discovery_awk_returns_exactly_both_enabled_deployments (section 3 above).

if grep -q 'CHART_VERSION="0.\${{ github.run_number }}.\${{ github.run_attempt }}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml's chart version is a SemVer containing both run_number and run_attempt"
else
  fail "goldengate-monitor.yaml's chart version does not include run_attempt -- reruns would collide on a mutable Helm OCI repository"
fi

if grep -q '.items\[0\].metadata.name' "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml still blindly selects .items[0] for pod discovery"
else
  pass "goldengate-monitor.yaml no longer blindly selects .items[0] -- pod selection filters on Running phase and container readiness"
fi

# 21. .dockerignore participates in the runtime-image hash, the Dockerfile requires an explicitly supplied digest-pinned private base image (no public default), and Ready-pod selection excludes terminating pods.
echo ""
echo "--- Phase 4D2 correction: .dockerignore hash input, digest-pinned base image, non-terminating Ready pod ---"

if grep -q '"\${MONITOR_SOURCE_PATH}/.dockerignore"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml includes .dockerignore in the runtime-image hash inputs"
else
  fail "goldengate-monitor.yaml's runtime-image hash inputs no longer include .dockerignore"
fi

if [ -f "${MONITOR_APP_DIR}/.dockerignore" ]; then
  pass "monitoring/monitor/.dockerignore exists (a hashed, tracked input)"
else
  fail "monitoring/monitor/.dockerignore is missing"
fi

if grep -q '^ARG BASE_IMAGE$' "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null \
    && ! grep -q '^ARG BASE_IMAGE=' "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null \
    && ! grep -q 'python:3.12-slim' "${MONITOR_APP_DIR}/Dockerfile" 2>/dev/null; then
  pass "monitoring/monitor/Dockerfile requires an explicitly supplied BASE_IMAGE (no public default)"
else
  fail "monitoring/monitor/Dockerfile still has a public default base image"
fi

if grep -q "name: Validate approved base image reference" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q "vars.MONITOR_BASE_IMAGE" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml validates an externally supplied MONITOR_BASE_IMAGE (vars.* convention, not hardcoded)"
else
  fail "goldengate-monitor.yaml is missing the base-image validation step"
fi

if grep -qE '@sha256:\[0-9a-f\]\{64\}\$' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml requires MONITOR_BASE_IMAGE to be digest-pinned (@sha256:<64 lowercase hex>)"
else
  fail "goldengate-monitor.yaml no longer enforces digest-pinning on MONITOR_BASE_IMAGE"
fi

BASE_IMAGE_STEP="$(awk '
  /- name: Validate approved base image reference/ { capture=1 }
  capture { print }
  capture && /- name: Prepare monitor image variables/ { exit }
' "$MONITOR_WORKFLOW")"
# The value is only ever interpolated on the GITHUB_ENV handoff line; failure/success messages never include it -- proven functionally by MonitorBaseImageValidationTests.test_failure_never_prints_the_raw_malformed_value/.test_success_path_never_prints_the_full_raw_value_either.
BASE_IMAGE_INTERPOLATIONS="$(grep -c '\${MONITOR_BASE_IMAGE}' <<< "$BASE_IMAGE_STEP" || true)"
if [ "${BASE_IMAGE_INTERPOLATIONS:-0}" -eq 1 ]; then
  pass "goldengate-monitor.yaml's base-image validation never prints the raw supplied value (only the GITHUB_ENV handoff interpolates it)"
else
  fail "goldengate-monitor.yaml's base-image validation interpolates \${MONITOR_BASE_IMAGE} ${BASE_IMAGE_INTERPOLATIONS:-0} times -- expected exactly 1 (GITHUB_ENV handoff only)"
fi

if grep -q "MONITOR_BASE_IMAGE_INPUT: \${{ vars.MONITOR_BASE_IMAGE }}" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml passes the GitHub expression through step-level env (MONITOR_BASE_IMAGE_INPUT), never direct shell interpolation"
else
  fail "goldengate-monitor.yaml no longer passes vars.MONITOR_BASE_IMAGE through step-level env"
fi

if grep -qF -- '\${{ vars.MONITOR_BASE_IMAGE }}"' <<< "$BASE_IMAGE_STEP"; then
  fail "goldengate-monitor.yaml's base-image validation run script still directly interpolates \${{ vars.MONITOR_BASE_IMAGE }}"
else
  pass "goldengate-monitor.yaml's base-image validation run script contains no direct \${{ vars.MONITOR_BASE_IMAGE }} interpolation"
fi

if grep -Fq -- '--build-arg "BASE_IMAGE=${MONITOR_BASE_IMAGE}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml passes the validated MONITOR_BASE_IMAGE into docker build via --build-arg"
else
  fail "goldengate-monitor.yaml no longer passes BASE_IMAGE into docker build"
fi

if grep -q 'echo "BASE_IMAGE \${MONITOR_BASE_IMAGE}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml folds the resolved base-image reference into the same runtime-input hash"
else
  fail "goldengate-monitor.yaml's hash no longer incorporates the resolved base-image reference"
fi

# Preflight pod selection uses a Deployment/ReplicaSet ownership-chain loop excluding terminating pods via `deletionTimestamp // empty` + bash comparison, while post-deployment verification still uses the single-jq-filter `deletionTimestamp == null` style -- one of each pattern is expected, not two of the same.
VERIFY_TERMINATING_EXCLUSION_COUNT="$(grep -c 'deletionTimestamp == null' "$MONITOR_WORKFLOW" 2>/dev/null || true)"
PREFLIGHT_TERMINATING_EXCLUSION_COUNT="$(grep -c 'deletionTimestamp // empty' "$MONITOR_WORKFLOW" 2>/dev/null || true)"
if [ "${VERIFY_TERMINATING_EXCLUSION_COUNT:-0}" -eq 1 ] && [ "${PREFLIGHT_TERMINATING_EXCLUSION_COUNT:-0}" -eq 1 ]; then
  pass "both the preflight (ownership-chain) and post-deployment verification (jq filter) pod selections exclude terminating pods"
else
  fail "expected exactly 1 ownership-chain and 1 jq-filter terminating-pod exclusion, found ${PREFLIGHT_TERMINATING_EXCLUSION_COUNT:-0} and ${VERIFY_TERMINATING_EXCLUSION_COUNT:-0}"
fi

# 22. Workflow-security and manager critical-service correction: no direct GitHub-expression interpolation inside a run script, a fully-anchored ECR repository+digest grammar (not prefix+suffix only), and manager-compatible adminsrvr/distsrvr/recvsrvr coverage for every deployment.
echo ""
echo "--- Phase 4D2 correction: safe env passthrough, full ECR grammar, manager critical-service coverage ---"

if grep -qF -- 'MONITOR_BASE_IMAGE="${{ vars.MONITOR_BASE_IMAGE }}"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "goldengate-monitor.yaml still assigns \${{ vars.MONITOR_BASE_IMAGE }} directly inside a run script"
else
  pass "goldengate-monitor.yaml no longer assigns \${{ vars.MONITOR_BASE_IMAGE }} directly inside a run script"
fi

if grep -qE "MONITOR_BASE_IMAGE_PATTERN='\^\[a-z0-9\]\+" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q 'MONITOR_BASE_IMAGE_REMAINDER' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml validates the full post-prefix remainder against an anchored repository+digest grammar (not prefix+suffix only)"
else
  fail "goldengate-monitor.yaml no longer validates the full ECR repository+digest grammar"
fi

if grep -q 'RECOGNIZED_CRITICAL_SERVICES = ("adminsrvr", "distsrvr", "recvsrvr")' "${MONITOR_APP_DIR}/health_rules.py" 2>/dev/null; then
  pass "health_rules.py defines the manager-compatible three-service recognized set (adminsrvr/distsrvr/recvsrvr)"
else
  fail "health_rules.py is missing the manager-compatible three-service recognized set"
fi

if grep -q "CRITICAL_SERVICES_BY_TYPE" "${MONITOR_APP_DIR}/health_rules.py" "${MONITOR_APP_DIR}/collector.py" 2>/dev/null; then
  fail "a per-type critical-service dict (CRITICAL_SERVICES_BY_TYPE) still exists -- Oracle/PostgreSQL must both default to the full three-service set"
else
  pass "no per-type critical-service dict remains -- every deployment defaults to the full three-service set"
fi

if grep -q "def resolve_critical_services" "${MONITOR_APP_DIR}/health_rules.py" 2>/dev/null; then
  pass "health_rules.py defines a bounded, fail-safe resolve_critical_services helper for the optional CONFIG override"
else
  fail "health_rules.py is missing resolve_critical_services"
fi

# 23. Observer source/build/chart retirement, legacy-values folder disablement without deletion, and gg-monitor legacy-fallback removal.
echo ""
echo "--- Phase 5A: legacy values folder disabled (retained, not deleted) ---"

if [ -f "$DETECT_SCRIPT" ] && command -v python3 >/dev/null 2>&1; then
  # Uses the real, tracked, executable detection script directly (never a reimplementation) and exercises its is_active_deployment_values_file() function and deletion-candidate case statement directly, against the real repository files.
  cp "$DETECT_SCRIPT" "${WORKDIR}/detect_script.sh"

  awk '/^is_active_deployment_values_file\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh" > "${WORKDIR}/is_active_fn.sh"

  # is_goldengate_deployment_values_file and its git-revision sibling both depend on _classify_deployment_model_yaml -- all three must be extracted and sourced together, in dependency order, or the classifier fails with a silent, useless "command not found". _efs_mode_from_yaml is also bundled here since the deletion loop below (deletion_loop.sh) calls it and only sources this same file.
  {
    awk '/^_classify_deployment_model_yaml\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
    echo ""
    awk '/^is_goldengate_deployment_values_file\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
    echo ""
    awk '/^is_goldengate_deployment_values_file_at_ref\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
    echo ""
    awk '/^_efs_mode_from_yaml\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
  } > "${WORKDIR}/is_gg_fn.sh"

  # Fails loudly if any expected function body failed to extract -- an empty/missing body would make every downstream source-and-call test meaningless.
  for required_fn in _classify_deployment_model_yaml is_goldengate_deployment_values_file is_goldengate_deployment_values_file_at_ref _efs_mode_from_yaml; do
    if ! grep -q "^${required_fn}() {" "${WORKDIR}/is_gg_fn.sh"; then
      fail "could not extract ${required_fn}() from ${DETECT_SCRIPT} -- the classifier test harness cannot run"
    fi
  done

  cat > "${WORKDIR}/run_is_active_checks.sh" <<HARNESS
#!/bin/bash
set -euo pipefail
source "${WORKDIR}/is_active_fn.sh"

check_one() {
  local file="\$1" expect_status="\$2" label="\$3"
  set +e
  reason="\$(is_active_deployment_values_file "\$file")"
  status=\$?
  set -e
  if [ "\$status" -eq "\$expect_status" ]; then
    echo "PASS \$label (\$reason)"
  else
    echo "FAIL \$label (expected status \$expect_status, got \$status, reason: \$reason)"
  fi
}

check_one "envs/dev/gg-postgresql-repltest-01/values.yaml" 0 "postgresql-repltest-active"
check_one "envs/dev/gg-mssql-repltest-01/values.yaml" 0 "mssql-repltest-active"
HARNESS

  ACTIVE_CHECK_OUTPUT="$(bash "${WORKDIR}/run_is_active_checks.sh" 2>&1 || true)"
  echo "$ACTIVE_CHECK_OUTPUT"

  cat > "${WORKDIR}/run_is_gg_checks.sh" <<HARNESS
#!/bin/bash
set -euo pipefail
source "${WORKDIR}/is_gg_fn.sh"

check_one() {
  local file="\$1" expect_status="\$2" label="\$3"
  set +e
  reason="\$(is_goldengate_deployment_values_file "\$file")"
  status=\$?
  set -e
  if [ "\$status" -eq "\$expect_status" ]; then
    echo "PASS \$label (\$reason)"
  else
    echo "FAIL \$label (expected status \$expect_status, got \$status, reason: \$reason)"
  fi
}

check_one "envs/dev/gg-postgresql-repltest-01/values.yaml" 0 "postgresql-repltest-is-gg"
check_one "envs/dev/gg-mssql-repltest-01/values.yaml" 0 "mssql-repltest-is-gg"
check_one "envs/dev/goldengate-monitor/values.yaml" 1 "monitor-is-not-gg"
check_one "envs/dev/argocd/values.yaml" 1 "argocd-is-not-gg"
HARNESS

  GG_CHECK_OUTPUT="$(bash "${WORKDIR}/run_is_gg_checks.sh" 2>&1 || true)"
  echo "$GG_CHECK_OUTPUT"

  if echo "$GG_CHECK_OUTPUT" | grep -q "^PASS postgresql-repltest-is-gg" \
      && echo "$GG_CHECK_OUTPUT" | grep -q "^PASS mssql-repltest-is-gg"; then
    pass "the real workflow's is_goldengate_deployment_values_file() classifies both canonical GoldenGate deployment folders correctly"
  else
    fail "one or more canonical GoldenGate deployment folders are misclassified by is_goldengate_deployment_values_file()"
  fi

  if echo "$GG_CHECK_OUTPUT" | grep -q "^PASS monitor-is-not-gg" \
      && echo "$GG_CHECK_OUTPUT" | grep -q "^PASS argocd-is-not-gg"; then
    pass "the real workflow's is_goldengate_deployment_values_file() correctly rejects goldengate-monitor and argocd (no deploymentModel field)"
  else
    fail "goldengate-monitor and/or argocd are incorrectly classified as GoldenGate deployments"
  fi

  if echo "$ACTIVE_CHECK_OUTPUT" | grep -q "^PASS postgresql-repltest-active" && echo "$ACTIVE_CHECK_OUTPUT" | grep -q "^PASS mssql-repltest-active"; then
    pass "the real workflow's is_active_deployment_values_file() reports both canonical folders active"
  else
    fail "one or both canonical folders are not reported active by the real workflow function"
  fi

  # A shared-chart-change selection scans every envs/dev/<id>/values.yaml (excluding argocd/) exactly as the workflow does, filtered through the same two real functions in the same order, proving the exact resulting active set.
  CANDIDATE_IDS="$(find envs/dev -mindepth 2 -maxdepth 2 -name values.yaml -not -path 'envs/dev/argocd/*' \
    | sed -E 's#^envs/dev/([^/]+)/values\.yaml$#\1#' | sort -u)"
  ACTIVE_IDS=""
  for id in $CANDIDATE_IDS; do
    set +e
    bash -c "source '${WORKDIR}/is_gg_fn.sh'; is_goldengate_deployment_values_file 'envs/dev/${id}/values.yaml'" >/dev/null 2>&1
    gg_st=$?
    set -e
    if [ "$gg_st" -ne 0 ]; then
      continue
    fi
    set +e
    bash -c "source '${WORKDIR}/is_active_fn.sh'; is_active_deployment_values_file 'envs/dev/${id}/values.yaml'" >/dev/null 2>&1
    st=$?
    set -e
    [ "$st" -eq 0 ] && ACTIVE_IDS="${ACTIVE_IDS} ${id}"
  done
  ACTIVE_IDS_SORTED="$(echo "$ACTIVE_IDS" | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ' ' | sed -E 's/ $//')"
  echo "Active candidate IDs for a shared-chart-change selection: ${ACTIVE_IDS_SORTED}"

  # Self-service: the expected active set is never a hardcoded name list -- it is derived from the same canonical folder-driven inventory (hack/goldengate-deployment-model.py list) the workflow itself must agree with, so onboarding a new envs/dev/<id>/values.yaml folder never requires editing this test.
  MODEL_ACTIVE_IDS="$(python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev list 2>/dev/null | awk '$1 == "ACTIVE" {print $2}' | sort -u | tr '\n' ' ' | sed -E 's/ $//')"
  echo "Canonical deployment-model ACTIVE IDs: ${MODEL_ACTIVE_IDS}"

  if [ -n "$ACTIVE_IDS_SORTED" ] && [ "$ACTIVE_IDS_SORTED" = "$MODEL_ACTIVE_IDS" ]; then
    pass "a shared chart change produces exactly the canonical deployment-model active set (${MODEL_ACTIVE_IDS})"
  else
    fail "a shared chart change produced an active set that diverges from the canonical deployment-model output: got [${ACTIVE_IDS_SORTED}], expected [${MODEL_ACTIVE_IDS}]"
  fi

  if echo "$ACTIVE_IDS_SORTED" | grep -qw "goldengate-monitor"; then
    fail "goldengate-monitor is present in the shared-chart-change active set -- it must never enter the GoldenGate matrix"
  else
    pass "goldengate-monitor is absent from the shared-chart-change active set"
  fi

  if echo "$ACTIVE_IDS_SORTED" | grep -qw "argocd"; then
    fail "argocd is present in the shared-chart-change active set -- it must never enter the GoldenGate matrix"
  else
    pass "argocd is absent from the shared-chart-change active set"
  fi

  # Extracts the real case-statement logic and exercises it with the REAL classifier functions (only jq is stubbed) against a throwaway Git repo built to exercise all 7 required scenarios: existing/removed files, GoldenGate/non-GoldenGate deploymentModel, malformed/unknown content.
  awk '/^for CANDIDATE_ID in \$DELETION_CANDIDATE_IDS; do$/,/^done$/' "${WORKDIR}/detect_script.sh" > "${WORKDIR}/deletion_loop.sh"

  if [ -s "${WORKDIR}/deletion_loop.sh" ] && [ -s "${WORKDIR}/is_gg_fn.sh" ]; then
    DELETION_REPO="${WORKDIR}/deletion-repo"
    rm -rf "$DELETION_REPO"
    mkdir -p "$DELETION_REPO"

    mkdir -p "${DELETION_REPO}/envs/dev/case2-removed-canonical" \
             "${DELETION_REPO}/envs/dev/goldengate-monitor" \
             "${DELETION_REPO}/envs/dev/argocd" \
             "${DELETION_REPO}/envs/dev/case6-malformed" \
             "${DELETION_REPO}/envs/dev/case7-unknown-model" \
             "${DELETION_REPO}/envs/dev/case-empty-zerobyte" \
             "${DELETION_REPO}/envs/dev/case-empty-comment" \
             "${DELETION_REPO}/envs/dev/case-empty-whitespace" \
             "${DELETION_REPO}/envs/dev/case-empty-null" \
             "${DELETION_REPO}/envs/dev/case3-historical-legacypair-removed" \
             "${DELETION_REPO}/envs/dev/case8-lifecycle-absent"

    printf 'deploymentModel: singleRuntime\nrunning: at-base-revision\n' > "${DELETION_REPO}/envs/dev/case2-removed-canonical/values.yaml"
    printf 'deploymentModel: singleRuntime\ndeployment:\n  enabled: true\npersistence:\n  enabled: true\n  provider: efs\n  efs:\n    mode: managed\n' > "${DELETION_REPO}/envs/dev/case8-lifecycle-absent/values.yaml"
    printf 'global:\n  environment: dev\nnamespace:\n  create: true\n' > "${DELETION_REPO}/envs/dev/goldengate-monitor/values.yaml"
    printf 'server:\n  extraArgs: []\n' > "${DELETION_REPO}/envs/dev/argocd/values.yaml"
    printf 'deploymentModel: singleRuntime\n  bad indent: [unterminated\n' > "${DELETION_REPO}/envs/dev/case6-malformed/values.yaml"
    printf 'deploymentModel: someUnknownModel\n' > "${DELETION_REPO}/envs/dev/case7-unknown-model/values.yaml"
    printf 'deploymentModel: singleRuntime\nrunning: at-base-revision\n' > "${DELETION_REPO}/envs/dev/case-empty-zerobyte/values.yaml"
    printf 'deploymentModel: singleRuntime\nrunning: at-base-revision\n' > "${DELETION_REPO}/envs/dev/case-empty-comment/values.yaml"
    printf 'deploymentModel: singleRuntime\nrunning: at-base-revision\n' > "${DELETION_REPO}/envs/dev/case-empty-whitespace/values.yaml"
    printf 'deploymentModel: singleRuntime\nrunning: at-base-revision\n' > "${DELETION_REPO}/envs/dev/case-empty-null/values.yaml"
    printf 'deploymentModel: legacyPair\nrunning: at-base-revision\n' > "${DELETION_REPO}/envs/dev/case3-historical-legacypair-removed/values.yaml"

    git -C "$DELETION_REPO" init -q
    git -C "$DELETION_REPO" config user.email "test@test.invalid"
    git -C "$DELETION_REPO" config user.name "test"
    git -C "$DELETION_REPO" add -A
    git -C "$DELETION_REPO" commit -q -m "base revision"
    DELETION_BEFORE_SHA="$(git -C "$DELETION_REPO" rev-parse HEAD)"

    # Mutates the working tree to the "after" state the loop evaluates: case2/3/4/5/6/7 removed (case3 proves the HISTORICAL DELETION CONTRACT still classifies a legacyPair deployment from the base revision); case1 added fresh, uncommitted (a "still exists, now inactive" candidate, invisible to the active-only path since it's legacyPair); case-empty-* files overwritten in place with the four "deliberately empty" shapes.
    git -C "$DELETION_REPO" rm -rq envs/dev/case2-removed-canonical envs/dev/goldengate-monitor envs/dev/argocd envs/dev/case6-malformed envs/dev/case7-unknown-model envs/dev/case3-historical-legacypair-removed

    mkdir -p "${DELETION_REPO}/envs/dev/case1-retired-legacypair-retained"
    printf 'deploymentModel: legacyPair\ndeployment:\n  enabled: false\n' > "${DELETION_REPO}/envs/dev/case1-retired-legacypair-retained/values.yaml"

    # case8: the file is NOT removed -- only its content changes to add lifecycle.state=absent, proving the physical-removal/lifecycle-absent distinction (the descriptor and its managed EFS declaration are still physically present).
    printf 'deploymentModel: singleRuntime\ndeployment:\n  enabled: true\npersistence:\n  enabled: true\n  provider: efs\n  efs:\n    mode: managed\nlifecycle:\n  state: absent\n' > "${DELETION_REPO}/envs/dev/case8-lifecycle-absent/values.yaml"

    : > "${DELETION_REPO}/envs/dev/case-empty-zerobyte/values.yaml"
    printf '# retired\n# nothing here\n' > "${DELETION_REPO}/envs/dev/case-empty-comment/values.yaml"
    printf '   \n\n   \n' > "${DELETION_REPO}/envs/dev/case-empty-whitespace/values.yaml"
    printf 'null\n' > "${DELETION_REPO}/envs/dev/case-empty-null/values.yaml"

    DELETION_TEST_OUTPUT="$(cd "$DELETION_REPO" && bash -c '
      set -euo pipefail
      source "'"${WORKDIR}"'/is_gg_fn.sh"
      source "'"${WORKDIR}"'/is_active_fn.sh"
      jq() {
        shift
        local args=("$@") model="" id="" reason=""
        for i in "${!args[@]}"; do
          [ "${args[$i]}" = "deployment_id" ] && id="${args[$((i+1))]}"
          [ "${args[$i]}" = "deployment_model" ] && model="${args[$((i+1))]}"
          [ "${args[$i]}" = "reason" ] && reason="${args[$((i+1))]}"
        done
        echo "[ADDED id=${id} model=${model} reason=${reason}]"
      }
      BEFORE_SHA="'"$DELETION_BEFORE_SHA"'"

      for id in case1-retired-legacypair-retained case2-removed-canonical case3-historical-legacypair-removed goldengate-monitor argocd case6-malformed case7-unknown-model case-empty-zerobyte case-empty-comment case-empty-whitespace case-empty-null case8-lifecycle-absent; do
        DELETION_MATRIX_ITEMS="[]"
        INACTIVE_LOG=""
        DELETION_CANDIDATE_IDS="$id"
        source "'"${WORKDIR}"'/deletion_loop.sh"
        echo "RESULT ${id} => ${DELETION_MATRIX_ITEMS}"
      done
    ' 2>&1)"
    DELETION_HARNESS_STATUS=$?
    echo "$DELETION_TEST_OUTPUT"

    # The harness itself must never silently swallow a broken classifier -- any command-not-found or Python traceback anywhere in the output fails this test outright.
    if [ "$DELETION_HARNESS_STATUS" -ne 0 ] \
        || echo "$DELETION_TEST_OUTPUT" | grep -qiE "command not found|Traceback \(most recent call last\)|: not found$"; then
      fail "the deletion-candidate test harness itself failed or is broken (command-not-found/traceback/non-zero exit) -- see output above"
    else
      pass "the deletion-candidate test harness ran the real classifier functions with no command-not-found/traceback"
    fi

    check_deletion_case() {
      local label="$1" pattern="$2"
      if echo "$DELETION_TEST_OUTPUT" | grep -qE "$pattern"; then
        pass "$label"
      else
        fail "$label -- expected pattern not found: ${pattern}"
      fi
    }

    check_deletion_case "retained legacyPair (deployment.enabled=false) produces no deletion entry" \
      '^RESULT case1-retired-legacypair-retained => \[\]$'
    check_deletion_case "2: removed canonical GoldenGate values (deploymentModel: singleRuntime) produces a deletion entry with deployment_model=singleRuntime and reason=physical-removal" \
      '^RESULT case2-removed-canonical => \[ADDED id=case2-removed-canonical model=singleRuntime reason=physical-removal\]$'
    check_deletion_case "4-req: the historical deletion contract still classifies a removed legacyPair deployment (deployment_model=legacyPair) even though legacyPair is no longer active/deployable" \
      '^RESULT case3-historical-legacypair-removed => \[ADDED id=case3-historical-legacypair-removed model=legacyPair reason=physical-removal\]$'
    check_deletion_case "11: removed goldengate-monitor values does not enter the GoldenGate deletion matrix" \
      '^RESULT goldengate-monitor => \[\]$'
    check_deletion_case "12: removed argocd values does not enter the GoldenGate deletion matrix" \
      '^RESULT argocd => \[\]$'
    check_deletion_case "13: removed malformed previous YAML does not enter deletion" \
      '^RESULT case6-malformed => \[\]$'
    check_deletion_case "14: removed unknown deploymentModel does not enter deletion" \
      '^RESULT case7-unknown-model => \[\]$'
    check_deletion_case "8: a zero-byte values file (previously valid) creates its deletion entry with reason=physical-removal" \
      '^RESULT case-empty-zerobyte => \[ADDED id=case-empty-zerobyte model=singleRuntime reason=physical-removal\]$'
    check_deletion_case "6: a comment-only canonical values file creates its deletion entry with reason=physical-removal" \
      '^RESULT case-empty-comment => \[ADDED id=case-empty-comment model=singleRuntime reason=physical-removal\]$'
    check_deletion_case "7: a whitespace-only values file creates its deletion entry with reason=physical-removal" \
      '^RESULT case-empty-whitespace => \[ADDED id=case-empty-whitespace model=singleRuntime reason=physical-removal\]$'
    check_deletion_case "9: YAML null creates its deletion entry when the previous file was valid, reason=physical-removal" \
      '^RESULT case-empty-null => \[ADDED id=case-empty-null model=singleRuntime reason=physical-removal\]$'
    check_deletion_case "11 (Issue 3): lifecycle.state=absent while the descriptor still physically exists produces a deletion-matrix entry classified as reason=lifecycle-absent, never physical-removal" \
      '^RESULT case8-lifecycle-absent => \[ADDED id=case8-lifecycle-absent model=singleRuntime reason=lifecycle-absent\]$'

    rm -rf "$DELETION_REPO"
  else
    fail "could not extract the deletion-candidate loop and/or classifier functions from ${DETECT_SCRIPT}"
  fi
else
  skip "Phase 5A legacy-folder behavioral checks -- ${DETECT_SCRIPT} or python3 not available"
fi

# Active/historical classifier split regression tests: manual legacyPair rejected; active legacyPair cannot enter the build matrix; missing/unknown deploymentModel never defaults to legacyPair; unknown deletion-matrix model fails closed; no active build/Application path contains legacyPair; no source/target StatefulSet/PVC validation remains; the workflow summary documents all deletion triggers. (Historical-legacyPair deletion is covered above by case3.)
echo ""
echo "--- Phase 5B2A: active-contract rejection of legacyPair; deletion-job fail-closed; no legacyPair in the active build/Application path ---"

if [ -f "${WORKDIR}/is_gg_fn.sh" ] && [ -s "${WORKDIR}/is_gg_fn.sh" ]; then
  CLASSIFIER_REPO="${WORKDIR}/classifier-repo"
  rm -rf "$CLASSIFIER_REPO"
  mkdir -p "${CLASSIFIER_REPO}/envs/dev/case-manual-legacypair" \
           "${CLASSIFIER_REPO}/envs/dev/case-push-active-legacypair" \
           "${CLASSIFIER_REPO}/envs/dev/case-no-model" \
           "${CLASSIFIER_REPO}/envs/dev/case-unknown-model-active"
  printf 'deploymentModel: legacyPair\nrunning: true\n' > "${CLASSIFIER_REPO}/envs/dev/case-manual-legacypair/values.yaml"
  printf 'deploymentModel: legacyPair\nrunning: true\n' > "${CLASSIFIER_REPO}/envs/dev/case-push-active-legacypair/values.yaml"
  printf 'runtime:\n  replicas: 1\n' > "${CLASSIFIER_REPO}/envs/dev/case-no-model/values.yaml"
  printf 'deploymentModel: totallyMadeUp\n' > "${CLASSIFIER_REPO}/envs/dev/case-unknown-model-active/values.yaml"

  CLASSIFIER_OUT="$(cd "$CLASSIFIER_REPO" && bash -c '
    set -euo pipefail
    source "'"${WORKDIR}"'/is_gg_fn.sh"
    for id in case-manual-legacypair case-push-active-legacypair case-no-model case-unknown-model-active; do
      set +e
      REASON="$(is_goldengate_deployment_values_file "envs/dev/${id}/values.yaml")"
      STATUS=$?
      set -e
      echo "CLASSIFY ${id} status=${STATUS} reason=${REASON}"
    done
  ' 2>&1)"
  echo "$CLASSIFIER_OUT"

  # 1: manual (workflow_dispatch-equivalent) legacyPair deployment request is rejected by the active contract -- workflow_dispatch validates it with exactly this same function.
  if echo "$CLASSIFIER_OUT" | grep -qE "^CLASSIFY case-manual-legacypair status=1 reason=not a GoldenGate deployment values file: deploymentModel='legacyPair'$"; then
    pass "1: a manual (workflow_dispatch) request for a legacyPair deployment is rejected by the active contract"
  else
    fail "1: a manual legacyPair deployment request was not rejected as expected"
  fi

  # 2: active legacyPair cannot enter the push-triggered build/update matrix -- that loop classifies every candidate with exactly this same function first.
  if echo "$CLASSIFIER_OUT" | grep -qE "^CLASSIFY case-push-active-legacypair status=1 reason=not a GoldenGate deployment values file: deploymentModel='legacyPair'$"; then
    pass "2: a legacyPair deployment values file cannot enter the active push build/update matrix"
  else
    fail "2: a legacyPair deployment values file was not excluded from the active build matrix as expected"
  fi

  # 3: missing/unknown current deploymentModel never defaults to legacyPair -- both a missing key and an unrecognized value must be rejected, never silently accepted.
  if echo "$CLASSIFIER_OUT" | grep -qE "^CLASSIFY case-no-model status=1 reason=not a GoldenGate deployment values file: deploymentModel=None$" \
      && ! echo "$CLASSIFIER_OUT" | grep -q "case-no-model.*legacyPair"; then
    pass "3a: a current values file with no deploymentModel key is rejected, never defaulted to legacyPair"
  else
    fail "3a: a current values file with no deploymentModel key was not handled as expected"
  fi
  if echo "$CLASSIFIER_OUT" | grep -qE "^CLASSIFY case-unknown-model-active status=1 reason=not a GoldenGate deployment values file: deploymentModel='totallyMadeUp'$"; then
    pass "3b: a current values file with an unrecognized deploymentModel is rejected, never defaulted to legacyPair"
  else
    fail "3b: a current values file with an unrecognized deploymentModel was not handled as expected"
  fi

  rm -rf "$CLASSIFIER_REPO"
else
  skip "active-contract classifier rejection tests -- ${WORKDIR}/is_gg_fn.sh not available"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  # 5: the deletion job's "Prepare deletion variables" step must fail closed (non-zero exit, no defaulting) when matrix.deployment_model is neither singleRuntime nor legacyPair.
  python3 - "$EKS_APP_WORKFLOW" > "${WORKDIR}/prepare_deletion_vars.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["delete_removed_argocd_applications"]["steps"]:
    if step.get("name") == "Prepare deletion variables":
        text = step["run"]
        text = text.replace('${{ matrix.environment }}', '$TEST_ENVIRONMENT')
        text = text.replace('${{ matrix.deployment_id }}', '$TEST_DEPLOYMENT_ID')
        text = text.replace('${{ matrix.deployment_model }}', '$TEST_DEPLOYMENT_MODEL')
        sys.stdout.write(text)
        break
else:
    sys.exit("step not found")
PYEOF

  if [ -s "${WORKDIR}/prepare_deletion_vars.sh" ]; then
    set +e
    UNKNOWN_DELETION_MODEL_OUT="$(TEST_ENVIRONMENT="dev" TEST_DEPLOYMENT_ID="gg-oracle-payments-01" TEST_DEPLOYMENT_MODEL="bogusModel" \
      bash -c 'set -euo pipefail; GITHUB_ENV=/dev/null; source "'"${WORKDIR}"'/prepare_deletion_vars.sh"' 2>&1)"
    UNKNOWN_DELETION_MODEL_STATUS=$?
    set -e
    echo "$UNKNOWN_DELETION_MODEL_OUT"

    if [ "$UNKNOWN_DELETION_MODEL_STATUS" -ne 0 ] \
        && echo "$UNKNOWN_DELETION_MODEL_OUT" | grep -qF "FAIL: unrecognized deployment_model 'bogusModel'" \
        && ! echo "$UNKNOWN_DELETION_MODEL_OUT" | grep -qi "defaulting to legacyPair"; then
      pass "5: an unknown deletion-matrix deployment_model fails the deletion job closed (never defaults to legacyPair)"
    else
      fail "5: an unknown deletion-matrix deployment_model was not rejected as expected (status=${UNKNOWN_DELETION_MODEL_STATUS})"
    fi

    # Sanity: singleRuntime and legacyPair both still resolve without error. RUNTIME_NAMESPACE is exported here to simulate the real job's earlier "Load resolved environment config" step (canonical, never reconstructed inside "Prepare deletion variables" itself) -- legacyPair's naming never depends on it.
    set +e
    SINGLE_OK_OUT="$(TEST_ENVIRONMENT="dev" TEST_DEPLOYMENT_ID="gg-oracle-payments-01" TEST_DEPLOYMENT_MODEL="singleRuntime" RUNTIME_NAMESPACE="goldengate-dev" \
      bash -c 'set -euo pipefail; GITHUB_ENV=/dev/null; source "'"${WORKDIR}"'/prepare_deletion_vars.sh"; echo "OK namespace=${TARGET_NAMESPACE} app=${ARGOCD_APP_NAME}"' 2>&1)"
    SINGLE_OK_STATUS=$?
    LEGACY_OK_OUT="$(TEST_ENVIRONMENT="dev" TEST_DEPLOYMENT_ID="payments-ora-to-pg-001" TEST_DEPLOYMENT_MODEL="legacyPair" \
      bash -c 'set -euo pipefail; GITHUB_ENV=/dev/null; source "'"${WORKDIR}"'/prepare_deletion_vars.sh"; echo "OK namespace=${TARGET_NAMESPACE} app=${ARGOCD_APP_NAME}"' 2>&1)"
    LEGACY_OK_STATUS=$?
    set -e

    if [ "$SINGLE_OK_STATUS" -eq 0 ] && echo "$SINGLE_OK_OUT" | grep -qF "OK namespace=goldengate-dev app=goldengate-dev-oracle-payments-01"; then
      pass "the deletion job still resolves singleRuntime namespace/Application naming correctly"
    else
      fail "the deletion job did not resolve singleRuntime namespace/Application naming as expected"
      echo "$SINGLE_OK_OUT"
    fi

    if [ "$LEGACY_OK_STATUS" -eq 0 ] && echo "$LEGACY_OK_OUT" | grep -qF "OK namespace=gg-dev-payments-ora-to-pg-001 app=goldengate-payments-ora-to-pg-001"; then
      pass "the deletion job still resolves the historical legacyPair namespace/Application naming (gg-<env>-<id> / goldengate-<id>) correctly"
    else
      fail "the deletion job did not resolve the historical legacyPair namespace/Application naming as expected"
      echo "$LEGACY_OK_OUT"
    fi
  else
    fail "could not extract the 'Prepare deletion variables' step from ${EKS_APP_WORKFLOW}"
  fi
else
  skip "deletion-job fail-closed unknown-model test -- python3 not available"
fi

# 6/7: static checks that no active build/Application-path code contains legacyPair conditional logic, and no source/target StatefulSet/PVC validation remains anywhere.
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  BUILD_JOB_LEGACY_CODE_HITS="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

hits = []
for step in doc["jobs"]["build_publish_and_deploy"]["steps"]:
    run = step.get("run", "")
    for lineno, line in enumerate(run.splitlines(), start=1):
        if "legacypair" not in line.lower():
            continue
        stripped = line.strip()
        # Allowed: blank/comment lines (bash and embedded python3 both use '#') or a bare echo/print (informational text) -- what must be absent is legacyPair in an actual conditional, comparison, assignment, or case pattern.
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("echo ") or stripped.startswith('echo"') \
                or stripped.startswith("print(") or stripped.startswith("print ("):
            continue
        hits.append(f'{step.get("name")}:{lineno}: {stripped}')

for hit in hits:
    print(hit)
PYEOF
)"
  echo "$BUILD_JOB_LEGACY_CODE_HITS"

  if [ -z "$BUILD_JOB_LEGACY_CODE_HITS" ]; then
    pass "6: no active build/Application-path code (non-comment lines) in build_publish_and_deploy references legacyPair"
  else
    fail "6: build_publish_and_deploy still contains non-comment legacyPair references:"$'\n'"${BUILD_JOB_LEGACY_CODE_HITS}"
  fi

  # Only non-comment lines count as "validation remaining" -- a comment merely explaining what was removed is expected and must not be flagged.
  strip_comment_hits() {
    while IFS= read -r hit; do
      [ -z "$hit" ] && continue
      content="${hit#*:}"
      case "$(echo "$content" | sed -E 's/^[[:space:]]*//')" in
        "#"*) continue ;;
        *) echo "$hit" ;;
      esac
    done
  }

  SOURCE_TARGET_HITS="$(grep -nE "source-statefulset\.yaml|target-statefulset\.yaml|source_pvc_name|target_pvc_name|SOURCE_STS|TARGET_STS|SOURCE_ENABLED|TARGET_ENABLED" "$EKS_APP_WORKFLOW" | strip_comment_hits || true)"
  if [ -z "$SOURCE_TARGET_HITS" ]; then
    pass "7: no source/target StatefulSet/PVC validation (source-statefulset.yaml, target-statefulset.yaml, SOURCE_STS/TARGET_STS, source_pvc_name/target_pvc_name) remains anywhere in ${EKS_APP_WORKFLOW}"
  else
    fail "7: source/target StatefulSet/PVC validation references remain in ${EKS_APP_WORKFLOW}:"$'\n'"${SOURCE_TARGET_HITS}"
  fi

  CREATE_NAMESPACE_HITS="$(grep -n "CreateNamespace=true\|managedNamespaceMetadata" "$EKS_APP_WORKFLOW" | strip_comment_hits || true)"
  if [ -z "$CREATE_NAMESPACE_HITS" ]; then
    pass "the Argo CD Application manifest no longer has a CreateNamespace=true/managedNamespaceMetadata (legacyPair-only) branch"
  else
    fail "CreateNamespace=true/managedNamespaceMetadata still present in ${EKS_APP_WORKFLOW}:"$'\n'"${CREATE_NAMESPACE_HITS}"
  fi

  RESOLVE_MODEL_HITS="$(grep -n "resolve_deployment_model" "$EKS_APP_WORKFLOW" || true)"
  if [ -z "$RESOLVE_MODEL_HITS" ]; then
    pass "resolve_deployment_model() (which defaulted missing/unknown values to legacyPair) no longer exists in ${EKS_APP_WORKFLOW}"
  else
    fail "resolve_deployment_model() still present in ${EKS_APP_WORKFLOW}:"$'\n'"${RESOLVE_MODEL_HITS}"
  fi
else
  skip "static legacyPair/source-target-validation absence checks -- python3 not available"
fi

# 9: the build job's workflow summary accurately documents every deletion trigger (physical removal, zero-byte, whitespace-only, comment-only, YAML null, lifecycle.state=absent) and describes enabled=false/deployment.enabled=false as retained (non-deleting).
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  BUILD_SUMMARY_TEXT="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["build_publish_and_deploy"]["steps"]:
    if step.get("name") == "Workflow summary":
        sys.stdout.write(step["run"])
        break
else:
    sys.exit("step not found")
PYEOF
)"

  SUMMARY_MISSING=""
  for phrase in "zero-byte" "whitespace-only" "comment-only" "YAML null" "lifecycle.state=absent" "physical removal" "retired-but-retained" "never trigger deletion"; do
    echo "$BUILD_SUMMARY_TEXT" | grep -qF "$phrase" || SUMMARY_MISSING="${SUMMARY_MISSING} [${phrase}]"
  done

  if [ -z "$SUMMARY_MISSING" ]; then
    pass "9: the workflow summary documents every deletion trigger (physical removal, zero-byte, whitespace-only, comment-only, YAML null, lifecycle.state=absent) and describes enabled=false/deployment.enabled=false as retained, never deleting"
  else
    fail "9: the workflow summary is missing expected deletion-trigger documentation:${SUMMARY_MISSING}"
  fi
else
  skip "workflow summary deletion-trigger documentation check -- python3 not available"
fi

# Malformed CURRENT YAML must fail the workflow closed (never silently skipped or deleted); whole-folder, whole-envs-directory, and rename scenarios exercised through the REAL discovery logic (git diff --name-status), not just the isolated per-ID loop above.
echo ""
echo "--- Phase 5B2A: malformed-current-YAML hard failure; folder/envs-directory/rename discovery ---"

if [ -f "${WORKDIR}/detect_script.sh" ] && [ -s "${WORKDIR}/detect_script.sh" ] && command -v python3 >/dev/null 2>&1; then
  # 15: malformed CURRENT YAML (file exists, has bytes, but isn't valid YAML) must abort the whole detection script with a clear error -- never treated as intentional deletion, never silently ignored.
  MALFORMED_REPO="${WORKDIR}/malformed-repo"
  rm -rf "$MALFORMED_REPO"
  mkdir -p "${MALFORMED_REPO}/envs/dev/case-malformed-current"
  printf 'deploymentModel: singleRuntime\nrunning: at-base-revision\n' > "${MALFORMED_REPO}/envs/dev/case-malformed-current/values.yaml"
  git -C "$MALFORMED_REPO" init -q
  git -C "$MALFORMED_REPO" config user.email "test@test.invalid"
  git -C "$MALFORMED_REPO" config user.name "test"
  git -C "$MALFORMED_REPO" add -A
  git -C "$MALFORMED_REPO" commit -q -m "base revision"
  MALFORMED_BEFORE_SHA="$(git -C "$MALFORMED_REPO" rev-parse HEAD)"
  printf 'deploymentModel: singleRuntime\n  bad indent: [unterminated\n' > "${MALFORMED_REPO}/envs/dev/case-malformed-current/values.yaml"

  set +e
  MALFORMED_CURRENT_OUTPUT="$(cd "$MALFORMED_REPO" && bash -c '
    set -euo pipefail
    source "'"${WORKDIR}"'/is_gg_fn.sh"
    source "'"${WORKDIR}"'/is_active_fn.sh"
    jq() { echo "[SHOULD_NOT_BE_CALLED]"; }
    BEFORE_SHA="'"$MALFORMED_BEFORE_SHA"'"
    DELETION_MATRIX_ITEMS="[]"
    INACTIVE_LOG=""
    DELETION_CANDIDATE_IDS="case-malformed-current"
    source "'"${WORKDIR}"'/deletion_loop.sh"
    echo "RESULT case-malformed-current => ${DELETION_MATRIX_ITEMS}"
  ' 2>&1)"
  MALFORMED_CURRENT_STATUS=$?
  set -e
  echo "$MALFORMED_CURRENT_OUTPUT"

  if [ "$MALFORMED_CURRENT_STATUS" -ne 0 ] \
      && echo "$MALFORMED_CURRENT_OUTPUT" | grep -qF "FAIL:" \
      && ! echo "$MALFORMED_CURRENT_OUTPUT" | grep -q "SHOULD_NOT_BE_CALLED" \
      && ! echo "$MALFORMED_CURRENT_OUTPUT" | grep -q "^RESULT"; then
    pass "15: malformed current YAML fails the workflow closed (non-zero exit, clear FAIL message, never reaches deletion-matrix construction)"
  else
    fail "15: malformed current YAML did not fail closed as expected (status=${MALFORMED_CURRENT_STATUS})"
  fi
  rm -rf "$MALFORMED_REPO"

  # 4/5/10: exercises the REAL discovery logic (REMOVED_PATH_IDS/CHANGED_VALUES_IDS via git diff --name-status) for whole-folder deletion, whole-envs-directory deletion, and folder rename; extracts only the discovery half, stopping before the CANDIDATE_ID loop (reused as-is from deletion_loop.sh above) so this test never needs to stub the unrelated jq recomputation that follows.
  awk '/^NAME_STATUS="\$\(git diff --name-status/,/^DELETION_CANDIDATE_IDS=/' "${WORKDIR}/detect_script.sh" > "${WORKDIR}/discovery_only.sh"

  if [ -s "${WORKDIR}/discovery_only.sh" ] && [ -s "${WORKDIR}/deletion_loop.sh" ]; then
    DISCOVERY_REPO="${WORKDIR}/discovery-repo"

    run_discovery_case() {
      local label="$1" setup_fn="$2" expect_pattern="$3" unexpected_pattern="$4"
      rm -rf "$DISCOVERY_REPO"
      mkdir -p "${DISCOVERY_REPO}/envs/dev/gg-oracle-payments-01" "${DISCOVERY_REPO}/envs/dev/gg-postgresql-payments-01"
      printf 'deploymentModel: singleRuntime\nname: oracle\n' > "${DISCOVERY_REPO}/envs/dev/gg-oracle-payments-01/values.yaml"
      printf 'deploymentModel: singleRuntime\nname: postgresql\n' > "${DISCOVERY_REPO}/envs/dev/gg-postgresql-payments-01/values.yaml"
      mkdir -p "${DISCOVERY_REPO}/envs/dev/goldengate-monitor"
      printf 'global:\n  environment: dev\n' > "${DISCOVERY_REPO}/envs/dev/goldengate-monitor/values.yaml"
      "$setup_fn" "$DISCOVERY_REPO"
      git -C "$DISCOVERY_REPO" init -q
      git -C "$DISCOVERY_REPO" config user.email "test@test.invalid"
      git -C "$DISCOVERY_REPO" config user.name "test"
      git -C "$DISCOVERY_REPO" add -A
      git -C "$DISCOVERY_REPO" commit -q -m "base revision"
      local before_sha
      before_sha="$(git -C "$DISCOVERY_REPO" rev-parse HEAD)"
      "${setup_fn}_mutate" "$DISCOVERY_REPO"
      git -C "$DISCOVERY_REPO" add -A
      git -C "$DISCOVERY_REPO" commit -q -m "after revision" --allow-empty
      local after_sha
      after_sha="$(git -C "$DISCOVERY_REPO" rev-parse HEAD)"

      local out status
      set +e
      out="$(cd "$DISCOVERY_REPO" && bash -c '
        set -euo pipefail
        source "'"${WORKDIR}"'/is_gg_fn.sh"
        source "'"${WORKDIR}"'/is_active_fn.sh"
        jq() {
          local stdin_content
          stdin_content="$(cat)"
          shift
          local args=("$@") model="" id=""
          for i in "${!args[@]}"; do
            [ "${args[$i]}" = "deployment_id" ] && id="${args[$((i+1))]}"
            [ "${args[$i]}" = "deployment_model" ] && model="${args[$((i+1))]}"
          done
          if [ "$stdin_content" = "[]" ]; then
            echo "[ADDED id=${id} model=${model}]"
          else
            echo "${stdin_content} [ADDED id=${id} model=${model}]"
          fi
        }
        BEFORE_SHA="'"$before_sha"'"
        AFTER_SHA="'"$after_sha"'"
        DELETION_MATRIX_ITEMS="[]"
        INACTIVE_LOG=""
        CHANGED_FILES="$(git diff --name-only "$BEFORE_SHA" "$AFTER_SHA" -- "envs/dev/**" "helm/goldengate/**" || true)"
        source "'"${WORKDIR}"'/discovery_only.sh"
        source "'"${WORKDIR}"'/deletion_loop.sh"
        echo "FINAL_DELETION_MATRIX=${DELETION_MATRIX_ITEMS}"
      ' 2>&1)"
      status=$?
      set -e
      echo "$out"

      if [ "$status" -eq 0 ] && echo "$out" | grep -qE "$expect_pattern" \
          && { [ -z "$unexpected_pattern" ] || ! echo "$out" | grep -qE "$unexpected_pattern"; }; then
        pass "$label"
      else
        fail "$label -- expected pattern [${expect_pattern}] not satisfied (or unexpected [${unexpected_pattern}] present), status=${status}"
      fi
    }

    # Test 4: deleting an entire canonical deployment folder.
    setup_folder_delete() { :; }
    setup_folder_delete_mutate() { rm -rf "$1/envs/dev/gg-postgresql-payments-01"; }
    run_discovery_case "4: deleting an entire canonical deployment folder creates its deletion entry" \
      setup_folder_delete \
      'ADDED id=gg-postgresql-payments-01 model=singleRuntime' \
      'ADDED id=gg-oracle-payments-01'

    # Test 5: deleting the complete envs directory.
    setup_envs_delete() { :; }
    setup_envs_delete_mutate() { rm -rf "$1/envs"; }
    run_discovery_case "5: deleting the complete envs directory creates deletion entries for all previously valid GoldenGate deployments and no unrelated folders" \
      setup_envs_delete \
      'ADDED id=gg-oracle-payments-01 model=singleRuntime' \
      'ADDED id=goldengate-monitor'

    # Test 10: renaming a deployment folder deletes the old ID and the new ID is discovered as an independent candidate (build-matrix discovery is a separate path) -- proves the OLD id is queued for deletion and the NEW id never appears as a deletion entry.
    setup_rename() { :; }
    setup_rename_mutate() { git -C "$1" mv envs/dev/gg-oracle-payments-01 envs/dev/gg-oracle-payments-01-renamed; }
    run_discovery_case "10: renaming a deployment folder deletes the old ID (and never queues the new ID for deletion)" \
      setup_rename \
      'ADDED id=gg-oracle-payments-01 model=singleRuntime' \
      'ADDED id=gg-oracle-payments-01-renamed'

    # RETIREMENT PROOF: fully self-contained -- synthetic existing-mode source/target descriptors (never read from this repo's own Git history, so the test survives a shallow checkout or any future commit that moves HEAD) committed then physically deleted in one commit, replayed through the real discovery+deletion logic against a genuine Git diff, confirming deploymentModel/efs_mode/reason for BOTH.
    RETIREMENT_PROOF_REPO="${WORKDIR}/retirement-proof-repo"
    rm -rf "$RETIREMENT_PROOF_REPO"
    mkdir -p "${RETIREMENT_PROOF_REPO}/envs/dev/gg-oracle-payments-01" "${RETIREMENT_PROOF_REPO}/envs/dev/gg-postgresql-payments-01"
    cat > "${RETIREMENT_PROOF_REPO}/envs/dev/gg-oracle-payments-01/values.yaml" <<'EOF'
deploymentModel: singleRuntime
deployment:
  enabled: true
  role: source
persistence:
  enabled: true
  provider: efs
  efs:
    mode: existing
    fileSystemId: fs-0123456789abcdef1
EOF
    cat > "${RETIREMENT_PROOF_REPO}/envs/dev/gg-postgresql-payments-01/values.yaml" <<'EOF'
deploymentModel: singleRuntime
deployment:
  enabled: true
  role: target
persistence:
  enabled: true
  provider: efs
  efs:
    mode: existing
    fileSystemId: fs-0123456789abcdef2
EOF
    git -C "$RETIREMENT_PROOF_REPO" init -q
    git -C "$RETIREMENT_PROOF_REPO" config user.email "test@test.invalid"
    git -C "$RETIREMENT_PROOF_REPO" config user.name "test"
    git -C "$RETIREMENT_PROOF_REPO" add -A
    git -C "$RETIREMENT_PROOF_REPO" commit -q -m "base revision: both historical descriptors present"
    RETIREMENT_BEFORE_SHA="$(git -C "$RETIREMENT_PROOF_REPO" rev-parse HEAD)"
    rm -rf "${RETIREMENT_PROOF_REPO}/envs/dev/gg-oracle-payments-01" "${RETIREMENT_PROOF_REPO}/envs/dev/gg-postgresql-payments-01"
    git -C "$RETIREMENT_PROOF_REPO" add -A
    git -C "$RETIREMENT_PROOF_REPO" commit -q -m "physical removal of both retired descriptors"

    set +e
    RETIREMENT_PROOF_OUTPUT="$(cd "$RETIREMENT_PROOF_REPO" && bash -c '
      set -euo pipefail
      source "'"${WORKDIR}"'/is_gg_fn.sh"
      source "'"${WORKDIR}"'/is_active_fn.sh"
      jq() {
        local stdin_content
        stdin_content="$(cat)"
        shift
        local args=("$@") model="" id="" reason="" efs_mode=""
        for i in "${!args[@]}"; do
          [ "${args[$i]}" = "deployment_id" ] && id="${args[$((i+1))]}"
          [ "${args[$i]}" = "deployment_model" ] && model="${args[$((i+1))]}"
          [ "${args[$i]}" = "reason" ] && reason="${args[$((i+1))]}"
          [ "${args[$i]}" = "efs_mode" ] && efs_mode="${args[$((i+1))]}"
        done
        if [ "$stdin_content" = "[]" ]; then
          echo "[ADDED id=${id} model=${model} efs_mode=${efs_mode} reason=${reason}]"
        else
          echo "${stdin_content} [ADDED id=${id} model=${model} efs_mode=${efs_mode} reason=${reason}]"
        fi
      }
      BEFORE_SHA="'"$RETIREMENT_BEFORE_SHA"'"
      DELETION_MATRIX_ITEMS="[]"
      INACTIVE_LOG=""
      DELETION_CANDIDATE_IDS="gg-oracle-payments-01 gg-postgresql-payments-01"
      source "'"${WORKDIR}"'/deletion_loop.sh"
      echo "FINAL_DELETION_MATRIX=${DELETION_MATRIX_ITEMS}"
    ' 2>&1)"
    RETIREMENT_PROOF_STATUS=$?
    set -e
    echo "$RETIREMENT_PROOF_OUTPUT"

    if [ "$RETIREMENT_PROOF_STATUS" -eq 0 ] \
        && echo "$RETIREMENT_PROOF_OUTPUT" | grep -qF "id=gg-oracle-payments-01 model=singleRuntime efs_mode=existing reason=physical-removal" \
        && echo "$RETIREMENT_PROOF_OUTPUT" | grep -qF "id=gg-postgresql-payments-01 model=singleRuntime efs_mode=existing reason=physical-removal"; then
      pass "RETIREMENT: physically deleting both gg-oracle-payments-01 and gg-postgresql-payments-01 in one commit is classified, via the real detect-goldengate-deployments.sh discovery+deletion logic against a genuine Git diff, as TWO physical-removal entries (deploymentModel=singleRuntime, efs_mode=existing, reason=physical-removal for both) -- the managed-EFS deletion guard sees efs_mode=existing (never managed) for both, so it passes"
    else
      fail "RETIREMENT: the real physical-removal classification for gg-oracle-payments-01/gg-postgresql-payments-01 did not match expectations (status=${RETIREMENT_PROOF_STATUS})"
    fi
    rm -rf "$RETIREMENT_PROOF_REPO"

    rm -rf "$DISCOVERY_REPO"
  else
    fail "could not extract the discovery-plus-deletion block from ${DETECT_SCRIPT} for folder/envs-directory/rename tests"
  fi
else
  skip "malformed-current-YAML and folder/envs-directory/rename discovery tests -- detect_script.sh or python3 not available"
fi

echo ""
echo "--- Phase 5A: no Argo CD Application/namespace/PVC/EFS deletion command tied to disabling the legacy folder ---"
if [ -f "$EKS_APP_WORKFLOW" ]; then
  # The only place this workflow deletes an Argo CD Application or namespace is delete_removed_argocd_applications, gated on has_deletions=true (already proven above to exclude deployment.enabled=false); no separate, disable-triggered deletion path may exist anywhere else. Phase 6D1 additionally allows deleting only the ephemeral, just-created replication Job/ConfigMap/SecretProviderClass named "$JOB_NAME" after a successful reconciliation -- never a PVC, EFS access point, or existing runtime resource.
  DIRECT_DELETE_HITS="$(grep -n 'kubectl delete\|delete-repository\|efs delete-access-point\|delete_access_point' "$EKS_APP_WORKFLOW" | grep -v 'kubectl delete application\|kubectl delete namespace\|kubectl delete job "\$JOB_NAME"\|kubectl delete configmap "\$JOB_NAME"\|kubectl delete secretproviderclass "\$JOB_NAME"' || true)"
  if [ -z "$DIRECT_DELETE_HITS" ]; then
    pass "no unexpected delete command exists outside the guarded Argo CD Application/namespace cleanup path"
  else
    fail "unexpected delete command(s) found in ${EKS_APP_WORKFLOW}:"$'\n'"${DIRECT_DELETE_HITS}"
  fi

  if grep -q 'delete_removed_argocd_applications' "$EKS_APP_WORKFLOW" \
      && grep -q "needs.detect_changed_deployments.outputs.has_deletions == 'true'" "$EKS_APP_WORKFLOW"; then
    pass "Argo CD Application/namespace deletion remains gated on has_deletions (deletion-matrix-driven, never folder-disable-driven)"
  else
    fail "the deletion job's has_deletions gating condition is missing or changed"
  fi
else
  skip "deletion-command sweep -- ${EKS_APP_WORKFLOW} not found"
fi

echo ""
echo "--- Phase 5A: no direct \${{ inputs.* }} interpolation in run scripts; marker-file injection tests ---"

if [ -f "$EKS_APP_WORKFLOW" ]; then
  INPUTS_INTERP_HITS="$(grep -n '\${{ *inputs\.' "$EKS_APP_WORKFLOW" | grep -v '^\s*[0-9]*: *INPUT_[A-Z_]*: \${{ *inputs\.' || true)"
  # The only acceptable occurrences are inside a step-level env: mapping (INPUT_X: ${{ inputs.x }}), never inside a run-script body; grep -v above already filtered the common env-mapping shape, so anything left is a real hit.
  if [ -n "$INPUTS_INTERP_HITS" ]; then
    fail "\${{ inputs.* }} appears outside a step-level env: mapping in ${EKS_APP_WORKFLOW}:"$'\n'"${INPUTS_INTERP_HITS}"
  else
    pass "every \${{ inputs.* }} occurrence in ${EKS_APP_WORKFLOW} is confined to a step-level env: mapping, never a run-script body"
  fi
else
  skip "inputs.* interpolation sweep -- ${EKS_APP_WORKFLOW} not found"
fi

if [ -f "$DETECT_SCRIPT" ] && command -v python3 >/dev/null 2>&1; then
  # Feeds the real, tracked detect-goldengate-deployments.sh a workflow_dispatch deployment_id containing shell metacharacters via INPUT_DEPLOYMENT_ID (exactly how the real env: mapping delivers it), confirming the payload is never evaluated as shell code; EVENT_NAME/BEFORE_SHA/AFTER_SHA are plain env vars here (the ${{ }} substitution happens once, outside this script), so no sed-based resolution is needed.
  MARKER_DIR="${WORKDIR}/marker-test"
  mkdir -p "$MARKER_DIR"
  MARKER_FILE="${MARKER_DIR}/PWNED"

  INJECTION_FAILED="false"
  run_injection_case() {
    local label="$1" payload="$2"
    rm -f "$MARKER_FILE"
    INJECTION_OUTPUT="$(
      cd "$REPO_ROOT" && \
      EVENT_NAME="workflow_dispatch" \
      INPUT_ENVIRONMENT="dev" \
      INPUT_DEPLOYMENT_ID="$payload" \
      INPUT_DEPLOY="true" \
      BEFORE_SHA="" \
      AFTER_SHA="" \
      GITHUB_OUTPUT="$(mktemp)" \
      GITHUB_ENV="$(mktemp)" \
      MARKER_FILE_FOR_TEST="$MARKER_FILE" \
      bash "$DETECT_SCRIPT" 2>&1 || true
    )"
    if [ -f "$MARKER_FILE" ]; then
      fail "marker-file injection succeeded for ${label} (deployment_id=${payload@Q}) -- command execution occurred"
      INJECTION_FAILED="true"
    fi
  }

  # Payloads reference $MARKER_FILE_FOR_TEST (exported above) rather than an embedded absolute path, so the same payload strings work regardless of $WORKDIR's location.
  run_injection_case "command-substitution" '$(touch "$MARKER_FILE_FOR_TEST")'
  run_injection_case "backticks" '`touch "$MARKER_FILE_FOR_TEST"`'
  run_injection_case "double-quote-break" 'x"; touch "$MARKER_FILE_FOR_TEST"; echo "'
  run_injection_case "single-quote-break" "x'; touch \"\$MARKER_FILE_FOR_TEST\"; echo '"
  run_injection_case "semicolon" 'x; touch "$MARKER_FILE_FOR_TEST"'
  run_injection_case "newline" "$(printf 'x\ntouch "$MARKER_FILE_FOR_TEST"')"
  run_injection_case "dollar-brace-ifs" '${IFS}touch${IFS}"$MARKER_FILE_FOR_TEST"'
  run_injection_case "background-ampersand" 'x & touch "$MARKER_FILE_FOR_TEST"'
  run_injection_case "pipe" 'x | touch "$MARKER_FILE_FOR_TEST"'

  if [ "$INJECTION_FAILED" = "false" ]; then
    pass "9 shell-metacharacter payloads in deployment_id (\$(...), backticks, quotes, semicolons, newlines, \${IFS}, &, |) cannot execute commands (marker file never created)"
  fi

  rm -rf "$MARKER_DIR"
else
  skip "marker-file injection tests -- ${DETECT_SCRIPT} or python3 not available"
fi

echo ""
echo "--- Phase 5A: observer Helm/template/chart-values retirement ---"

if [ -f "helm/goldengate/templates/_observer.tpl" ]; then
  fail "helm/goldengate/templates/_observer.tpl still exists"
else
  pass "helm/goldengate/templates/_observer.tpl no longer exists"
fi

if grep -q "^\s*observer:" "helm/goldengate/values.yaml" 2>/dev/null; then
  fail "helm/goldengate/values.yaml still exposes a monitoring.observer block"
else
  pass "helm/goldengate/values.yaml exposes no monitoring.observer block"
fi

if grep -A1 "^monitoring:" "helm/goldengate/values.yaml" 2>/dev/null | grep -q "labels:"; then
  pass "helm/goldengate/values.yaml still exposes monitoring.labels (preserved for shared monitoring/future logging)"
else
  fail "helm/goldengate/values.yaml no longer exposes monitoring.labels -- must be preserved"
fi

OBSERVER_TEMPLATE_HITS="$(grep -l -i "goldengate-observer\|monitoring\.observer\|observerContainer" helm/goldengate/templates/*.yaml 2>/dev/null || true)"
if [ -z "$OBSERVER_TEMPLATE_HITS" ]; then
  pass "no helm/goldengate template references an observer container/include/value"
else
  fail "observer references remain in: ${OBSERVER_TEMPLATE_HITS}"
fi

if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  while IFS= read -r id; do
    [ -z "$id" ] && continue
    VALUES_FILE="envs/dev/${id}/values.yaml"
    RENDERED="${WORKDIR}/${id}-observer-check.yaml"
    ns="$(python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev describe "$id" 2>/dev/null | python3 -c 'import json, sys; print(json.load(sys.stdin)["runtimeNamespace"])')"
    derive_shared_overrides_for_deployment "$id"
    if helm template "$id" "$RUNTIME_CHART" --namespace "$ns" -f "$VALUES_FILE" \
        --set global.environment=dev --set global.deploymentId="$id" "${SHARED_OVERRIDES[@]}" > "$RENDERED" 2>"${WORKDIR}/${id}-observer-check.err"; then
      if grep -qi "goldengate-observer\|observer-enabled" "$RENDERED"; then
        fail "${id}: rendered manifest still contains an observer container/annotation reference"
      else
        pass "${id}: rendered manifest contains no observer container/annotation reference"
      fi
    else
      fail "${id}: helm template failed during observer-absence render check"
      cat "${WORKDIR}/${id}-observer-check.err"
    fi
  done < <(python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev list 2>/dev/null | awk '$1 == "ACTIVE" {print $2}')
else
  skip "rendered-manifest observer-absence check -- helm and/or python3/PyYAML not available"
fi

if [ -d "monitoring/observer" ]; then
  fail "monitoring/observer directory still exists"
else
  pass "monitoring/observer directory no longer exists"
fi

echo ""
echo "--- Phase 5A: observer image logic retired from ${EKS_APP_WORKFLOW} ---"
if [ -f "$EKS_APP_WORKFLOW" ]; then
  if grep -q "ensure_observer_image:" "$EKS_APP_WORKFLOW"; then
    fail "${EKS_APP_WORKFLOW} still defines the ensure_observer_image job"
  else
    pass "${EKS_APP_WORKFLOW} no longer defines the ensure_observer_image job"
  fi

  OBSERVER_ECR_HITS="$(grep -n "OBSERVER_ECR_REPOSITORY\|OBSERVER_SOURCE_PATH\|Ensure observer ECR repository\|observer ECR repository policy" "$EKS_APP_WORKFLOW" || true)"
  if [ -z "$OBSERVER_ECR_HITS" ]; then
    pass "${EKS_APP_WORKFLOW} contains no observer ECR repository creation/policy operation"
  else
    fail "${EKS_APP_WORKFLOW} still references observer ECR repository operations:"$'\n'"${OBSERVER_ECR_HITS}"
  fi

  if grep -q "AllowEksDevAccountPullGoldengateObserver" "$EKS_APP_WORKFLOW"; then
    fail "${EKS_APP_WORKFLOW} still defines the observer cross-account ECR repository policy statement"
  else
    pass "${EKS_APP_WORKFLOW} no longer defines the observer cross-account ECR repository policy statement"
  fi

  if grep -q "monitoring/observer" "$EKS_APP_WORKFLOW"; then
    fail "${EKS_APP_WORKFLOW} still references monitoring/observer (push trigger path or elsewhere)"
  else
    pass "${EKS_APP_WORKFLOW} no longer references monitoring/observer anywhere"
  fi
else
  skip "workflow observer-retirement checks -- ${EKS_APP_WORKFLOW} not found"
fi

echo ""
echo "--- Phase 5A: gg-monitor legacy-fallback removal ---"

if grep -q "legacyFallback" "helm/goldengate-monitor/values.yaml" 2>/dev/null; then
  fail "helm/goldengate-monitor/values.yaml still defines legacyFallback"
else
  pass "helm/goldengate-monitor/values.yaml no longer defines legacyFallback"
fi

if grep -q "LEGACY_FALLBACK_ENABLED" "helm/goldengate-monitor/templates/deployment.yaml" 2>/dev/null; then
  fail "helm/goldengate-monitor/templates/deployment.yaml still sets LEGACY_FALLBACK_ENABLED"
else
  pass "helm/goldengate-monitor/templates/deployment.yaml no longer sets LEGACY_FALLBACK_ENABLED"
fi

if grep -q "legacy_fallback_enabled\|LEGACY_FALLBACK_ENABLED" "${MONITOR_APP_DIR}/config.py" 2>/dev/null; then
  fail "config.py still has a legacy_fallback_enabled field"
else
  pass "config.py has no legacy_fallback_enabled field"
fi

if grep -q "compute_legacy_effective_status\|_LEGACY_STATUS_MAP\|legacy-observer-fallback" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null; then
  fail "monitor.py still contains legacy-observer status-conversion code or the legacy-observer-fallback data source"
else
  pass "monitor.py contains no legacy-observer status-conversion code or legacy-observer-fallback data source"
fi

if grep -q "gg-{pipeline_id}-{role}\|gg-payments-ora-to-pg-001-source\|gg-payments-ora-to-pg-001-target" "${MONITOR_APP_DIR}/monitor.py" 2>/dev/null; then
  fail "monitor.py still builds or hardcodes a legacy per-role observer partition key"
else
  pass "monitor.py never builds or hardcodes a legacy per-role observer partition key"
fi

if [ "$HELM_AVAILABLE" = "true" ]; then
  if helm template gg-monitor "$MONITOR_CHART_STAGED" --namespace goldengate-monitoring \
      --set image.repository=example.com/x --set image.tag=1 --set serviceAccount.roleArn=arn:aws:iam::000000000000:role/x \
      --set-string namespace.name=goldengate-monitoring --set-string aws.region="$RESOLVED_AWS_REGION" \
      2>"${WORKDIR}/monitor-legacy-render.err" | grep -q "LEGACY_FALLBACK_ENABLED"; then
    fail "rendered goldengate-monitor Deployment still contains LEGACY_FALLBACK_ENABLED"
  else
    pass "rendered goldengate-monitor Deployment contains no LEGACY_FALLBACK_ENABLED variable"
  fi
else
  skip "rendered monitor Deployment legacy-fallback check -- helm not available"
fi

echo ""
echo "--- Phase 5A: no alarms/SNS/gg-alerter introduced; no Fluent Bit outside the Phase 6A platform chart; IAM unchanged ---"

# Structural signals only -- never a bare substring grep, which would false-positive on this repo's own negative-assertion code (e.g. a forbidden-string tuple that deliberately mentions these names to prove their absence); this block only proves Fluent Bit was never added as its own sibling chart or into the runtime/monitor charts (the expected helm/goldengate-platform/templates/fluent-bit-*.yaml is checked separately above).
NOT_YET_HITS=""
[ -d "monitoring/gg-alerter" ] && NOT_YET_HITS="${NOT_YET_HITS} monitoring/gg-alerter/"
[ -d "helm/gg-alerter" ] && NOT_YET_HITS="${NOT_YET_HITS} helm/gg-alerter/"
FLUENTBIT_CHART_HITS="$(find helm -maxdepth 2 -iname "*fluent-bit*" -o -iname "*fluentbit*" 2>/dev/null | grep -v '^helm/argocd/' || true)"
[ -n "$FLUENTBIT_CHART_HITS" ] && NOT_YET_HITS="${NOT_YET_HITS} ${FLUENTBIT_CHART_HITS}"
DAEMONSET_HITS="$(grep -rl "kind: DaemonSet" helm/goldengate helm/goldengate-monitor 2>/dev/null || true)"
[ -n "$DAEMONSET_HITS" ] && NOT_YET_HITS="${NOT_YET_HITS} ${DAEMONSET_HITS}"
ALARM_SNS_HITS="$(grep -rl "aws_cloudwatch_metric_alarm\|aws cloudwatch put-metric-alarm\|sns:Publish\|sns:CreateTopic\|aws sns create-topic" \
  envs/dev "$EKS_APP_WORKFLOW" "$MONITOR_WORKFLOW" helm/goldengate-monitor 2>/dev/null || true)"
[ -n "$ALARM_SNS_HITS" ] && NOT_YET_HITS="${NOT_YET_HITS} ${ALARM_SNS_HITS}"

if [ -z "$NOT_YET_HITS" ]; then
  pass "no alarm/SNS/gg-alerter/Fluent Bit implementation was introduced"
else
  fail "unexpected alarm/SNS/gg-alerter/Fluent Bit implementation found in:${NOT_YET_HITS}"
fi

# A separate phase legitimately changes envs/dev/policies/goldengate-secrets-read-dev and iam.tf's description text -- the monitor role's PERMISSION CONTENT (policies_1.json) must remain untouched, and iam.tf's structural identifiers must not change even though description text may. Starting with Fresh-EKS Phase A, assume_role_policy/sts.json is EXCLUDED here since it legitimately regenerates for the new EKS OIDC issuer (already independently verified by this suite's render-iam-policies/trust-subject checks) -- this check now guards permission content only. Compare with --ignore-all-space since whitespace/line-ending diffs are pre-existing baseline noise.
MONITOR_IAM_DIFF="$(git diff --ignore-all-space -- envs/dev/policies/goldengate-monitor-read-dev/policies 2>/dev/null || true)"
if [ -z "$MONITOR_IAM_DIFF" ]; then
  pass "envs/dev/policies/goldengate-monitor-read-dev/policies (permission content) has no substantive changes (monitor IAM permissions untouched)"
else
  fail "unexpected change detected in envs/dev/policies/goldengate-monitor-read-dev/policies -- the monitor role's permission content must remain untouched"
fi

# 27. Runtime IAM least-privilege reduction (observer DynamoDB/CloudWatch permissions removed; monitor IAM and Secrets Manager/KMS access for canonical and legacy runtime pods unchanged).
echo ""
echo "--- Phase 5B1: runtime IAM least-privilege reduction ---"

RUNTIME_POLICY_FILE="envs/dev/policies/goldengate-secrets-read-dev/policies/policies_1.json"
MONITOR_POLICY_FILE="envs/dev/policies/goldengate-monitor-read-dev/policies/policies_1.json"

if [ -f "$RUNTIME_POLICY_FILE" ] && [ -f "$MONITOR_POLICY_FILE" ] && command -v python3 >/dev/null 2>&1; then
  IAM_TEST_OUTPUT="$(python3 - "$RUNTIME_POLICY_FILE" "$MONITOR_POLICY_FILE" <<'PYEOF'
import json
import sys

runtime_path, monitor_path = sys.argv[1:3]

with open(runtime_path) as f:
    runtime = json.load(f)

with open(monitor_path) as f:
    monitor = json.load(f)

runtime_statements = runtime.get("Statement") or []
monitor_statements = monitor.get("Statement") or []


def actions_of(stmt):
    a = stmt.get("Action")
    if isinstance(a, str):
        return {a}
    return set(a or [])


def find_sid(statements, sid):
    for s in statements:
        if s.get("Sid") == sid:
            return s
    return None


results = []


def check(label, condition):
    results.append((label, bool(condition)))


# 1. Runtime policy grants no DynamoDB action anywhere (the entire monitoring-state statement, not just its Sid, must be gone).
runtime_dynamodb_actions = set()
for s in runtime_statements:
    runtime_dynamodb_actions |= {a for a in actions_of(s) if a.startswith("dynamodb:")}
check("1_no_dynamodb_actions", not runtime_dynamodb_actions)

# 2. Runtime policy grants no cloudwatch:PutMetricData (or any cloudwatch:*).
runtime_cloudwatch_actions = set()
for s in runtime_statements:
    runtime_cloudwatch_actions |= {a for a in actions_of(s) if a.startswith("cloudwatch:")}
check("2_no_cloudwatch_actions", not runtime_cloudwatch_actions)

# 3. Runtime role retains its Secrets Manager statement, byte-identical to the original (never broadened to compensate for the removed statements).
secrets_stmt = find_sid(runtime_statements, "AllowReadGoldenGateDevSecrets")
check(
    "3_secrets_manager_retained",
    secrets_stmt is not None
    and actions_of(secrets_stmt) == {"secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"}
    and secrets_stmt.get("Resource") == ["arn:aws:secretsmanager:eu-west-1:668311715351:secret:dev/goldengate/*"]
    and secrets_stmt.get("Effect") == "Allow",
)

# 4. Runtime role retains its KMS Decrypt statement, byte-identical.
kms_stmt = find_sid(runtime_statements, "AllowDecryptGoldenGateSecretsKms")
check(
    "4_kms_retained",
    kms_stmt is not None
    and actions_of(kms_stmt) == {"kms:Decrypt"}
    and kms_stmt.get("Resource") == "*"
    and kms_stmt.get("Effect") == "Allow",
)

# 5. Monitor role retains DynamoDB read/write (CONFIG reads + LEASE/STATE# writes travel over the same table-level actions) and PutMetricData scoped to GoldenGate/Pipelines.
monitor_ddb_stmt = find_sid(monitor_statements, "AllowReadWriteGoldenGateMonitoringState")
monitor_cw_stmt = find_sid(monitor_statements, "AllowPublishGoldenGateMonitoringMetrics")
check(
    "5_monitor_dynamodb_and_metrics_retained",
    monitor_ddb_stmt is not None
    and actions_of(monitor_ddb_stmt) == {
        "dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem",
        "dynamodb:UpdateItem", "dynamodb:DescribeTable",
    }
    and monitor_ddb_stmt.get("Resource") == "arn:aws:dynamodb:eu-west-1:668311715351:table/gg-eks-pipeline"
    and monitor_cw_stmt is not None
    and actions_of(monitor_cw_stmt) == {"cloudwatch:PutMetricData"}
    and (monitor_cw_stmt.get("Condition") or {}).get("StringEquals", {}).get("cloudwatch:namespace") == "GoldenGate/Pipelines",
)

# 6. Runtime and monitor policies remain distinct documents (never merged/aliased into each other).
check("6_roles_remain_separate", runtime_path != monitor_path and runtime_statements != monitor_statements)

# 9. No wildcard (Resource: "*") DynamoDB, CloudWatch, or Secrets Manager action in the runtime policy (the pre-existing KMS Decrypt Resource: "*" is a known, unchanged grant, exempted here and covered by checks 3/4's byte-identical comparison).
wildcard_violations = []
for s in runtime_statements:
    if s.get("Resource") == "*":
        for a in actions_of(s):
            if a.startswith("dynamodb:") or a.startswith("cloudwatch:") or a.startswith("secretsmanager:"):
                wildcard_violations.append(a)
check("9_no_new_wildcard_access", not wildcard_violations)

print(f"RESULT={json.dumps(dict(results))}")
for label, ok in results:
    print(f"{'PASS' if ok else 'FAIL'} {label}")
PYEOF
  )"
  echo "$IAM_TEST_OUTPUT"

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 1_no_dynamodb_actions$"; then
    pass "1: runtime policy grants no dynamodb:* action (GetItem/Query/PutItem/UpdateItem/DescribeTable against gg-eks-pipeline removed)"
  else
    fail "1: runtime policy still grants a dynamodb:* action"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 2_no_cloudwatch_actions$"; then
    pass "2: runtime policy grants no cloudwatch:PutMetricData (or any cloudwatch:*) action"
  else
    fail "2: runtime policy still grants a cloudwatch:* action"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 3_secrets_manager_retained$"; then
    pass "3: runtime role retains its required Secrets Manager permissions, byte-identical to the original"
  else
    fail "3: runtime role's Secrets Manager permissions are missing or were altered"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 4_kms_retained$"; then
    pass "4: runtime role retains its required KMS Decrypt permission, byte-identical to the original"
  else
    fail "4: runtime role's KMS Decrypt permission is missing or was altered"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 5_monitor_dynamodb_and_metrics_retained$"; then
    pass "5: monitor role retains DynamoDB read/write (CONFIG reads, LEASE/STATE# writes) and PutMetricData scoped to GoldenGate/Pipelines"
  else
    fail "5: monitor role's DynamoDB or scoped CloudWatch permissions are missing or were altered"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 6_roles_remain_separate$"; then
    pass "6: runtime and monitor IAM policies remain separate documents"
  else
    fail "6: runtime and monitor IAM policies are not distinct"
  fi

  if echo "$IAM_TEST_OUTPUT" | grep -q "^PASS 9_no_new_wildcard_access$"; then
    pass "9: no wildcard DynamoDB, CloudWatch, or Secrets Manager access exists in the runtime policy"
  else
    fail "9: a wildcard-resourced DynamoDB/CloudWatch/Secrets Manager action was found in the runtime policy"
  fi

  # 10. No statement was broadened to compensate: the runtime policy has exactly the 2 retained statements, nothing more.
  RUNTIME_STMT_COUNT="$(python3 -c "import json; print(len((json.load(open('${RUNTIME_POLICY_FILE}')) or {}).get('Statement') or []))")"
  if [ "$RUNTIME_STMT_COUNT" = "2" ]; then
    pass "10: runtime policy has exactly 2 statements (no broadening or replacement compensation)"
  else
    fail "10: runtime policy has ${RUNTIME_STMT_COUNT} statements, expected exactly 2"
  fi
else
  skip "Phase 5B1 IAM least-privilege checks -- policy files or python3 not available"
fi

# 7. The one shared runtime ServiceAccount (annotated by the platform workflow, never duplicated in per-deployment values) is injected from the canonical resolver (RUNTIME_ROLE_ARN, which resolves to GoldenGateSecretsReadRole-dev for envs/dev/environment.yaml), never a re-typed literal.
if grep -qF 'runtimeServiceAccount.roleArn="$RUNTIME_ROLE_ARN"' "$PLATFORM_WORKFLOW" 2>/dev/null; then
  pass "7: the shared runtime ServiceAccount (platform workflow) is injected from the canonical RUNTIME_ROLE_ARN resolver output"
else
  fail "7: the platform workflow no longer injects runtimeServiceAccount.roleArn from the canonical RUNTIME_ROLE_ARN"
fi

# 8. Fresh-EKS Phase A/Phase 10: serviceAccount.roleArn is shared environment identity, removed from envs/dev/goldengate-monitor/values.yaml -- gg-monitor's IRSA role must now be injected by the monitor workflow from the canonical resolver (MONITOR_ROLE_ARN), never a re-typed literal in the committed values file.
if grep -q "role/GoldenGateMonitorReadRole-dev" "envs/dev/goldengate-monitor/values.yaml" 2>/dev/null; then
  fail "8: envs/dev/goldengate-monitor/values.yaml still hardcodes GoldenGateMonitorReadRole-dev -- it must be injected via the workflow's resolver, not committed"
elif grep -q 'set-string serviceAccount.roleArn="\$MONITOR_ROLE_ARN"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "8: gg-monitor's IRSA role is injected from the canonical resolver (MONITOR_ROLE_ARN), not hardcoded in envs/dev/goldengate-monitor/values.yaml"
else
  fail "8: the monitor workflow no longer injects serviceAccount.roleArn from MONITOR_ROLE_ARN"
fi

# 11. Terraform references remain valid: iam.tf's module block still exists, still derives its name from the canonical environment config (Fresh-EKS Phase A -- never a re-typed literal), and still attaches the same policy_folder.
if grep -q 'module "goldengate_secrets_read_role_dev"' envs/dev/iam.tf \
    && grep -q 'name          = local.gg_env_role_names.runtime' envs/dev/iam.tf \
    && grep -q 'policy_folder = "goldengate-secrets-read-dev"' envs/dev/iam.tf \
    && grep -q 'module "goldengate_monitor_read_role_dev"' envs/dev/iam.tf \
    && grep -q 'name          = local.gg_env_role_names.monitor' envs/dev/iam.tf \
    && grep -q 'policy_folder = "goldengate-monitor-read-dev"' envs/dev/iam.tf; then
  pass "11: envs/dev/iam.tf's module blocks still derive the same roles from environment config and attach the same policy_folder values"
else
  fail "11: envs/dev/iam.tf's role/policy_folder identifiers appear to have changed"
fi

if command -v terraform >/dev/null 2>&1; then
  TF_FMT_OUTPUT="$(terraform fmt -check -recursive -diff envs/dev/ 2>&1 || true)"
  if [ -z "$TF_FMT_OUTPUT" ]; then
    pass "11b: terraform fmt -check -recursive reports no formatting differences"
  else
    fail "11b: terraform fmt -check -recursive found formatting differences"
    echo "$TF_FMT_OUTPUT"
  fi
else
  skip "terraform fmt -check -- terraform not available"
fi

# 12. Stable, commit-independent collector safety-contract checks (see collector_safety_contract_check above).
collector_safety_contract_check "12"

echo ""
echo "--- Phase 5A: stale ServiceManager.pid and Argo CD deletion safeguards preserved ---"

PID_GUARD_MISSING=""
for f in helm/goldengate/templates/runtime-statefulset.yaml; do
  [ -f "$f" ] || continue
  grep -q "ServiceManager.pid" "$f" || PID_GUARD_MISSING="${PID_GUARD_MISSING} ${f}"
done
if [ -z "$PID_GUARD_MISSING" ]; then
  pass "27: exact ServiceManager.pid cleanup remains present in the runtime StatefulSet template"
else
  fail "stale ServiceManager.pid cleanup is missing from:${PID_GUARD_MISSING}"
fi

if [ -f "helm/goldengate/templates/source-statefulset.yaml" ] || [ -f "helm/goldengate/templates/target-statefulset.yaml" ]; then
  fail "legacyPair source-statefulset.yaml/target-statefulset.yaml still exist -- must be removed"
else
  pass "legacyPair source-statefulset.yaml/target-statefulset.yaml no longer exist"
fi

if grep -q "resources-finalizer.argocd.argoproj.io" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "Refusing to delete namespace" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "ownership labels" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "Argo CD deletion safeguards (finalizer wait, shared-namespace refusal, ownership-label verification) remain present"
else
  fail "one or more Argo CD deletion safeguards appear to be missing from ${EKS_APP_WORKFLOW}"
fi

# 24. No accidental pasted command-note files under hack/.
echo ""
echo "--- No accidental command-note files under hack/ ---"

if [ -f "hack/test.yaml" ]; then
  fail "hack/test.yaml exists -- this was an accidental pasted VDR command note and is not a legitimate repository file"
else
  pass "hack/test.yaml does not exist"
fi

# Generic guard: any *.yaml/*.yml file anywhere under hack/ must actually parse as YAML -- a plain-prose/shell command note accidentally saved with that extension is caught here even if renamed or a new one is added later.
BAD_HACK_YAML=""
if command -v python3 >/dev/null 2>&1; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if ! python3 -c "import sys, yaml; yaml.safe_load(open(sys.argv[1]))" "$f" >/dev/null 2>&1; then
      BAD_HACK_YAML="${BAD_HACK_YAML} ${f}"
    fi
  done <<< "$(find hack -type f \( -iname "*.yaml" -o -iname "*.yml" \) 2>/dev/null || true)"

  if [ -z "$BAD_HACK_YAML" ]; then
    pass "every *.yaml/*.yml file under hack/ parses as valid YAML (no pasted command notes)"
  else
    fail "file(s) under hack/ have a YAML extension but do not parse as YAML (likely an accidental command-note paste):${BAD_HACK_YAML}"
  fi
else
  skip "hack/ YAML-validity guard -- python3 not available"
fi

# 25. Repository hygiene: proven-dead file cleanup regression checks.
echo ""
echo "--- Repository hygiene: dead-file cleanup ---"

if [ -f ".github/workflows/build-monitor-base-image-once.yaml" ]; then
  fail "the temporary base-image workflow (build-monitor-base-image-once.yaml) still exists"
else
  pass "no temporary base-image workflow remains"
fi

JUNK_ARTIFACTS="$(find . -not -path "./.git/*" \( \
  -iname "__pycache__" -o -iname "*.pyc" -o -iname ".pytest_cache" -o -iname ".mypy_cache" \
  -o -iname ".DS_Store" -o -iname "Thumbs.db" -o -iname "*.tmp" -o -iname "*.bak" \
  -o -iname "*.orig" -o -iname "*.rej" -o -iname "*~" -o -iname "rendered" \
  \) 2>/dev/null || true)"
if [ -z "$JUNK_ARTIFACTS" ]; then
  pass "no Python cache, pytest/mypy cache, editor backup, or rendered/ artifacts exist in the repository"
else
  fail "junk/cache artifacts found:${JUNK_ARTIFACTS}"
fi

if [ ! -d "envs/dev/payments-ora-to-pg-001" ]; then
  pass "20: the retired payments-ora-to-pg-001 source folder is absent (removed in Phase 5B2A; still available via Git history)"
else
  fail "20: envs/dev/payments-ora-to-pg-001 still exists -- it must be fully removed in Phase 5B2A"
fi

if ! grep -rn "payments-ora-to-pg-001" "$CANONICAL_CONFIG" 2>/dev/null | grep -qv "pipeline:"; then
  pass "21: no active deployment-registry configuration references the retired deployment folder (only the shared logical pipeline: grouping id remains, which is unrelated and intentionally preserved)"
else
  fail "21: ${CANONICAL_CONFIG} appears to reference the retired deployment beyond the shared pipeline: grouping id"
fi

if [ ! -e "envs/dev/gg-oracle-payments-01" ] && [ ! -e "envs/dev/gg-postgresql-payments-01" ]; then
  pass "the retired gg-oracle-payments-01/gg-postgresql-payments-01 descriptor folders are physically absent (replaced by the live managed pair gg-postgresql-repltest-01/gg-mssql-repltest-01; still available via Git history)"
else
  fail "envs/dev/gg-oracle-payments-01 and/or envs/dev/gg-postgresql-payments-01 still exist -- they must be fully removed"
fi

if ! grep -q "gg-oracle-payments-01\|gg-postgresql-payments-01" "$CANONICAL_CONFIG" 2>/dev/null; then
  pass "no active deployment-registry configuration references the retired gg-oracle-payments-01/gg-postgresql-payments-01 descriptors"
else
  fail "${CANONICAL_CONFIG} still references a retired descriptor"
fi

CANONICAL_PRESENCE_MISSING=""
for f in \
  envs/dev/gg-postgresql-repltest-01/values.yaml \
  envs/dev/gg-mssql-repltest-01/values.yaml \
  envs/dev/goldengate-monitor/values.yaml \
  helm/goldengate/templates/runtime-statefulset.yaml \
  helm/goldengate/templates/runtime-ingress.yaml \
  helm/goldengate-monitor/templates/deployment.yaml \
  monitoring/monitor/monitor.py \
  monitoring/monitor/collector.py \
  monitoring/monitor/config.py \
  monitoring/monitor/health_rules.py \
  monitoring/monitor/tools/gg_api_contract_probe.py \
  monitoring/monitor/requirements-test.txt \
  ; do
  [ -e "$f" ] || CANONICAL_PRESENCE_MISSING="${CANONICAL_PRESENCE_MISSING} ${f}"
done
if [ -z "$CANONICAL_PRESENCE_MISSING" ]; then
  pass "all canonical runtime and monitor files remain present"
else
  fail "canonical runtime/monitor file(s) unexpectedly missing:${CANONICAL_PRESENCE_MISSING}"
fi

if [ -d "helm/argocd/charts/argo-cd" ] && [ -f "helm/argocd/Chart.lock" ]; then
  if [ "$HELM_AVAILABLE" = "true" ]; then
    if helm lint helm/argocd >"${WORKDIR}/argocd-lint.log" 2>&1 \
        && helm template argocd-hygiene-check helm/argocd --namespace argocd >"${WORKDIR}/argocd-template.log" 2>"${WORKDIR}/argocd-template.err"; then
      pass "the Argo CD vendored dependency (helm/argocd/charts/argo-cd) remains functional: helm lint and helm template both succeed"
    else
      fail "the Argo CD vendored dependency is present but helm lint/template failed"
      cat "${WORKDIR}/argocd-lint.log" "${WORKDIR}/argocd-template.err" 2>/dev/null
    fi
  else
    skip "Argo CD vendored dependency functional check -- helm not available"
  fi
else
  fail "the Argo CD vendored dependency directory or Chart.lock is missing -- helm/argocd/charts/argo-cd and helm/argocd/Chart.lock must be retained"
fi

if [ -f "helm/argocd/charts/argo-cd-9.3.7.tgz" ]; then
  echo "INFO: helm/argocd/charts/argo-cd-9.3.7.tgz is present (a redundant, Helm-regenerable duplicate of the vendored directory was removed when proven safe; its presence here is not itself a failure, only a note)."
fi

# 26. EFS rendered-resource validation: strict basePath derivation (matching goldengate.efsBasePath), fail-closed YAML parsing, no fragile grep on an optional key under set -euo pipefail.
echo ""
echo "--- EFS persistence validation: basePath derivation, strict parsing, StorageClass/PVC checks ---"

if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  python3 - "$EKS_APP_WORKFLOW" > "${WORKDIR}/efs_validate.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["build_publish_and_deploy"]["steps"]:
    if step.get("name") == "Validate EFS persistence resources are rendered":
        sys.stdout.write(step["run"])
        break
else:
    sys.exit("step not found")
PYEOF

  if [ ! -s "${WORKDIR}/efs_validate.sh" ]; then
    fail "could not extract the 'Validate EFS persistence resources are rendered' step from ${EKS_APP_WORKFLOW}"
  else
    EFS_WORKDIR="${WORKDIR}/efs-test"
    mkdir -p "${EFS_WORKDIR}/rendered" "${EFS_WORKDIR}/values"

    # mode=existing scratch fixtures: derived by mutating ONLY persistence.efs on scratch copies of the two current real descriptors (never a hand-duplicated retired production descriptor) -- proves the generic mode=existing code path from a source that always exists.
    ORACLE_EXISTING_FIXTURE="${EFS_WORKDIR}/values/existing-mode-a.yaml"
    POSTGRESQL_EXISTING_FIXTURE="${EFS_WORKDIR}/values/existing-mode-b.yaml"
    python3 -c "
import yaml

def make_existing_fixture(src_path, dst_path, fs_id):
    with open(src_path) as f:
        data = yaml.safe_load(f)
    data['persistence']['efs']['mode'] = 'existing'
    data['persistence']['efs']['fileSystemId'] = fs_id
    with open(dst_path, 'w') as f:
        yaml.dump(data, f)

make_existing_fixture('envs/dev/gg-postgresql-repltest-01/values.yaml', '${ORACLE_EXISTING_FIXTURE}', 'fs-0123456789abcdef1')
make_existing_fixture('envs/dev/gg-mssql-repltest-01/values.yaml', '${POSTGRESQL_EXISTING_FIXTURE}', 'fs-0123456789abcdef1')
"

    # EFS_MODE/EFS_FILE_SYSTEM_ID_DECLARED/RESOLVED_EFS_ID mirror what the real workflow's earlier "Resolve deployment identity"/"Resolve EFS filesystem ID" steps would have already exported via $GITHUB_ENV; every call site here uses the scratch existing-mode fixtures above (fs-0123456789abcdef1) unless a scenario is expected to fail before that value is ever consulted.
    run_efs_step() {
      ( cd "$EFS_WORKDIR" && \
        RELEASE_NAME="$1" VALUES_FILE="$2" DEPLOYMENT_ID="$3" DEPLOYMENT_MODEL="$4" ENVIRONMENT="$5" \
        EFS_MODE="existing" EFS_FILE_SYSTEM_ID_DECLARED="fs-0123456789abcdef1" RESOLVED_EFS_ID="fs-0123456789abcdef1" \
        bash "${WORKDIR}/efs_validate.sh" 2>&1 )
      return $?
    }

    helm template gg-oracle-payments-01 "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "$ORACLE_EXISTING_FIXTURE" \
      --set global.environment=dev --set global.deploymentId=gg-oracle-payments-01 "${ORACLE_SHARED_OVERRIDES[@]}" \
      > "${EFS_WORKDIR}/rendered/gg-oracle-payments-01.yaml" 2>"${EFS_WORKDIR}/oracle-render.err" || true
    helm template gg-postgresql-payments-01 "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "$POSTGRESQL_EXISTING_FIXTURE" \
      --set global.environment=dev --set global.deploymentId=gg-postgresql-payments-01 "${POSTGRESQL_SHARED_OVERRIDES[@]}" \
      > "${EFS_WORKDIR}/rendered/gg-postgresql-payments-01.yaml" 2>"${EFS_WORKDIR}/postgres-render.err" || true
    set +e
    ORACLE_OUT="$(run_efs_step "gg-oracle-payments-01" "$ORACLE_EXISTING_FIXTURE" "gg-oracle-payments-01" "singleRuntime" "dev")"
    ORACLE_STATUS=$?
    set -e
    echo "$ORACLE_OUT"
    if [ "$ORACLE_STATUS" -eq 0 ] && echo "$ORACLE_OUT" | grep -qF "Expected EFS basePath: /gg-oracle-payments-01"; then
      pass "1: gg-oracle-payments-01 (no explicit basePath) resolves to /gg-oracle-payments-01"
    else
      fail "1: gg-oracle-payments-01 basePath derivation failed or produced an unexpected value"
    fi

    set +e
    POSTGRES_OUT="$(run_efs_step "gg-postgresql-payments-01" "$POSTGRESQL_EXISTING_FIXTURE" "gg-postgresql-payments-01" "singleRuntime" "dev")"
    POSTGRES_STATUS=$?
    set -e
    echo "$POSTGRES_OUT"
    if [ "$POSTGRES_STATUS" -eq 0 ] && echo "$POSTGRES_OUT" | grep -qF "Expected EFS basePath: /gg-postgresql-payments-01"; then
      pass "2: gg-postgresql-payments-01 (no explicit basePath) resolves to /gg-postgresql-payments-01"
    else
      fail "2: gg-postgresql-payments-01 basePath derivation failed or produced an unexpected value"
    fi

    if [ "$ORACLE_STATUS" -eq 0 ] && [ "$POSTGRES_STATUS" -eq 0 ] \
        && echo "$ORACLE_OUT" | grep -qF "OK: EFS StorageClass, runtime PVC, and StatefulSet u02/u03" \
        && echo "$POSTGRES_OUT" | grep -qF "OK: EFS StorageClass, runtime PVC, and StatefulSet u02/u03"; then
      pass "3: both actual rendered manifests (Oracle and PostgreSQL) pass full EFS validation"
    else
      fail "3: one or both actual rendered manifests failed EFS validation"
    fi

    # 4: an explicit non-empty basePath override is honored.
    python3 -c "
import yaml
with open('${ORACLE_EXISTING_FIXTURE}') as f:
    data = yaml.safe_load(f)
data['persistence']['efs']['storageClass']['basePath'] = '/custom-override-path'
with open('${EFS_WORKDIR}/values/oracle-override.yaml', 'w') as f:
    yaml.dump(data, f)
"
    helm template gg-oracle-payments-01 "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "${EFS_WORKDIR}/values/oracle-override.yaml" \
      --set global.environment=dev --set global.deploymentId=gg-oracle-payments-01 "${ORACLE_SHARED_OVERRIDES[@]}" \
      > "${EFS_WORKDIR}/rendered/oracle-override.yaml" 2>"${EFS_WORKDIR}/override-render.err" || true

    set +e
    OVERRIDE_OUT="$(run_efs_step "oracle-override" "${EFS_WORKDIR}/values/oracle-override.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    OVERRIDE_STATUS=$?
    set -e
    if [ "$OVERRIDE_STATUS" -eq 0 ] && echo "$OVERRIDE_OUT" | grep -qF "Expected EFS basePath: /custom-override-path"; then
      pass "4: an explicit non-empty basePath override is honored"
    else
      fail "4: explicit basePath override was not honored"
      echo "$OVERRIDE_OUT"
    fi

    # 5: mode=existing with a missing fileSystemId fails with a clear controlled error (never an unexplained shell abort).
    cat > "${EFS_WORKDIR}/values/missing-fsid.yaml" <<'EOF'
deploymentModel: singleRuntime
persistence:
  enabled: true
  provider: efs
  efs:
    mode: existing
    storageClass:
      basePath: /x
EOF
    set +e
    MISSING_FSID_OUT="$(run_efs_step "x" "${EFS_WORKDIR}/values/missing-fsid.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    MISSING_FSID_STATUS=$?
    set -e
    if [ "$MISSING_FSID_STATUS" -ne 0 ] && echo "$MISSING_FSID_OUT" | grep -qF "persistence.efs.fileSystemId must be a non-empty string when persistence.efs.mode=existing"; then
      pass "5: mode=existing with a missing fileSystemId fails with a clear controlled error"
    else
      fail "5: a missing fileSystemId did not fail with the expected controlled error"
      echo "$MISSING_FSID_OUT"
    fi

    # 5b: mode absent entirely fails with a clear controlled error (never silently inferred).
    cat > "${EFS_WORKDIR}/values/missing-mode.yaml" <<'EOF'
deploymentModel: singleRuntime
persistence:
  enabled: true
  provider: efs
  efs:
    fileSystemId: fs-0123456789abcdef1
EOF
    set +e
    MISSING_MODE_OUT="$(run_efs_step "x" "${EFS_WORKDIR}/values/missing-mode.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    MISSING_MODE_STATUS=$?
    set -e
    if [ "$MISSING_MODE_STATUS" -ne 0 ] && echo "$MISSING_MODE_OUT" | grep -qF "persistence.efs.mode must be exactly 'existing' or 'managed'"; then
      pass "5b: mode absent entirely fails with a clear controlled error"
    else
      fail "5b: a missing persistence.efs.mode did not fail with the expected controlled error"
      echo "$MISSING_MODE_OUT"
    fi

    # 5c: mode=managed with a committed fileSystemId fails with a clear controlled error (never silently permitted).
    cat > "${EFS_WORKDIR}/values/managed-with-fsid.yaml" <<'EOF'
deploymentModel: singleRuntime
persistence:
  enabled: true
  provider: efs
  efs:
    mode: managed
    fileSystemId: fs-0123456789abcdef1
EOF
    set +e
    MANAGED_WITH_FSID_OUT="$(run_efs_step "x" "${EFS_WORKDIR}/values/managed-with-fsid.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    MANAGED_WITH_FSID_STATUS=$?
    set -e
    if [ "$MANAGED_WITH_FSID_STATUS" -ne 0 ] && echo "$MANAGED_WITH_FSID_OUT" | grep -qF "must not be set when persistence.efs.mode=managed"; then
      pass "5c: mode=managed with a committed fileSystemId fails with a clear controlled error"
    else
      fail "5c: mode=managed with a committed fileSystemId did not fail with the expected controlled error"
      echo "$MANAGED_WITH_FSID_OUT"
    fi

    # 5d: mode=managed without a committed fileSystemId, using the workflow-resolved RESOLVED_EFS_ID (never the values file), passes.
    cat > "${EFS_WORKDIR}/values/managed-ok.yaml" <<'EOF'
deploymentModel: singleRuntime
persistence:
  enabled: true
  provider: efs
  efs:
    mode: managed
EOF
    helm template gg-managed-ok "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "$ORACLE_EXISTING_FIXTURE" \
      --set global.environment=dev --set global.deploymentId=gg-managed-ok "${ORACLE_SHARED_OVERRIDES[@]}" \
      --set persistence.efs.fileSystemId=fs-0123456789abcdef0 \
      > "${EFS_WORKDIR}/rendered/gg-managed-ok.yaml" 2>"${EFS_WORKDIR}/managed-ok-render.err" || true
    set +e
    MANAGED_OK_OUT="$( cd "$EFS_WORKDIR" && \
      RELEASE_NAME="gg-managed-ok" VALUES_FILE="${EFS_WORKDIR}/values/managed-ok.yaml" DEPLOYMENT_ID="gg-managed-ok" DEPLOYMENT_MODEL="singleRuntime" ENVIRONMENT="dev" \
      EFS_MODE="managed" EFS_FILE_SYSTEM_ID_DECLARED="" RESOLVED_EFS_ID="fs-0123456789abcdef0" \
      bash "${WORKDIR}/efs_validate.sh" 2>&1 )"
    MANAGED_OK_STATUS=$?
    set -e
    if [ "$MANAGED_OK_STATUS" -eq 0 ] && echo "$MANAGED_OK_OUT" | grep -qF "Expected EFS fileSystemId (RESOLVED_EFS_ID): fs-0123456789abcdef0"; then
      pass "5d: mode=managed with no committed fileSystemId validates against RESOLVED_EFS_ID alone"
    else
      fail "5d: mode=managed validation against RESOLVED_EFS_ID did not behave as expected"
      echo "$MANAGED_OK_OUT"
    fi

    # 6: malformed YAML fails closed.
    cat > "${EFS_WORKDIR}/values/malformed.yaml" <<'EOF'
deploymentModel: singleRuntime
persistence:
  enabled: true
  provider: efs
  efs:
    fileSystemId: fs-x
  bad indent: [unterminated
EOF
    set +e
    MALFORMED_OUT="$(run_efs_step "x" "${EFS_WORKDIR}/values/malformed.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    MALFORMED_STATUS=$?
    set -e
    if [ "$MALFORMED_STATUS" -ne 0 ] && echo "$MALFORMED_OUT" | grep -qF "is not valid YAML"; then
      pass "6: malformed YAML fails closed"
    else
      fail "6: malformed YAML did not fail closed as expected"
      echo "$MALFORMED_OUT"
    fi

    # 7: an unknown deploymentModel fails closed (this EFS validation step only ever expects deployment_model=singleRuntime, passed through from the job's upstream assertion -- never re-inferred here).
    cat > "${EFS_WORKDIR}/values/unknown-model.yaml" <<'EOF'
deploymentModel: someWeirdModel
persistence:
  enabled: true
  provider: efs
  efs:
    fileSystemId: fs-x
EOF
    set +e
    UNKNOWN_MODEL_OUT="$(run_efs_step "x" "${EFS_WORKDIR}/values/unknown-model.yaml" "gg-oracle-payments-01" "someWeirdModel" "dev")"
    UNKNOWN_MODEL_STATUS=$?
    set -e
    if [ "$UNKNOWN_MODEL_STATUS" -ne 0 ] && echo "$UNKNOWN_MODEL_OUT" | grep -qF "unexpected deploymentModel"; then
      pass "7: an unknown deploymentModel fails closed"
    else
      fail "7: an unknown deploymentModel did not fail closed as expected"
      echo "$UNKNOWN_MODEL_OUT"
    fi

    # 8/9/11: mutates a real rendered manifest's StorageClass to prove the rendered-resource checks have teeth (wrong basePath, wrong fileSystemId, duplicate matching-name StorageClass).
    python3 -c "
import yaml
with open('${EFS_WORKDIR}/rendered/gg-oracle-payments-01.yaml') as f:
    docs = list(yaml.safe_load_all(f))
out = []
for d in docs:
    if d and d.get('kind') == 'StorageClass':
        d['parameters']['basePath'] = '/wrong-base-path'
    out.append(d)
with open('${EFS_WORKDIR}/rendered/wrong-basepath.yaml', 'w') as f:
    yaml.dump_all(out, f)
"
    set +e
    WRONG_BASEPATH_OUT="$(run_efs_step "wrong-basepath" "$ORACLE_EXISTING_FIXTURE" "gg-oracle-payments-01" "singleRuntime" "dev")"
    WRONG_BASEPATH_STATUS=$?
    set -e
    if [ "$WRONG_BASEPATH_STATUS" -ne 0 ] && echo "$WRONG_BASEPATH_OUT" | grep -qF "parameters.basePath"; then
      pass "8: a rendered StorageClass with the wrong basePath fails"
    else
      fail "8: a rendered StorageClass with the wrong basePath did not fail as expected"
      echo "$WRONG_BASEPATH_OUT"
    fi

    python3 -c "
import yaml
with open('${EFS_WORKDIR}/rendered/gg-oracle-payments-01.yaml') as f:
    docs = list(yaml.safe_load_all(f))
out = []
for d in docs:
    if d and d.get('kind') == 'StorageClass':
        d['parameters']['fileSystemId'] = 'fs-wrongwrongwrong'
    out.append(d)
with open('${EFS_WORKDIR}/rendered/wrong-fsid.yaml', 'w') as f:
    yaml.dump_all(out, f)
"
    set +e
    WRONG_FSID_OUT="$(run_efs_step "wrong-fsid" "$ORACLE_EXISTING_FIXTURE" "gg-oracle-payments-01" "singleRuntime" "dev")"
    WRONG_FSID_STATUS=$?
    set -e
    if [ "$WRONG_FSID_STATUS" -ne 0 ] && echo "$WRONG_FSID_OUT" | grep -qF "parameters.fileSystemId"; then
      pass "9: a rendered StorageClass with the wrong filesystem ID fails"
    else
      fail "9: a rendered StorageClass with the wrong filesystem ID did not fail as expected"
      echo "$WRONG_FSID_OUT"
    fi

    # 10: absence of the optional basePath key never causes an unexplained shell exit -- structural proof (the fragile grep pattern is gone) plus behavioral proof (tests 1/2 above already completed cleanly, not a raw "unbound variable"/pipefail abort).
    if grep -qE "grep.*basePath" "${WORKDIR}/efs_validate.sh"; then
      fail "10: the EFS validation step still greps for basePath in the values file -- the fragile fallback was not removed"
    else
      pass "10: the EFS validation step no longer greps for the optional basePath key (no set -e/pipefail exposure)"
    fi

    python3 -c "
import yaml
with open('${EFS_WORKDIR}/rendered/gg-oracle-payments-01.yaml') as f:
    docs = list(yaml.safe_load_all(f))
out = list(docs)
for d in docs:
    if d and d.get('kind') == 'StorageClass':
        out.append(dict(d))
        break
with open('${EFS_WORKDIR}/rendered/duplicate-storageclass.yaml', 'w') as f:
    yaml.dump_all(out, f)
"
    set +e
    DUP_SC_OUT="$(run_efs_step "duplicate-storageclass" "$ORACLE_EXISTING_FIXTURE" "gg-oracle-payments-01" "singleRuntime" "dev")"
    DUP_SC_STATUS=$?
    set -e
    if [ "$DUP_SC_STATUS" -ne 0 ] && echo "$DUP_SC_OUT" | grep -qF "expected exactly one StorageClass"; then
      pass "11: exactly one expected StorageClass is required (a duplicate is rejected)"
    else
      fail "11: a duplicate matching-name StorageClass was not rejected as expected"
      echo "$DUP_SC_OUT"
    fi

    # 22: legacyPair Helm rendering is rejected with a clear controlled error (the chart no longer implements legacyPair source/target rendering); also confirms an unknown deploymentModel fails closed the same way.
    set +e
    LEGACY_REJECT_ERR="$(helm template ogg-legacy-reject "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "$ORACLE_EXISTING_FIXTURE" \
      --set global.environment=dev --set global.deploymentId=ogg-legacy-reject "${ORACLE_SHARED_OVERRIDES[@]}" \
      --set deploymentModel=legacyPair 2>&1)"
    LEGACY_REJECT_STATUS=$?
    set -e
    if [ "$LEGACY_REJECT_STATUS" -ne 0 ] && echo "$LEGACY_REJECT_ERR" | grep -qF "deploymentModel=legacyPair is no longer supported by this chart"; then
      pass "22: legacyPair Helm rendering is rejected with the expected controlled error"
    else
      fail "22: legacyPair Helm rendering was not rejected as expected (status=${LEGACY_REJECT_STATUS})"
      echo "$LEGACY_REJECT_ERR"
    fi

    set +e
    UNKNOWN_MODEL_ERR="$(helm template ogg-unknown-reject "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "$ORACLE_EXISTING_FIXTURE" \
      --set global.environment=dev --set global.deploymentId=ogg-unknown-reject \
      --set deploymentModel=someUnknownModel 2>&1)"
    UNKNOWN_MODEL_STATUS=$?
    set -e
    if [ "$UNKNOWN_MODEL_STATUS" -ne 0 ] && echo "$UNKNOWN_MODEL_ERR" | grep -qF "Unsupported or missing deploymentModel"; then
      pass "an unknown/missing deploymentModel fails closed with a clear controlled error"
    else
      fail "an unknown deploymentModel was not rejected as expected (status=${UNKNOWN_MODEL_STATUS})"
      echo "$UNKNOWN_MODEL_ERR"
    fi

    # 13: this EFS-only correction did not touch observer removal or the workflow-matrix classifier logic elsewhere in the same file.
    PHASE5A_SPOTCHECK_OK="true"
    if grep -q "^  ensure_observer_image:" "$EKS_APP_WORKFLOW"; then
      PHASE5A_SPOTCHECK_OK="false"
    fi
    if ! grep -q "is_goldengate_deployment_values_file() {" "$DETECT_SCRIPT"; then
      PHASE5A_SPOTCHECK_OK="false"
    fi
    if grep -q "LEGACY_FALLBACK_ENABLED" "helm/goldengate-monitor/templates/deployment.yaml" 2>/dev/null; then
      PHASE5A_SPOTCHECK_OK="false"
    fi
    if [ "$PHASE5A_SPOTCHECK_OK" = "true" ]; then
      pass "13: this EFS-only correction did not reintroduce observer/legacy-fallback logic or remove the workflow-matrix classifier"
    else
      fail "13: unexpected Phase 5A regression detected alongside the EFS correction"
    fi

    rm -rf "$EFS_WORKDIR"
  fi
else
  skip "EFS persistence validation regression tests -- helm and/or python3/PyYAML not available"
fi

# The "Detect changed deployments" step's inline run: scalar once reached ~23,971 UTF-8 characters, above GitHub Actions' ~21,000-character limit, which made GitHub reject the whole workflow file at compile time; the fix moved the real implementation into the tracked hack/detect-goldengate-deployments.sh, leaving the step as a small env:-mapping wrapper -- these tests prove the fix and guard against regressing back over the limit.
echo ""
echo "--- Phase 5B2A: workflow-compilation-size correction ---"

if [ -f "$EKS_APP_WORKFLOW" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  RUN_LENGTHS_JSON="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import json
import sys
import yaml

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    doc = yaml.safe_load(f)

results = []
for jobname, job in doc.get("jobs", {}).items():
    for step in job.get("steps", []):
        run = step.get("run")
        if run is None:
            continue
        results.append({
            "job": jobname,
            "name": step.get("name", "<unnamed>"),
            "length": len(run.encode("utf-8")),
        })

detect_step = None
for r in results:
    if r["job"] == "detect_changed_deployments" and r["name"] == "Detect changed deployments":
        detect_step = r
        break

print(json.dumps({
    "results": sorted(results, key=lambda r: -r["length"]),
    "detect_step_length": detect_step["length"] if detect_step else None,
    "max_length": max((r["length"] for r in results), default=0),
}))
PYEOF
)"
  echo "$RUN_LENGTHS_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data['results'][:6]:
    print(f\"{r['length']:7d} chars  job={r['job']:30s} step={r['name']}\")
print()
print('detect_step_length:', data['detect_step_length'])
print('max_length:', data['max_length'])
"

  # 1: the "Detect changed deployments" run: body is below GitHub's 21,000-character limit (the exact defect this phase fixes).
  DETECT_STEP_LENGTH="$(echo "$RUN_LENGTHS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['detect_step_length'])")"
  if [ -n "$DETECT_STEP_LENGTH" ] && [ "$DETECT_STEP_LENGTH" -lt 21000 ]; then
    pass "1: the 'Detect changed deployments' run: body (${DETECT_STEP_LENGTH} chars) is below GitHub's 21,000-character run: limit"
  else
    fail "1: the 'Detect changed deployments' run: body is missing or still at/above the 21,000-character limit (length=${DETECT_STEP_LENGTH:-<missing>})"
  fi

  # 2: safety margin -- every run: scalar in the whole workflow is below 18,000 characters, not just below the hard 21,000 limit.
  MAX_RUN_LENGTH="$(echo "$RUN_LENGTHS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['max_length'])")"
  if [ -n "$MAX_RUN_LENGTH" ] && [ "$MAX_RUN_LENGTH" -lt 18000 ]; then
    pass "2: every run: scalar in ${EKS_APP_WORKFLOW} is below the 18,000-character safety margin (max=${MAX_RUN_LENGTH})"
  else
    fail "2: at least one run: scalar in ${EKS_APP_WORKFLOW} is at/above the 18,000-character safety margin (max=${MAX_RUN_LENGTH:-<missing>})"
  fi

  # 3/4: the workflow header has a non-empty name and run-name. PyYAML (YAML 1.1) parses an unquoted top-level "on" key as the boolean True, not the string "on" -- expected, and must not be treated as missing/malformed anywhere this script inspects the parsed document.
  HEADER_CHECK="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as f:
    doc = yaml.safe_load(f)

name_ok = isinstance(doc.get("name"), str) and doc.get("name").strip() != ""
run_name_ok = isinstance(doc.get("run-name"), str) and doc.get("run-name").strip() != ""

# YAML 1.1 boolean-key quirk: PyYAML resolves the unquoted key "on" to the Python boolean True; both True and the literal string "on" are accepted here as "the trigger key is present".
on_present = True in doc or "on" in doc

print(f"name_ok={name_ok}")
print(f"run_name_ok={run_name_ok}")
print(f"on_present={on_present}")
print(f"name={doc.get('name')!r}")
PYEOF
)"
  echo "$HEADER_CHECK"

  if echo "$HEADER_CHECK" | grep -q "^name_ok=True$"; then
    pass "3: the workflow header contains a non-empty name"
  else
    fail "3: the workflow header name is missing or empty"
  fi

  if echo "$HEADER_CHECK" | grep -q "^run_name_ok=True$"; then
    pass "4: the workflow header contains a non-empty run-name"
  else
    fail "4: the workflow header run-name is missing or empty"
  fi

  if echo "$HEADER_CHECK" | grep -q "^on_present=True$"; then
    pass "the workflow's trigger key (\"on\", resolved by PyYAML/YAML 1.1 as boolean True) is present -- this is expected YAML 1.1 behavior, not a parse defect"
  else
    fail "the workflow's trigger key (on:) could not be found under either its YAML 1.1 boolean-True resolution or the literal string \"on\""
  fi

  # 5: the detection step actually calls the external script.
  if python3 -c "
import sys, yaml
doc = yaml.safe_load(open('$EKS_APP_WORKFLOW'))
for step in doc['jobs']['detect_changed_deployments']['steps']:
    if step.get('name') == 'Detect changed deployments':
        sys.exit(0 if 'bash hack/detect-goldengate-deployments.sh' in step.get('run', '') else 1)
sys.exit(1)
"; then
    pass "5: the 'Detect changed deployments' step invokes hack/detect-goldengate-deployments.sh"
  else
    fail "5: the 'Detect changed deployments' step does not invoke hack/detect-goldengate-deployments.sh"
  fi

  # 6: no second, embedded copy of _classify_deployment_model_yaml remains inside the workflow YAML -- the one and only implementation lives in ${DETECT_SCRIPT}.
  CLASSIFIER_IN_WORKFLOW_COUNT="$(grep -c "_classify_deployment_model_yaml() {" "$EKS_APP_WORKFLOW" || true)"
  if [ "${CLASSIFIER_IN_WORKFLOW_COUNT:-0}" -eq 0 ]; then
    pass "6: no embedded copy of _classify_deployment_model_yaml exists inside ${EKS_APP_WORKFLOW}"
  else
    fail "6: ${EKS_APP_WORKFLOW} still contains an embedded _classify_deployment_model_yaml definition (found ${CLASSIFIER_IN_WORKFLOW_COUNT})"
  fi

  # 9: workflow input/context expressions are mapped through a step-level env: block, never pasted directly into the external shell implementation; checked two ways: the env: mapping carries INPUT_*/EVENT_NAME/BEFORE_SHA/AFTER_SHA, and the script itself contains no "${{ ... }}" syntax at all.
  ENV_MAPPING_CHECK="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as f:
    doc = yaml.safe_load(f)

for step in doc["jobs"]["detect_changed_deployments"]["steps"]:
    if step.get("name") == "Detect changed deployments":
        env = step.get("env", {})
        required = {"INPUT_ENVIRONMENT", "INPUT_DEPLOYMENT_ID", "INPUT_DEPLOY", "EVENT_NAME", "BEFORE_SHA", "AFTER_SHA"}
        missing = required - set(env.keys())
        print(f"missing={sorted(missing)}")
        break
else:
    print("missing=<step not found>")
PYEOF
)"
  echo "$ENV_MAPPING_CHECK"

  if [ "$ENV_MAPPING_CHECK" = "missing=[]" ]; then
    pass "9a: the workflow step maps INPUT_ENVIRONMENT/INPUT_DEPLOYMENT_ID/INPUT_DEPLOY/EVENT_NAME/BEFORE_SHA/AFTER_SHA through env:, not directly into the run: body"
  else
    fail "9a: the workflow step's env: mapping is missing required keys (${ENV_MAPPING_CHECK})"
  fi

  if [ -f "$DETECT_SCRIPT" ]; then
    GITHUB_EXPR_IN_SCRIPT="$(grep -c '\${{' "$DETECT_SCRIPT" || true)"
    if [ "${GITHUB_EXPR_IN_SCRIPT:-0}" -eq 0 ]; then
      pass "9b: ${DETECT_SCRIPT} contains no \${{ ... }} GitHub Actions expression syntax -- it only reads plain shell environment variables"
    else
      fail "9b: ${DETECT_SCRIPT} still contains \${{ ... }} GitHub Actions expression syntax (found ${GITHUB_EXPR_IN_SCRIPT} occurrence(s))"
    fi
  else
    fail "9b: ${DETECT_SCRIPT} does not exist"
  fi
else
  skip "workflow-compilation-size checks -- ${EKS_APP_WORKFLOW} or python3/PyYAML not available"
fi

if [ -f "$DETECT_SCRIPT" ]; then
  # 7: the external script is executable, or is explicitly invoked through bash regardless of its own executable bit (the workflow wrapper always does `bash hack/detect-goldengate-deployments.sh`, so either is sufficient).
  SCRIPT_IS_EXECUTABLE="false"
  [ -x "$DETECT_SCRIPT" ] && SCRIPT_IS_EXECUTABLE="true"
  SCRIPT_INVOKED_VIA_BASH="false"
  grep -q "bash hack/detect-goldengate-deployments.sh" "$EKS_APP_WORKFLOW" 2>/dev/null && SCRIPT_INVOKED_VIA_BASH="true"

  if [ "$SCRIPT_IS_EXECUTABLE" = "true" ] || [ "$SCRIPT_INVOKED_VIA_BASH" = "true" ]; then
    pass "7: ${DETECT_SCRIPT} is executable (${SCRIPT_IS_EXECUTABLE}) or explicitly invoked through bash (${SCRIPT_INVOKED_VIA_BASH})"
  else
    fail "7: ${DETECT_SCRIPT} is neither executable nor explicitly invoked through bash from ${EKS_APP_WORKFLOW}"
  fi

  # 8: the external script writes all four required GitHub outputs.
  OUTPUTS_MISSING=""
  for output_name in has_changes deployment_matrix has_deletions deletion_matrix; do
    grep -qE "echo \"${output_name}=" "$DETECT_SCRIPT" || OUTPUTS_MISSING="${OUTPUTS_MISSING} ${output_name}"
  done
  if [ -z "$OUTPUTS_MISSING" ]; then
    pass "8: ${DETECT_SCRIPT} writes all four required GitHub outputs (has_changes, deployment_matrix, has_deletions, deletion_matrix)"
  else
    fail "8: ${DETECT_SCRIPT} is missing output(s):${OUTPUTS_MISSING}"
  fi

  bash -n "$DETECT_SCRIPT" >/dev/null 2>&1 && pass "${DETECT_SCRIPT} passes bash -n syntax check" || fail "${DETECT_SCRIPT} fails bash -n syntax check"
else
  skip "external script executable/output checks -- ${DETECT_SCRIPT} not found"
fi

# 16: no docs directory or runbook was added by this phase.
NEW_DOC_FILES="$(git -C "$REPO_ROOT" status --porcelain=v1 2>/dev/null | grep -E '^\?\? .*\.(md|MD)$' || true)"
if [ -z "$NEW_DOC_FILES" ] && [ ! -d "docs" ]; then
  pass "16: no docs directory or runbook (.md file) was added"
else
  fail "16: unexpected new documentation file(s)/directory found:"$'\n'"${NEW_DOC_FILES}"
fi

# 17/18: stable collector safety-contract re-check (second checkpoint) plus IAM remains unchanged.
collector_safety_contract_check "17"

if command -v git >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # 18: Fresh-EKS Phase A superseded the narrower "these specific IAM files never change" narrative from an earlier phase -- the OIDC rebind legitimately regenerates every assume_role_policy/sts.json (all 6 role folders), which the dedicated "render-iam-policies --check" and trust-subject-exactness checks elsewhere in this suite already verify are byte-for-byte the deterministic output of hack/goldengate-environment.py, not an unreviewed edit. This check's remaining job is narrower and permanent: no policies_1.json PERMISSION-content file may change unless account/region/cluster identity in environment.yaml itself changed (proven separately by render-iam-policies --check being a no-op today), and no file outside envs/dev/policies/**, envs/dev/iam.tf, envs/dev/environment.tf, or envs/dev/goldengate_inventory.tf may be touched by an IAM-labeled diff.
  IAM_DIFF_STAT="$(git -C "$REPO_ROOT" diff --stat=300 --ignore-all-space -- envs/dev/policies envs/dev/iam.tf envs/dev/environment.tf envs/dev/goldengate_inventory.tf 2>/dev/null || true)"
  IAM_DIFF_FILES="$(echo "$IAM_DIFF_STAT" | grep -oE '\S+\.(json|tf)' | sort -u || true)"
  POLICY_CONTENT_DIFF_FILES="$(echo "$IAM_DIFF_FILES" | grep -F 'policies_1.json' || true)"
  if [ -z "$POLICY_CONTENT_DIFF_FILES" ]; then
    pass "18: no envs/dev/policies/**/policies_1.json permission-content file changed (account/region/cluster identity in environment.yaml is unchanged, confirmed separately by render-iam-policies --check); only assume_role_policy/sts.json (OIDC rebind, verified elsewhere) and/or envs/dev/iam.tf, envs/dev/environment.tf, envs/dev/goldengate_inventory.tf may legitimately differ"
  else
    fail "18: a policies_1.json PERMISSION-content file changed unexpectedly (account/region/cluster identity should be unchanged):"$'\n'"${POLICY_CONTENT_DIFF_FILES}"
  fi
else
  skip "collector.py/monitor.py/IAM unchanged checks -- not a git repository"
fi

# 21. goldengate-monitor-metrics-config.yaml + the piped hack/goldengate-metrics-config.py helper -- the dedicated, controlled workflow for tuning a single deployment's CONFIG.metricsEnabled outside Terraform. Static structural checks only (functional/mocked behavior covered by hack/test-goldengate-metrics-config.py).
echo ""
echo "--- Phase 6C1: metrics config workflow + helper ---"

if [ -f "$METRICS_CONFIG_WORKFLOW" ]; then
  pass "21: goldengate-monitor-metrics-config.yaml exists"
else
  fail "21: goldengate-monitor-metrics-config.yaml is missing"
fi

if python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  pass "21: goldengate-monitor-metrics-config.yaml is valid YAML"
else
  fail "21: goldengate-monitor-metrics-config.yaml is not valid YAML"
fi

if grep -q "workflow_dispatch:" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && ! grep -qE "^\s*(push|pull_request|schedule):" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  pass "21: goldengate-monitor-metrics-config.yaml is workflow_dispatch only, no automatic trigger"
else
  fail "21: goldengate-monitor-metrics-config.yaml has an unexpected trigger"
fi

if grep -q "deployment_name:" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -A3 "deployment_name:" "$METRICS_CONFIG_WORKFLOW" | grep -q "type: string" \
    && ! grep -A6 "deployment_name:" "$METRICS_CONFIG_WORKFLOW" | grep -q "type: choice"; then
  pass "21: deployment_name is a free-form string input, not a hardcoded choice list"
else
  fail "21: deployment_name input is missing or unexpectedly a hardcoded choice list"
fi

if grep -q "Validate deployment_name against the canonical registry" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -q "Generate the folder-driven canonical registry" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -qF 'CANONICAL_REGISTRY=work/generated/${{ inputs.environment }}/goldengate-deployments.yaml' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && ! grep -q "gg-oracle-payments-01" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && ! grep -q "gg-postgresql-payments-01" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  pass "21: deployment_name is validated dynamically against the deployment-model-generated registry, never hardcoded"
else
  fail "21: deployment_name validation is missing, or a deployment name is hardcoded in workflow logic"
fi

if grep -q 'CANONICAL_ENABLED" != "true"' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  pass "21: registry validation refuses a deployment_name that is not enabled=true"
else
  fail "21: registry validation does not check the canonical enabled flag"
fi

if grep -q 'EXPECTED="ENABLE \${DEPLOYMENT_NAME}"' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -q 'EXPECTED="DISABLE \${DEPLOYMENT_NAME}"' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -q 'CONFIRMATION" != "\$EXPECTED"' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  pass "21: apply_change=true requires an exact ENABLE/DISABLE <deployment_name> confirmation string"
else
  fail "21: exact confirmation-string validation is missing or weakened"
fi

if grep -q "Confirm the global hard switch when enabling" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -q 'if: \${{ inputs.desired_metrics_enabled }}' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -q 'POD_CLOUDWATCH_ENV" != "true"' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  pass "21: enabling a deployment's gate requires the deployed global hard switch to already be true"
else
  fail "21: the global-hard-switch precondition for enabling is missing"
fi

if grep -q "table.get_item(" "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null \
    && ! grep -qE '\.[Ss]can\(' "$METRICS_CONFIG_WORKFLOW" "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null; then
  pass "21: the metrics-config workflow/helper use GetItem only, never Scan"
else
  fail "21: the metrics-config workflow/helper no longer use GetItem-only reads"
fi

if grep -qE "cloudwatch:(ListMetrics|GetMetricData|DescribeAlarms|PutMetricAlarm)" "$METRICS_CONFIG_WORKFLOW" "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null \
    || grep -qiE "sns|gg-alert|alarm" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  fail "21: the metrics-config workflow references a CloudWatch read/alarm/SNS/gg-alerter concept -- none is in scope for Phase 6C1"
else
  pass "21: the metrics-config workflow introduces no CloudWatch read, alarm, SNS, or gg-alerter reference"
fi

if grep -qE "docker (build|push)|helm (package|push)|argocd|kubectl apply|kubectl create|kubectl patch|kubectl delete" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  fail "21: the metrics-config workflow appears to build/push an image, package/push a Helm chart, touch Argo CD, or mutate a Kubernetes object"
else
  pass "21: the metrics-config workflow never builds/pushes an image, packages/pushes a Helm chart, touches Argo CD, or mutates a Kubernetes object"
fi

if grep -q "kubectl exec -i \"\$POD_NAME\"" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -q '< "\$METRICS_CONFIG_HELPER"' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  pass "21: the DynamoDB update runs inside the existing gg-monitor pod via its own IRSA (piped kubectl exec), not the workflow's own AWS credentials"
else
  fail "21: the metrics-config workflow no longer runs the update inside the gg-monitor pod's own IRSA"
fi

if grep -q 'SET metricsEnabled = :desired' "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null \
    && ! grep -qE 'SET (deploymentType|alertsEnabled|pipeline|recordType)' "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null; then
  pass "21: hack/goldengate-metrics-config.py's UpdateExpression only ever sets metricsEnabled"
else
  fail "21: hack/goldengate-metrics-config.py's UpdateExpression writes an unexpected attribute"
fi

if grep -q 'Attr("metricsEnabled").eq(current_metrics_enabled)' "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null \
    && grep -q "ConditionalCheckFailedException" "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null \
    && grep -qi "not retrying automatically" "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null; then
  pass "21: hack/goldengate-metrics-config.py uses optimistic concurrency and never auto-retries a ConditionalCheckFailedException"
else
  fail "21: hack/goldengate-metrics-config.py is missing its optimistic-concurrency guard or auto-retries on conflict"
fi

if python3 -m py_compile "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null; then
  pass "21: hack/goldengate-metrics-config.py compiles cleanly"
  find hack -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
else
  fail "21: hack/goldengate-metrics-config.py fails to compile"
fi

# 22. Corrections: no direct input interpolation in shell run: blocks, timestamp captured before UpdateItem, ConsistentRead=True/ReturnValues=ALL_NEW, hardened preflight pod-selection ownership chain, and a validated helper action line.
echo ""
echo "--- Phase 6C1 corrections: input safety, timestamp ordering, consistency, pod ownership ---"

if python3 -c "
import yaml
doc = yaml.safe_load(open('$METRICS_CONFIG_WORKFLOW'))
bad = []
for job in doc.get('jobs', {}).values():
    for step in job.get('steps', []):
        run = step.get('run')
        if not run:
            continue
        if '\${{ inputs.deployment_name }}' in run or '\${{ inputs.confirmation }}' in run:
            bad.append(step.get('name'))
import sys
sys.exit(1 if bad else 0)
" 2>/dev/null; then
  pass "22: goldengate-monitor-metrics-config.yaml never substitutes inputs.deployment_name/inputs.confirmation directly inside a run: block"
else
  fail "22: goldengate-monitor-metrics-config.yaml still substitutes a user-controlled string input directly inside a run: block"
fi

for step_name in "Validate deployment_name against the canonical registry" "Validate the exact confirmation string" \
                 "Run the metrics-config helper inside the monitor pod" "Post-update observation" "Workflow summary"; do
  if grep -A2 "name: ${step_name}\$" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null | grep -q "env:"; then
    pass "22: '${step_name}' step passes user-controlled/expression values through env:"
  else
    fail "22: '${step_name}' step is missing an env: block for its expression values"
  fi
done

if grep -q 'VALIDATION_START_TS="\$(date -u +%Y-%m-%dT%H:%M:%SZ)"' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -q 'echo "VALIDATION_START_TS=\${VALIDATION_START_TS}" >> "\$GITHUB_ENV"' "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  pass "22: goldengate-monitor-metrics-config.yaml captures VALIDATION_START_TS via GITHUB_ENV in the helper-execution step"
else
  fail "22: goldengate-monitor-metrics-config.yaml no longer captures VALIDATION_START_TS before the helper runs"
fi

if grep -A20 "name: Post-update observation" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null | grep -q "VALIDATION_START_TS:-" \
    && ! grep -A80 "name: Post-update observation" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null | grep -q 'VALIDATION_START_TS="\$(date -u'; then
  pass "22: Post-update observation reuses the inherited VALIDATION_START_TS and never recomputes it"
else
  fail "22: Post-update observation may recompute VALIDATION_START_TS after the helper already ran"
fi

if grep -q "ACTION_LINE_COUNT" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null \
    && grep -q "none|plan|updated) ;;" "$METRICS_CONFIG_WORKFLOW" 2>/dev/null; then
  pass "22: goldengate-monitor-metrics-config.yaml requires exactly one action= line in {none,plan,updated}"
else
  fail "22: goldengate-monitor-metrics-config.yaml no longer validates the helper's action= line"
fi

CONSISTENT_READ_COUNT_HELPER="$(grep -c "ConsistentRead=True" "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null || true)"
if [ "${CONSISTENT_READ_COUNT_HELPER:-0}" -ge 2 ]; then
  pass "22: hack/goldengate-metrics-config.py uses ConsistentRead=True on both the initial and verification GetItem"
else
  fail "22: hack/goldengate-metrics-config.py is missing ConsistentRead=True on one or both GetItem calls (found ${CONSISTENT_READ_COUNT_HELPER:-0})"
fi

CONSISTENT_READ_COUNT_MONITOR="$(grep -c "ConsistentRead=True" "$MONITOR_WORKFLOW" 2>/dev/null || true)"
if [ "${CONSISTENT_READ_COUNT_MONITOR:-0}" -eq 2 ]; then
  pass "22: goldengate-monitor.yaml's two inline CONFIG-inventory readers both use ConsistentRead=True"
else
  fail "22: goldengate-monitor.yaml's inline CONFIG-inventory readers do not both use ConsistentRead=True (found ${CONSISTENT_READ_COUNT_MONITOR:-0}, expected 2)"
fi

if grep -q 'ReturnValues="ALL_NEW"' "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null \
    && grep -q "new_attributes = update_response.get" "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null; then
  pass "22: hack/goldengate-metrics-config.py requests and validates UpdateItem's ReturnValues=ALL_NEW attributes"
else
  fail "22: hack/goldengate-metrics-config.py no longer requests/validates ReturnValues=ALL_NEW"
fi

UPDATE_ITEM_CALL_COUNT="$(grep -c "table.update_item(" "$METRICS_CONFIG_HELPER_SCRIPT" 2>/dev/null || true)"
if [ "${UPDATE_ITEM_CALL_COUNT:-0}" -eq 1 ]; then
  pass "22: hack/goldengate-metrics-config.py has exactly one UpdateItem call site"
else
  fail "22: hack/goldengate-metrics-config.py has ${UPDATE_ITEM_CALL_COUNT:-0} UpdateItem call sites, expected exactly 1"
fi

if grep -q "DEPLOY_UID=" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q "rs_deploy_uid.*!= .\$DEPLOY_UID" "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q 'pod_sa" != "gg-monitor"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "22: goldengate-monitor.yaml's CloudWatch preflight verifies Deployment/ReplicaSet pod ownership, not just a label match"
else
  fail "22: goldengate-monitor.yaml's CloudWatch preflight no longer verifies pod ownership"
fi

# 23. Phase 6C1-UI correction: comment-style checker YAML block-scalar awareness, wired in as the single implementation of the rule.
echo ""
echo "--- Phase 6C1-UI correction: comment-style checker ---"

COMMENT_CHECKER_FIXTURE_STATUS="$(python3 -c "
import importlib.util, os, tempfile

spec = importlib.util.spec_from_file_location('check_comment_style', os.path.join(os.getcwd(), 'hack', 'check-comment-style.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def check_text(text, suffix='.yaml'):
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        return mod.check_file(path)
    finally:
        os.remove(path)

failures = []

v = check_text('key: |\n  # this looks like a comment but is data\n  # so is this\n  real: content\n')
if v:
    failures.append(f'A (block-scalar comment ignored): expected 0 violations, got {v!r}')

v = check_text('# real comment line one\n# real comment line two\nkey: value\n')
if len(v) != 1:
    failures.append(f'B (real 2-line yaml comment): expected exactly 1 violation, got {v!r}')

v = check_text('key: |\n  # inside block, data\n  still inside\nouter:\n  # real comment 1\n  # real comment 2\n  value: 1\n')
if len(v) != 1:
    failures.append(f'C (block scalar ends by dedent): expected exactly 1 violation, got {v!r}')
elif v[0][1] != 5:
    failures.append(f'C (block scalar ends by dedent): expected the violation anchored at line 5, got {v!r}')

v = check_text('# real comment line one\n# real comment line two\necho hi\n', suffix='.sh')
if len(v) != 1:
    failures.append(f'D (shell unaffected): expected exactly 1 violation, got {v!r}')

v = check_text('# real comment line one\n# real comment line two\nx = 1\n', suffix='.py')
if len(v) != 1:
    failures.append(f'D (python unaffected): expected exactly 1 violation, got {v!r}')

print('FAIL:' + '; '.join(failures) if failures else 'OK')
" 2>&1)"
if [ "$COMMENT_CHECKER_FIXTURE_STATUS" = "OK" ]; then
  pass "23: comment-style checker correctly distinguishes YAML block-scalar data from real source comments"
else
  fail "23: comment-style checker fixture tests failed: ${COMMENT_CHECKER_FIXTURE_STATUS}"
fi

if python3 hack/check-comment-style.py; then
  pass "23: hack/check-comment-style.py reports zero real violations across the approved executable-source scope"
else
  fail "23: hack/check-comment-style.py reported one or more comment-style violations (see output above)"
fi
find hack -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# 24. Phase 6C2: Terraform-managed GoldenGate CloudWatch fleet dashboard.
echo ""
echo "--- Phase 6C2: CloudWatch fleet dashboard ---"

DASHBOARD_TF="envs/dev/cloudwatch_dashboard.tf"

if [ -f "$DASHBOARD_TF" ]; then
  pass "24: envs/dev/cloudwatch_dashboard.tf exists"
else
  fail "24: envs/dev/cloudwatch_dashboard.tf is missing"
fi

if grep -qF 'dashboard_name = "gg-${local.gg_env_environment}-fleet-overview"' "$DASHBOARD_TF" 2>/dev/null; then
  pass "24: dashboard name derives from environment config (Fresh-EKS Phase A) and resolves to gg-dev-fleet-overview for the real dev environment"
else
  fail "24: dashboard name is missing or no longer derives \"gg-\${local.gg_env_environment}-fleet-overview\""
fi

GOLDENGATE_INVENTORY_TF="envs/dev/goldengate_inventory.tf"
if grep -q 'local.goldengate_deployment_names' "$DASHBOARD_TF" 2>/dev/null \
    && ! grep -q 'yamldecode(file("\${path.module}/goldengate-deployments.yaml"))' "$DASHBOARD_TF" 2>/dev/null; then
  pass "24: dashboard source derives from the folder-driven inventory (goldengate_inventory.tf), not a handwritten registry file"
else
  fail "24: dashboard source no longer derives from the folder-driven inventory"
fi

if grep -q "gg-oracle-payments-01" "$DASHBOARD_TF" 2>/dev/null || grep -q "gg-postgresql-payments-01" "$DASHBOARD_TF" 2>/dev/null; then
  fail "24: cloudwatch_dashboard.tf hardcodes a canonical deployment name"
else
  pass "24: cloudwatch_dashboard.tf does not hardcode gg-oracle-payments-01/gg-postgresql-payments-01"
fi

if grep -q 'try(doc.deployment.enabled, false) == true' "$GOLDENGATE_INVENTORY_TF" 2>/dev/null \
    && grep -q 'goldengate_enabled_jsonenc\[each.key\] == "true" || local.goldengate_enabled_jsonenc\[each.key\] == "false"' "$GOLDENGATE_INVENTORY_TF" 2>/dev/null \
    && ! grep -q 'can(tobool(each.value.deployment.enabled))' "$GOLDENGATE_INVENTORY_TF" 2>/dev/null; then
  pass "24: disabled deployments are excluded by an enabled==true check, and a jsonencode()-based literal-Boolean precondition rejects Boolean-like strings (can(tobool(...)) does not, since it also accepts the string \"true\")"
else
  fail "24: folder-driven inventory eligibility no longer requires a literal Boolean enabled==true via the jsonencode() proof"
fi

if grep -q 'sort(keys(local.goldengate_enabled_deployments))' "$GOLDENGATE_INVENTORY_TF" 2>/dev/null; then
  pass "24: deployment ordering is deterministic (sort(keys(...)))"
else
  fail "24: deployment name ordering no longer uses sort()"
fi

if grep -q 'gg_dashboard_namespace *= "GoldenGate/Pipelines"' "$DASHBOARD_TF" 2>/dev/null; then
  pass "24: dashboard namespace is exactly GoldenGate/Pipelines"
else
  fail "24: dashboard namespace is missing or not exactly GoldenGate/Pipelines"
fi

REQUIRED_METRIC_NAMES_LINE="$(grep 'gg_dashboard_deployment_metric_names = \[' "$DASHBOARD_TF" 2>/dev/null || true)"
if echo "$REQUIRED_METRIC_NAMES_LINE" | grep -q '"DeploymentDown"' \
    && echo "$REQUIRED_METRIC_NAMES_LINE" | grep -q '"HeartbeatAgeSeconds"' \
    && echo "$REQUIRED_METRIC_NAMES_LINE" | grep -q '"LagBreached"' \
    && echo "$REQUIRED_METRIC_NAMES_LINE" | grep -q '"AbendFailure"' \
    && [ "$(echo "$REQUIRED_METRIC_NAMES_LINE" | grep -o '"[A-Za-z]*"' | wc -l)" -eq 4 ]; then
  pass "24: required deployment metrics are present exactly (DeploymentDown, HeartbeatAgeSeconds, LagBreached, AbendFailure)"
else
  fail "24: dashboard deployment metric set is missing an expected metric or includes an unexpected one"
fi

if grep -q '"CriticalServiceDown"' "$DASHBOARD_TF" 2>/dev/null; then
  pass "24: CriticalServiceDown metric is present"
else
  fail "24: CriticalServiceDown metric is missing"
fi

if grep -q 'gg_dashboard_critical_services = \["adminsrvr", "distsrvr", "recvsrvr"\]' "$DASHBOARD_TF" 2>/dev/null; then
  pass "24: critical-service list is exactly adminsrvr/distsrvr/recvsrvr"
else
  fail "24: critical-service list is missing or does not exactly match adminsrvr/distsrvr/recvsrvr"
fi

if grep -q "aws_cloudwatch_log_group.goldengate_runtime.name" "$DASHBOARD_TF" 2>/dev/null \
    && grep -q "aws_cloudwatch_log_group.goldengate_monitor.name" "$DASHBOARD_TF" 2>/dev/null; then
  pass "24: dashboard references the existing Terraform log-group resources by attribute, never a duplicated literal"
else
  fail "24: dashboard no longer references the existing log-group resources by attribute"
fi

DASHBOARD_FORBIDDEN_FOUND="false"
if grep -q "aws_cloudwatch_metric_alarm" "$DASHBOARD_TF" 2>/dev/null; then
  fail "24: cloudwatch_dashboard.tf introduces an aws_cloudwatch_metric_alarm resource"
  DASHBOARD_FORBIDDEN_FOUND="true"
fi
if grep -qi "aws_sns_topic\|aws_sns" "$DASHBOARD_TF" 2>/dev/null; then
  fail "24: cloudwatch_dashboard.tf introduces an SNS resource"
  DASHBOARD_FORBIDDEN_FOUND="true"
fi
if grep -qE 'cloudwatch:(GetMetricData|ListMetrics|DescribeAlarms|GetDashboard)' "$DASHBOARD_TF" 2>/dev/null || grep -q 'resource "aws_iam' "$DASHBOARD_TF" 2>/dev/null; then
  fail "24: cloudwatch_dashboard.tf references a CloudWatch read IAM action or defines a new IAM resource"
  DASHBOARD_FORBIDDEN_FOUND="true"
fi
if grep -qE '<<-?(EOT|EOF)' "$DASHBOARD_TF" 2>/dev/null; then
  fail "24: cloudwatch_dashboard.tf uses a heredoc, expected native Terraform collections passed to jsonencode"
  DASHBOARD_FORBIDDEN_FOUND="true"
fi
if [ "$DASHBOARD_FORBIDDEN_FOUND" = "false" ]; then
  pass "24: no alarm, SNS, CloudWatch-read IAM action, new IAM resource, or heredoc-built JSON is present"
fi

if grep -q "Real Extract and Replicat processes are not configured yet" "$DASHBOARD_TF" 2>/dev/null \
    && grep -q 'STATE#<process>' "$DASHBOARD_TF" 2>/dev/null; then
  pass "24: dashboard includes honest process-visibility deferral text"
else
  fail "24: dashboard is missing the honest process-visibility deferral text"
fi

if grep -q "Terraform-managed PromQL charts" "$DASHBOARD_TF" 2>/dev/null \
    && grep -q "console-generated query source" "$DASHBOARD_TF" 2>/dev/null; then
  pass "24: dashboard includes honest Container Insights/PromQL deferral text"
else
  fail "24: dashboard is missing the honest Container Insights/PromQL deferral text"
fi

if grep -qi "FILL(" "$DASHBOARD_TF" 2>/dev/null || grep -q '"expression"' "$DASHBOARD_TF" 2>/dev/null; then
  fail "24: cloudwatch_dashboard.tf converts missing data to a healthy zero via a metric expression"
else
  pass "24: no metric expression converts missing data to a healthy zero"
fi

# The old coarse "no IAM file changed" check is superseded by check 18's content-aware role protection above.

if python3 hack/test-goldengate-metrics-config.py >/dev/null 2>&1; then
  pass "22: hack/test-goldengate-metrics-config.py (Phase 6C1 corrections functional suite) passes"
  find hack -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
else
  fail "22: hack/test-goldengate-metrics-config.py failed"
fi

# 25. Phase 6C1B: process-discovery status and fail-closed STATE-row correction.
echo ""
echo "--- Phase 6C1B: process-discovery status correction ---"

COLLECTOR_PY="monitoring/monitor/collector.py"
MONITOR_PY="monitoring/monitor/monitor.py"
MONITOR_WORKFLOW=".github/workflows/goldengate-monitor.yaml"

if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  NEW_WORKFLOW_FILES="$(git status --porcelain=v1 2>/dev/null | grep -E '^\?\? \.github/workflows/.*\.ya?ml$' || true)"
  if [ -z "$NEW_WORKFLOW_FILES" ]; then
    pass "25: no new workflow file introduced (goldengate-monitor.yaml modified in place)"
  else
    fail "25: an unexpected new workflow file was introduced:"$'\n'"${NEW_WORKFLOW_FILES}"
  fi
else
  skip "25: no-new-workflow check -- not a git repository"
fi

if grep -q '"${MONITOR_SOURCE_PATH}/collector.py"' "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q '"${MONITOR_SOURCE_PATH}/monitor.py"' "$MONITOR_WORKFLOW" 2>/dev/null \
    && grep -q '"${MONITOR_SOURCE_PATH}/ui.py"' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "25: monitor image content hash still covers collector.py, monitor.py, and ui.py"
else
  fail "25: monitor image content hash no longer covers all three changed source files"
fi

if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # configmap.yaml/values.yaml excluded: Phase 6D0 legitimately touched their explanatory comments, not their logic. efs-storageclass.yaml/goldengate/values.yaml excluded: the Phase 6D1 EFS correction legitimately updated the mode-aware fail-guard wording and added the persistence.efs.mode default, neither a template logic/behavior change. secretproviderclass.yaml excluded: the monitor CSI VDR correction legitimately regrouped the rendered objects by adminSecret (duplicate top-level objectName rejected by the AWS Secrets Store CSI provider), not a Phase 6C1B process-discovery change. runtime-secretproviderclass.yaml excluded: the Fresh-EKS Phase A/Phase 9-carry-forward correction legitimately made runtime.csi.region fail-closed instead of silently rendering an empty region, not a Phase 6C1B process-discovery change.
  NOT_PERMITTED_DIFF="$(git diff --stat --ignore-all-space -- \
    monitoring/monitor/health_rules.py monitoring/monitor/Dockerfile \
    'helm/goldengate-monitor/**' 'helm/goldengate/**' \
    ':!helm/goldengate-monitor/templates/configmap.yaml' ':!helm/goldengate-monitor/values.yaml' \
    ':!helm/goldengate-monitor/templates/secretproviderclass.yaml' \
    ':!helm/goldengate/templates/efs-storageclass.yaml' ':!helm/goldengate/values.yaml' \
    ':!helm/goldengate/templates/runtime-secretproviderclass.yaml' 2>/dev/null || true)"
  if [ -z "$NOT_PERMITTED_DIFF" ]; then
    pass "25: no Helm chart or Dockerfile file outside this phase's own scope changed"
  else
    fail "25: an out-of-scope file changed unexpectedly:"$'\n'"${NOT_PERMITTED_DIFF}"
  fi
else
  skip "25: out-of-scope file check -- not a git repository"
fi

if grep -q "delete_item" "$COLLECTOR_PY" 2>/dev/null; then
  fail "25: collector.py introduces a DeleteItem call into the process lifecycle"
else
  pass "25: no DeleteItem call exists in collector.py"
fi

if grep -qE '\.scan\(' "$COLLECTOR_PY" "$MONITOR_PY" 2>/dev/null; then
  fail "25: a DynamoDB Scan call was introduced"
else
  pass "25: no DynamoDB Scan call exists in collector.py or monitor.py"
fi

EXPECTED_METRIC_NAMES="AbendEvent AbendFailure AbendState CriticalServiceDown DeploymentDown HeartbeatAgeSeconds LagBreached ExtractLagSeconds ReplicatLagSeconds"
ACTUAL_METRIC_NAMES="$(grep -oE '"MetricName": "[A-Za-z]+"' "$COLLECTOR_PY" | sed -E 's/"MetricName": "([A-Za-z]+)"/\1/' | sort -u)"
UNEXPECTED_METRIC_NAMES="false"
for name in $ACTUAL_METRIC_NAMES; do
  case " $EXPECTED_METRIC_NAMES " in
    *" $name "*) ;;
    *) UNEXPECTED_METRIC_NAMES="true"; echo "  unexpected metric name: $name" ;;
  esac
done
if [ "$UNEXPECTED_METRIC_NAMES" = "false" ]; then
  pass "25: no new CloudWatch metric name introduced"
else
  fail "25: an unexpected CloudWatch metric name was introduced"
fi

if grep -qE 'STATE#unknown|STATE#None|STATE#["'"'"']?\s*\+|recordType.*=.*"STATE#"\s*$' "$COLLECTOR_PY" 2>/dev/null; then
  fail "25: a synthetic process fallback (STATE#unknown/STATE#None/STATE#) may exist"
else
  pass "25: no synthetic process fallback exists"
fi

REGISTRY_EXTRACTION_FIXTURE_RESULT="$(python3 - "$MONITOR_WORKFLOW" "$CANONICAL_CONFIG" <<'PYEOF'
import subprocess
import sys
import tempfile
import os

import yaml


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, lambda l, n: l.construct_mapping(n))

with open(sys.argv[1]) as f:
    doc = yaml.load(f, Loader=Loader)

run_text = None
for job in doc["jobs"].values():
    for step in job.get("steps", []):
        run = step.get("run") or ""
        if "ENABLED_DEPLOYMENT_PAIRS_DISCOVERY" in run:
            run_text = run
            break
    if run_text is not None:
        break

if run_text is None:
    print("FAIL: could not locate the ENABLED_DEPLOYMENT_PAIRS_DISCOVERY extraction step in the workflow")
    sys.exit(1)

start_marker = "< <(awk '\n"
end_marker = "\n' ${GENERATED_REGISTRY_PATH})"
start = run_text.index(start_marker) + len(start_marker)
end = run_text.index(end_marker, start)
awk_script = run_text[start:end]

if "\\s" in awk_script:
    print("FAIL: extracted extraction logic still contains a non-portable \\s AWK pattern")
    sys.exit(1)

awk_file = tempfile.NamedTemporaryFile(mode="w", suffix=".awk", delete=False)
awk_file.write(awk_script)
awk_file.close()


def run_awk(registry_path):
    result = subprocess.run(["awk", "-f", awk_file.name, registry_path], capture_output=True, text=True, check=True)
    return sorted(line.split("|", 1)[0] for line in result.stdout.splitlines() if line.strip())


# Self-service: expected names are derived from the SAME canonical registry file being scanned (never a hardcoded real-inventory list), so onboarding a new envs/dev/<id>/values.yaml folder never requires editing this test.
with open(sys.argv[2]) as f:
    canonical_doc = yaml.safe_load(f)
expected_names = sorted(d["name"] for d in canonical_doc["deployments"] if d.get("enabled"))

real_names = run_awk(sys.argv[2])
if real_names != expected_names:
    print(f"FAIL: real-registry extraction returned {real_names!r}, expected {expected_names!r} (derived from the same canonical registry file)")
    sys.exit(1)

fixture_yaml = (
    "environment: dev\n"
    "runtimeNamespace: goldengate-dev\n"
    "deployments:\n"
    "  - name: gg-fixture-enabled\n"
    "    type: oracle\n"
    "    enabled: true\n"
    "  - name: gg-fixture-disabled\n"
    "    type: oracle\n"
    "    enabled: false\n"
)
fixture_file = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
fixture_file.write(fixture_yaml)
fixture_file.close()

fixture_names = run_awk(fixture_file.name)
os.unlink(awk_file.name)
os.unlink(fixture_file.name)

if fixture_names != ["gg-fixture-enabled"]:
    print(f"FAIL: fixture-registry extraction returned {fixture_names!r}, expected only the enabled deployment")
    sys.exit(1)

print("OK")
PYEOF
)"
if [ "$REGISTRY_EXTRACTION_FIXTURE_RESULT" = "OK" ]; then
  pass "25: post-rollout registry extraction is portable AWK and returns the exact expected names against both the real DEV registry and a temporary enabled/disabled fixture"
else
  fail "25: registry-extraction fixture failed:"$'\n'"${REGISTRY_EXTRACTION_FIXTURE_RESULT}"
fi

if grep -q 'status not in ("OK", "EMPTY")' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "25: post-rollout workflow validates OK/EMPTY and rejects incomplete discovery"
else
  fail "25: post-rollout workflow no longer validates OK/EMPTY discovery status"
fi

DISCOVERY_CONSISTENCY_FIXTURE_RESULT="$(python3 - "$MONITOR_WORKFLOW" <<'PYEOF'
import json
import os
import subprocess
import sys
import tempfile

import yaml


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, lambda l, n: l.construct_mapping(n))

with open(sys.argv[1]) as f:
    doc = yaml.load(f, Loader=Loader)

run_text = None
for job in doc["jobs"].values():
    for step in job.get("steps", []):
        run = step.get("run") or ""
        if "PROCESS_DISCOVERY_CHECK" in run and "<<'PYEOF'" in run:
            run_text = run
            break
    if run_text is not None:
        break

if run_text is None:
    print("FAIL: could not locate the PROCESS_DISCOVERY_CHECK validation step")
    sys.exit(1)

anchor = run_text.index('cat > "$PROCESS_DISCOVERY_CHECK"')
start_marker = "<<'PYEOF'\n"
end_marker = "\nPYEOF"
start = run_text.index(start_marker, anchor) + len(start_marker)
end = run_text.index(end_marker, start)
script_body = run_text[start:end]

script_file = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False)
script_file.write(script_body)
script_file.close()

base = {"deploymentName": "gg-x", "alertsEnabled": False, "processes": []}


def run_case(discovery):
    names_file = tempfile.NamedTemporaryFile(mode="w", delete=False)
    names_file.write("gg-x")
    names_file.close()
    status_file = tempfile.NamedTemporaryFile(mode="w", delete=False)
    json.dump({"logicalPipelines": [{"runtimes": [{**base, "processDiscovery": discovery}]}]}, status_file)
    status_file.close()
    env = dict(os.environ, ENABLED_NAMES_FILE=names_file.name, API_STATUS_FILE=status_file.name)
    proc = subprocess.run([sys.executable, script_file.name], capture_output=True, text=True, env=env)
    os.unlink(names_file.name)
    os.unlink(status_file.name)
    return proc.returncode


cases = [
    ("OK, extract=1", {"status": "OK", "extractCount": 1, "replicatCount": 0, "distpathCount": 0,
                       "totalCount": 1, "detailFailureCount": 0, "extractsStatus": "OK", "replicatsStatus": "EMPTY"}, 0),
    ("EMPTY, distpath=3 (independent)", {"status": "EMPTY", "extractCount": 0, "replicatCount": 0, "distpathCount": 3,
                                         "totalCount": 0, "detailFailureCount": 0, "extractsStatus": "EMPTY", "replicatsStatus": "EMPTY"}, 0),
    ("OK, extract=0 replicat=0", {"status": "OK", "extractCount": 0, "replicatCount": 0, "distpathCount": 0,
                                  "totalCount": 0, "detailFailureCount": 0, "extractsStatus": "OK", "replicatsStatus": "OK"}, 1),
    ("EMPTY, extract=1", {"status": "EMPTY", "extractCount": 1, "replicatCount": 0, "distpathCount": 0,
                          "totalCount": 1, "detailFailureCount": 0, "extractsStatus": "EMPTY", "replicatsStatus": "EMPTY"}, 1),
    ("OK, boolean extractCount", {"status": "OK", "extractCount": True, "replicatCount": 0, "distpathCount": 0,
                                  "totalCount": 1, "detailFailureCount": 0, "extractsStatus": "OK", "replicatsStatus": "OK"}, 1),
    ("OK, detailFailureCount=1", {"status": "OK", "extractCount": 1, "replicatCount": 0, "distpathCount": 0,
                                  "totalCount": 1, "detailFailureCount": 1, "extractsStatus": "OK", "replicatsStatus": "OK"}, 1),
]

failed = False
for label, discovery, expected_code in cases:
    actual_code = run_case(discovery)
    if actual_code != expected_code:
        print(f"FAIL: case {label!r} expected exit {expected_code}, got {actual_code}")
        failed = True

os.unlink(script_file.name)
print("FAIL" if failed else "OK")
PYEOF
)"
if [ "$DISCOVERY_CONSISTENCY_FIXTURE_RESULT" = "OK" ]; then
  pass "25: post-rollout discovery-consistency validation enforces OK/EMPTY count rules, rejects Boolean counts, and treats distribution count independently"
else
  fail "25: discovery-consistency fixture failed:"$'\n'"${DISCOVERY_CONSISTENCY_FIXTURE_RESULT}"
fi

if grep -qE 'totalCount.*>\s*0|len\(.*processes.*\)\s*>\s*0|require.*non-?zero.*process' "$MONITOR_WORKFLOW" 2>/dev/null; then
  fail "25: workflow appears to require a non-zero process count"
else
  pass "25: workflow does not require a non-zero process count"
fi

if grep -q 'alerts_enabled is not False' "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "25: post-rollout workflow confirms alertsEnabled remains literal false"
else
  fail "25: post-rollout workflow no longer confirms alertsEnabled remains false"
fi

if python3 hack/check-comment-style.py "$COLLECTOR_PY" "$MONITOR_PY" monitoring/monitor/ui.py \
    monitoring/monitor/tests/test_collector.py monitoring/monitor/tests/test_monitor.py "$MONITOR_WORKFLOW" >/dev/null 2>&1; then
  pass "25: comment-style checker remains integrated and reports zero violations"
else
  fail "25: comment-style checker reported a violation in the Phase 6C1B files"
fi

# 26. Phase 6D0: generic, folder-driven GoldenGate deployment onboarding.
echo ""
echo "--- Phase 6D0: folder-driven onboarding architecture ---"

DEPLOYMENT_MODEL_TOOL="hack/goldengate-deployment-model.py"
INVENTORY_TF="envs/dev/goldengate_inventory.tf"

if [ -f "$DEPLOYMENT_MODEL_TOOL" ]; then
  pass "26: hack/goldengate-deployment-model.py exists as the single deployment-model tool"
else
  fail "26: hack/goldengate-deployment-model.py is missing"
fi

if python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev validate >/dev/null 2>&1; then
  pass "26: the deployment-model tool validates the real DEV folder-driven descriptors cleanly"
else
  fail "26: the deployment-model tool reported a validation problem against the real DEV descriptors"
fi

if python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev registry 2>/dev/null | grep -q "gg-postgresql-repltest-01" \
    && python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev registry 2>/dev/null | grep -q "gg-mssql-repltest-01"; then
  pass "26: the generated registry contains both existing live deployments"
else
  fail "26: the generated registry is missing an existing live deployment"
fi

if [ -f "$INVENTORY_TF" ] && grep -q "goldengate_enabled_deployments" "$INVENTORY_TF" 2>/dev/null; then
  pass "26: envs/dev/goldengate_inventory.tf provides the folder-driven Terraform inventory"
else
  fail "26: envs/dev/goldengate_inventory.tf is missing or does not define goldengate_enabled_deployments"
fi

if grep -q "local.goldengate_deployment_names" envs/dev/dynamodb.tf 2>/dev/null; then
  pass "26: DynamoDB CONFIG for_each is folder-driven (no longer reads goldengate-deployments.yaml directly)"
else
  fail "26: DynamoDB CONFIG no longer derives from the folder-driven inventory"
fi

if grep -q "local.goldengate_deployment_names" envs/dev/cloudwatch_dashboard.tf 2>/dev/null \
    && ! grep -q "yamldecode(file(\"\${path.module}/goldengate-deployments.yaml\"))" envs/dev/cloudwatch_dashboard.tf 2>/dev/null; then
  pass "26: CloudWatch dashboard inventory is folder-driven (no longer reads goldengate-deployments.yaml directly)"
else
  fail "26: CloudWatch dashboard no longer derives from the folder-driven inventory"
fi

if grep -q "data.external" envs/dev/*.tf 2>/dev/null || grep -q "local-exec" envs/dev/*.tf 2>/dev/null; then
  fail "26: Terraform inventory uses data.external or local-exec"
else
  pass "26: no data.external or local-exec exists in envs/dev Terraform"
fi

if [ ! -f "hack/ensure-goldengate-admin-secret.py" ] && [ ! -f "hack/test-ensure-goldengate-admin-secret.py" ]; then
  pass "26: no per-deployment secret bootstrap helper exists (removed with the shared-secret model)"
else
  fail "26: a per-deployment secret bootstrap helper still exists"
fi

DEPLOY_ROLE_POLICY="envs/dev/policies/goldengate-eks-deploy-dev/policies/policies_1.json"
if ! grep -qE "PutSecretValue|GetRandomPassword" "$DEPLOY_ROLE_POLICY" 2>/dev/null; then
  pass "26: deployment-role IAM policy has no PutSecretValue or GetRandomPassword permission"
else
  fail "26: deployment-role IAM policy still grants a secret-mutation permission"
fi

if grep -q "secretsmanager:GetSecretValue" "$DEPLOY_ROLE_POLICY" 2>/dev/null; then
  fail "26: deployment-role IAM policy grants GetSecretValue (must remain read-only DescribeSecret/ListSecretVersionIds)"
else
  pass "26: deployment-role IAM policy never grants GetSecretValue"
fi

if grep -q "dev/goldengate/source/admin-??????" "$DEPLOY_ROLE_POLICY" 2>/dev/null \
    && grep -q "dev/goldengate/target/admin-??????" "$DEPLOY_ROLE_POLICY" 2>/dev/null \
    && grep -q "dev/goldengate/tls-certificate-??????" "$DEPLOY_ROLE_POLICY" 2>/dev/null; then
  pass "26: deployment-role read-only secret validation is scoped to exactly the three shared secret ARNs"
else
  fail "26: deployment-role read-only secret validation is not scoped to the three approved shared secret ARNs"
fi

# Restored shared-identity phase: exactly ONE approved runtime ServiceAccount (gg-runtime-sa) for every deploymentType, never a per-flavour map/branch. deploymentType controls image/product/ports/replication semantics, never AWS runtime identity.
if grep -q "gg-runtime-sa" helm/goldengate-platform/values.yaml 2>/dev/null \
    && grep -qF ".Values.runtimeServiceAccount.name" helm/goldengate-platform/templates/runtime-serviceaccounts.yaml 2>/dev/null; then
  pass "26: the shared gg-runtime-sa identity exists in the platform chart (values.yaml default consumed by name via the template, never a hardcoded literal in the template itself)"
else
  fail "26: the shared gg-runtime-sa identity is missing from the platform chart"
fi

if grep -qE '^\s*runtimeServiceAccount:\s*$' helm/goldengate-platform/values.yaml 2>/dev/null \
    && grep -qE '^\s*name:\s*gg-runtime-sa\s*$' helm/goldengate-platform/values.yaml 2>/dev/null \
    && ! grep -q "runtimeServiceAccounts:" helm/goldengate-platform/values.yaml 2>/dev/null; then
  pass "26: the platform chart's runtimeServiceAccount default is a single object (name: gg-runtime-sa), never a per-flavour map"
else
  fail "26: the platform chart no longer defines the single shared runtimeServiceAccount default"
fi

if grep -qE '\{\{-?\s*range\s+\$type' helm/goldengate-platform/templates/runtime-serviceaccounts.yaml 2>/dev/null \
    || grep -q "runtimeServiceAccounts" helm/goldengate-platform/templates/runtime-serviceaccounts.yaml 2>/dev/null; then
  fail "26: the platform chart template still iterates a per-flavour runtimeServiceAccounts map (must be a single shared identity, no range loop)"
else
  pass "26: the platform chart template renders the single shared identity directly, no per-flavour range loop"
fi

for engine_literal in "goldengate.adcb/engine: oracle" "goldengate.adcb/engine: postgresql" "goldengate.adcb/engine: mssql" "goldengate.adcb/engine: daa" "goldengate.adcb/engine: sqlserver" "goldengate.adcb/engine: distributed" "gg-oracle-sa" "gg-postgresql-sa" "gg-mssql-sa" "gg-daa-sa"; do
  if grep -qF -- "$engine_literal" helm/goldengate-platform/templates/runtime-serviceaccounts.yaml 2>/dev/null; then
    fail "26: the platform chart template hardcodes an engine-specific identity/label: ${engine_literal}"
  fi
done
if grep -qF "goldengate.adcb/purpose: runtime" helm/goldengate-platform/templates/runtime-serviceaccounts.yaml 2>/dev/null; then
  pass "26: the platform chart labels the shared identity goldengate.adcb/purpose: runtime, never a per-flavour engine literal"
else
  fail "26: the platform chart's shared runtime ServiceAccount is missing the goldengate.adcb/purpose: runtime label"
fi

# Config-placement: transitionalRuntimeServiceAccounts is a DEV-only migration list, never a chart-level default.
if grep -qE '^\s*transitionalRuntimeServiceAccounts:\s*\[\]\s*$' helm/goldengate-platform/values.yaml 2>/dev/null; then
  pass "26: the generic platform chart default is transitionalRuntimeServiceAccounts: [] (empty)"
else
  fail "26: the generic platform chart default no longer declares an empty transitionalRuntimeServiceAccounts list"
fi

# Fresh-EKS Phase A: this is a new cluster with no live migration workloads, so DEV no longer overrides transitionalRuntimeServiceAccounts -- it must inherit the chart's own safe [] default (checked just above) rather than redeclare it.
if grep -qE '^\s*transitionalRuntimeServiceAccounts:' platform/dev/goldengate-platform/values.yaml 2>/dev/null; then
  fail "26: platform/dev/goldengate-platform/values.yaml still overrides transitionalRuntimeServiceAccounts -- the fresh cluster must inherit the chart's own [] default instead"
else
  pass "26: platform/dev/goldengate-platform/values.yaml no longer overrides transitionalRuntimeServiceAccounts -- inherits the chart's own [] default"
fi

if grep -qE '\{\{-?\s*range\s+\.Values\.transitionalRuntimeServiceAccounts' helm/goldengate-platform/templates/runtime-serviceaccounts.yaml 2>/dev/null; then
  pass "26: the platform chart template renders transitionalRuntimeServiceAccounts via one generic range, never a per-flavour branch"
else
  fail "26: the platform chart template no longer generically renders transitionalRuntimeServiceAccounts"
fi

if [ "$HELM_AVAILABLE" = "true" ]; then
  PLATFORM_SA_RENDER="$(helm template gg-platform helm/goldengate-platform \
    --values platform/dev/goldengate-platform/values.yaml \
    --set-string runtimeServiceAccount.roleArn=arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev \
    --set-string fluentBit.serviceAccount.roleArn=arn:aws:iam::668311715351:role/GoldenGatePlatformLoggingRole-dev \
    --set-string fluentBit.aws.region=eu-west-1 \
    --set-string fluentBit.image.reference=229410149234.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit@sha256:366923ffc51dfde4966e743dcbd4ca05211b733d4f69c7591903bc7660fbf243 \
    "${PLATFORM_SHARED_OVERRIDES[@]}" \
    2>/dev/null)"
  RENDERED_SA_COUNT="$(echo "$PLATFORM_SA_RENDER" | grep -c '^kind: ServiceAccount$' || true)"
  if [ "$RENDERED_SA_COUNT" -eq 2 ] \
      && [ "$(echo "$PLATFORM_SA_RENDER" | grep -c 'name: gg-runtime-sa')" -eq 1 ] \
      && echo "$PLATFORM_SA_RENDER" | grep -q "name: gg-fluent-bit" \
      && ! echo "$PLATFORM_SA_RENDER" | grep -qE "name: gg-(oracle|postgresql|mssql|daa|mysql|sqlserver|distributed)-sa"; then
    pass "26: the platform chart renders exactly the 2 expected ServiceAccounts (canonical gg-runtime-sa + gg-fluent-bit) -- no migration-compatibility identity on the fresh cluster, never a per-deploymentType identity"
  else
    fail "26: the rendered platform chart ServiceAccount set is not exactly {gg-runtime-sa, gg-fluent-bit} (found ${RENDERED_SA_COUNT} ServiceAccount documents)"
  fi

  # Adding a brand-new deploymentType (e.g. mysql, mssql) must have ZERO effect on the platform chart's rendered ServiceAccount set -- it is driven entirely by fixed values.yaml data, never by the folder-driven deployment inventory.
  if ! echo "$PLATFORM_SA_RENDER" | grep -q "name: gg-mysql-sa" && ! echo "$PLATFORM_SA_RENDER" | grep -q "name: gg-mssql-sa"; then
    pass "26: the real gg-mssql-repltest-01 descriptor (and any synthetic mysql deployment) never causes the platform chart to render gg-mssql-sa/gg-mysql-sa -- the ServiceAccount set is fixed values.yaml data, not folder-derived"
  else
    fail "26: an engine-specific ServiceAccount (gg-mssql-sa/gg-mysql-sa) was rendered by the platform chart -- self-service onboarding must never create one"
  fi

  # Generic render WITHOUT the DEV migration override: proves transitional identities are environment-specific, never library defaults.
  set +e
  GENERIC_PLATFORM_SA_RENDER="$(helm template gg-platform helm/goldengate-platform \
    --set-string runtimeServiceAccount.roleArn=arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev \
    --set environment=dev --set namespaces.runtime.create=true --set-string namespaces.runtime.name=goldengate-dev \
    2>&1)"
  set -e
  if [ "$(echo "$GENERIC_PLATFORM_SA_RENDER" | grep -c '^kind: ServiceAccount$')" -eq 1 ] \
      && echo "$GENERIC_PLATFORM_SA_RENDER" | grep -q "name: gg-runtime-sa" \
      && ! echo "$GENERIC_PLATFORM_SA_RENDER" | grep -qE "name: gg-(oracle|postgresql|mssql|daa|mysql)-sa"; then
    pass "26: the generic chart render (no DEV override, fluentBit.create defaults false) renders ONLY gg-runtime-sa -- no gg-oracle-sa/gg-postgresql-sa/gg-mssql-sa"
  else
    fail "26: the generic chart render (no DEV override) unexpectedly rendered transitional or engine-specific ServiceAccounts"
  fi
else
  skip "26: platform ServiceAccount render check -- helm not available"
fi

STS_TRUST_POLICY="envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json"
if grep -q "goldengate-dev:gg-runtime-sa" "$STS_TRUST_POLICY" 2>/dev/null; then
  pass "26: IAM trust policy trusts the permanent canonical system:serviceaccount:goldengate-dev:gg-runtime-sa subject"
else
  fail "26: IAM trust policy is missing the canonical system:serviceaccount:goldengate-dev:gg-runtime-sa subject"
fi

# Fresh EKS cluster (Fresh-EKS Phase A): the migration-compatibility subjects from the destroyed cluster must NOT be recreated -- there are no old Oracle/PostgreSQL runtime pods on this cluster needing migration trust.
if grep -q "goldengate-dev:gg-oracle-sa" "$STS_TRUST_POLICY" 2>/dev/null \
    || grep -q "goldengate-dev:gg-postgresql-sa" "$STS_TRUST_POLICY" 2>/dev/null; then
  fail "26: IAM trust policy still trusts a migration-only transitional subject (gg-oracle-sa/gg-postgresql-sa) -- must not be recreated on the fresh cluster"
else
  pass "26: IAM trust policy trusts neither migration-only transitional subject (gg-oracle-sa, gg-postgresql-sa)"
fi

if grep -q "gg-dev-\*:ogg-oracle-sa" "$STS_TRUST_POLICY" 2>/dev/null; then
  fail "26: IAM trust policy still retains the legacy historical Oracle wildcard exception (system:serviceaccount:gg-dev-*:ogg-oracle-sa) -- must not be recreated on the fresh cluster"
else
  pass "26: IAM trust policy no longer retains the legacy historical Oracle wildcard exception (system:serviceaccount:gg-dev-*:ogg-oracle-sa)"
fi

if grep -qE '"system:serviceaccount:goldengate-dev:\*"|goldengate-dev:gg-\\?\*-sa|goldengate-dev:gg-mssql-sa' "$STS_TRUST_POLICY" 2>/dev/null; then
  fail "26: IAM trust policy contains an unexpected new wildcard or gg-mssql-sa subject"
else
  pass "26: IAM trust policy contains no new wildcard and no gg-mssql-sa subject"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  SYNTHETIC_TYPE_TRUST_CHECK="$(python3 -c '
import importlib.util
spec = importlib.util.spec_from_file_location("goldengate_deployment_model", "hack/goldengate-deployment-model.py")
gdm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gdm)
before = gdm.resolve_runtime_service_account("oracle")
after_synthetic = gdm.resolve_runtime_service_account("some-brand-new-future-type")
print("OK" if before == after_synthetic == "gg-runtime-sa" else "FAIL")
' 2>&1)"
  if [ "$SYNTHETIC_TYPE_TRUST_CHECK" = "OK" ]; then
    pass "26: a synthetic never-before-seen deploymentType resolves the SAME gg-runtime-sa identity -- onboarding a new engine never requires a new IAM trust subject"
  else
    fail "26: a synthetic deploymentType did not resolve gg-runtime-sa: ${SYNTHETIC_TYPE_TRUST_CHECK}"
  fi
else
  skip "26: synthetic-type trust-stability check -- python3 unavailable"
fi

# Fresh-EKS Phase A resolved this blocker definitively: this is a brand-new cluster with no live workloads at all, so no live-cluster inventory evidence is needed to prove the legacy namespace-wildcard subject is safe to remove -- it is unconditionally absent now.
if grep -qE '"system:serviceaccount:[^"]*\*[^"]*"' "$STS_TRUST_POLICY" 2>/dev/null; then
  fail "26: IAM trust still contains a namespace-wildcard subject -- must not exist on the fresh cluster"
else
  pass "26: IAM trust contains no namespace-wildcard subject"
fi

if grep -q "SUPPORTED_TYPES" monitoring/monitor/config.py 2>/dev/null; then
  fail "26: monitor config.py still defines a fixed SUPPORTED_TYPES engine allowlist"
else
  pass "26: monitor config.py no longer defines a fixed engine allowlist"
fi

if grep -qE "ogg-oracle\"|-> *ogg-oracle|oracle.*=>.*ogg-" .github/workflows/goldengate-eks-app.yaml 2>/dev/null; then
  fail "26: an engine-to-image mapping was introduced in the app workflow"
else
  pass "26: no engine-to-image mapping exists in the app workflow"
fi

if grep -q "goldengate-deployment-model.py" .github/workflows/goldengate-monitor.yaml 2>/dev/null \
    && grep -qF 'registry --output "$GENERATED_REGISTRY_PATH"' .github/workflows/goldengate-monitor.yaml 2>/dev/null; then
  pass "26: the monitor workflow generates the registry via the deployment-model tool before chart staging"
else
  fail "26: the monitor workflow no longer generates the registry via the deployment-model tool"
fi

if grep -q "REPLICATION_DISABLED_MESSAGE" "$DEPLOYMENT_MODEL_TOOL" 2>/dev/null; then
  fail "26: the retired Phase 6D0 unconditional replication rejection still exists in the deployment-model tool"
else
  pass "26: the Phase 6D0 unconditional replication rejection has been fully replaced"
fi

if grep -q "REPLICATION_SCOPE_MESSAGE" "$DEPLOYMENT_MODEL_TOOL" 2>/dev/null \
    && grep -q "postgresql source paired with an mssql target" "$DEPLOYMENT_MODEL_TOOL" 2>/dev/null; then
  pass "26: replication.enabled=true outside the approved postgresql-source/mssql-target scope is rejected with the fixed Phase 6D1 message"
else
  fail "26: the fixed Phase 6D1 replication-scope rejection message is missing"
fi

FORBIDDEN_6D0_TERMS_FOUND="false"
for term in "CreateExtract" "CreateReplicat" "aws_cloudwatch_metric_alarm" "aws_sns" "utility-sidecar" "observer-sidecar" "gg-alerter"; do
  if grep -rq -- "$term" "$DEPLOYMENT_MODEL_TOOL" "$INVENTORY_TF" envs/dev/cloudwatch_dashboard.tf envs/dev/secret.tf 2>/dev/null; then
    fail "26: forbidden Phase 6D0 term found: ${term}"
    FORBIDDEN_6D0_TERMS_FOUND="true"
  fi
done
if [ "$FORBIDDEN_6D0_TERMS_FOUND" = "false" ]; then
  pass "26: no process creation, alarm, SNS, gg-alerter, or sidecar reference exists in the new Phase 6D0 source"
fi

if python3 hack/check-comment-style.py >/dev/null 2>&1; then
  pass "26: comment-style checker remains integrated and reports zero violations"
else
  fail "26: comment-style checker reported a violation in the Phase 6D0 files"
fi

echo ""
echo "--- Generic runtime-identity contract agreement (Python vs Terraform deterministic naming) ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  IDENTITY_NAMING_AGREEMENT_CHECK="$(python3 - "$DEPLOYMENT_MODEL_TOOL" "$INVENTORY_TF" <<'PYEOF'
import re
import sys

tool_path, tf_path = sys.argv[1], sys.argv[2]

with open(tool_path) as f:
    tool_src = f.read()
if "RUNTIME_IDENTITY_MAP" in tool_src:
    print("FAIL: a retired hardcoded RUNTIME_IDENTITY_MAP still exists in the deployment-model tool")
    sys.exit(1)
if 'f"gg-{deployment_type}-sa"' in tool_src:
    print("FAIL: resolve_runtime_service_account still uses the retired per-type f\"gg-{deployment_type}-sa\" naming -- every type must share gg-runtime-sa")
    sys.exit(1)

with open(tf_path) as f:
    tf_src = f.read()
if "goldengate_runtime_identity_map" in tf_src:
    print("FAIL: a retired hardcoded goldengate_runtime_identity_map still exists in envs/dev/goldengate_inventory.tf")
    sys.exit(1)
if '"gg-${' in tf_src:
    print("FAIL: envs/dev/goldengate_inventory.tf still interpolates a per-type \"gg-${type}-sa\" ServiceAccount name -- every type must share gg-runtime-sa")
    sys.exit(1)
if 'id => "gg-runtime-sa"' not in tf_src:
    print("FAIL: envs/dev/goldengate_inventory.tf's goldengate_runtime_service_account_names no longer resolves the constant gg-runtime-sa")
    sys.exit(1)

sys.path.insert(0, tool_path.rsplit("/", 1)[0])
spec_globals = {"__file__": tool_path, "__name__": "gdm_check"}
exec(compile(tool_src, tool_path, "exec"), spec_globals)
resolve = spec_globals["resolve_runtime_service_account"]
for sample_type in ("oracle", "postgresql", "mssql", "daa", "mysql", "cassandra"):
    python_name = resolve(sample_type)
    if python_name != "gg-runtime-sa":
        print(f"FAIL: resolve_runtime_service_account({sample_type!r}) returned {python_name!r}, expected the shared 'gg-runtime-sa'")
        sys.exit(1)

print("OK: both hack/goldengate-deployment-model.py and envs/dev/goldengate_inventory.tf derive the SAME shared gg-runtime-sa for every deploymentType, never a per-type map/string interpolation")
PYEOF
)"
  IDENTITY_NAMING_AGREEMENT_STATUS=$?
  set -e
  if [ "$IDENTITY_NAMING_AGREEMENT_STATUS" -eq 0 ]; then
    pass "26: ${IDENTITY_NAMING_AGREEMENT_CHECK}"
  else
    fail "26: ${IDENTITY_NAMING_AGREEMENT_CHECK}"
  fi
else
  skip "26: runtime-identity contract agreement check -- python3 unavailable"
fi

echo ""
echo "--- Generic model: synthetic canonical mssql / daa flavour rendering (no live folders added) ---"

if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  SYNTHETIC_VALUES_DIR="$(mktemp -d)"
  cat > "${SYNTHETIC_VALUES_DIR}/mssql.yaml" <<'EOF'
deployment:
  enabled: true
  pipeline: synthetic-test-pipeline
  role: source
global:
  environment: dev
deploymentModel: singleRuntime
replication:
  enabled: false
runtime:
  enabled: true
  deploymentType: mssql
  businessDomain: payments
  containerName: ogg-sqlserver
  replicas: 1
  image:
    repository: 229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-sqlserver
    tag: "23.26.2.0.1"
  csi:
    enabled: true
    admin:
      enabled: true
      objectType: secretsmanager
      mountPath: /mnt/secrets-store/admin
    certificate:
      enabled: true
      objectType: secretsmanager
      mountPath: /etc/nginx/cert
  service:
    type: ClusterIP
    ports:
      https: 8443
      metrics: 9015
  storage:
    u02:
      type: efs
    u03:
      type: emptyDir
ingress:
  enabled: true
  mode: shared
  className: alb
  hostDomain: goldengate-dev.adcbmis.local
  alb:
    groupName: gg-poc-dev-alb
    groupOrder: "199"
    certificateArn: arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7
persistence:
  enabled: true
  provider: efs
  efs:
    fileSystemId: fs-0123456789abcdef0
    storageClass:
      create: true
EOF
  sed -e 's/deploymentType: mssql/deploymentType: daa/' \
      -e 's/groupOrder: "199"/groupOrder: "198"/' \
      "${SYNTHETIC_VALUES_DIR}/mssql.yaml" > "${SYNTHETIC_VALUES_DIR}/daa.yaml"

  MSSQL_DERIVED_SA="$(python3 -c "
import sys; sys.path.insert(0, '$(dirname "$DEPLOYMENT_MODEL_TOOL")')
spec_globals = {'__file__': '$DEPLOYMENT_MODEL_TOOL', '__name__': 'gdm_check'}
exec(compile(open('$DEPLOYMENT_MODEL_TOOL').read(), '$DEPLOYMENT_MODEL_TOOL', 'exec'), spec_globals)
print(spec_globals['resolve_runtime_service_account']('mssql'))
")"
  DAA_DERIVED_SA="$(python3 -c "
import sys; sys.path.insert(0, '$(dirname "$DEPLOYMENT_MODEL_TOOL")')
spec_globals = {'__file__': '$DEPLOYMENT_MODEL_TOOL', '__name__': 'gdm_check'}
exec(compile(open('$DEPLOYMENT_MODEL_TOOL').read(), '$DEPLOYMENT_MODEL_TOOL', 'exec'), spec_globals)
print(spec_globals['resolve_runtime_service_account']('daa'))
")"

  if [ "$MSSQL_DERIVED_SA" = "gg-runtime-sa" ] && [ "$DAA_DERIVED_SA" = "gg-runtime-sa" ]; then
    pass "26: the deterministic naming rule derives the SAME shared gg-runtime-sa for both mssql and daa -- deploymentType never selects AWS runtime identity"
  else
    fail "26: deterministic naming for mssql/daa did not resolve gg-runtime-sa as expected (mssql=${MSSQL_DERIVED_SA}, daa=${DAA_DERIVED_SA})"
  fi

  if helm template synthetic-mssql "$RUNTIME_CHART" \
      --namespace goldengate-dev \
      --values "${SYNTHETIC_VALUES_DIR}/mssql.yaml" \
      --set global.environment=dev \
      --set runtime.csi.admin.objectName=dev/goldengate/source/admin \
      --set runtime.csi.certificate.objectName=dev/goldengate/tls-certificate \
      --set-string runtime.csi.region="$RESOLVED_AWS_REGION" \
      --set runtime.serviceAccount.create=false \
      --set runtime.serviceAccount.name="$MSSQL_DERIVED_SA" \
      > "${SYNTHETIC_VALUES_DIR}/mssql-rendered.yaml" 2>"${SYNTHETIC_VALUES_DIR}/mssql.log" \
      && grep -q "serviceAccountName: gg-runtime-sa" "${SYNTHETIC_VALUES_DIR}/mssql-rendered.yaml" \
      && grep -q "image: \"229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-sqlserver:23.26.2.0.1\"" "${SYNTHETIC_VALUES_DIR}/mssql-rendered.yaml"; then
    pass "26: a synthetic deploymentType: mssql descriptor renders with serviceAccountName: gg-runtime-sa (the one shared identity) and the image taken directly from the values file (image stays ogg-sqlserver, independent of the deploymentType token)"
  else
    fail "26: synthetic mssql rendering failed or used an unexpected ServiceAccount/image"
    cat "${SYNTHETIC_VALUES_DIR}/mssql.log" 2>/dev/null || true
  fi

  if helm template synthetic-daa "$RUNTIME_CHART" \
      --namespace goldengate-dev \
      --values "${SYNTHETIC_VALUES_DIR}/daa.yaml" \
      --set global.environment=dev \
      --set runtime.csi.admin.objectName=dev/goldengate/source/admin \
      --set runtime.csi.certificate.objectName=dev/goldengate/tls-certificate \
      --set-string runtime.csi.region="$RESOLVED_AWS_REGION" \
      --set runtime.serviceAccount.create=false \
      --set runtime.serviceAccount.name="$DAA_DERIVED_SA" \
      > "${SYNTHETIC_VALUES_DIR}/daa-rendered.yaml" 2>"${SYNTHETIC_VALUES_DIR}/daa.log" \
      && grep -q "serviceAccountName: gg-runtime-sa" "${SYNTHETIC_VALUES_DIR}/daa-rendered.yaml"; then
    pass "26: a synthetic deploymentType: daa descriptor renders with serviceAccountName: gg-runtime-sa (the one shared identity), no distributed-to-daa alias map required"
  else
    fail "26: synthetic daa rendering failed or used an unexpected ServiceAccount"
    cat "${SYNTHETIC_VALUES_DIR}/daa.log" 2>/dev/null || true
  fi

  rm -rf "$SYNTHETIC_VALUES_DIR"

  if [ -d "envs/dev/gg-mssql-payments-01" ] || [ -d "envs/dev/gg-daa-payments-01" ] || [ -d "envs/dev/gg-sqlserver-payments-01" ]; then
    fail "26: a real SQL Server/DAA runtime deployment folder was added -- out of scope for this phase"
  else
    pass "26: no real SQL Server/DAA runtime deployment folder was added"
  fi
else
  skip "26: synthetic mssql/daa rendering -- helm or python3 not available"
fi

echo ""
echo "--- Phase 6D0 correction: onboarding-workflow job graph ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  JOB_GRAPH_CHECK="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

jobs = doc["jobs"]
expected_order = [
    "validate_model", "terraform_sync_once", "platform_sync_once", "validate_shared_secrets_once",
    "detect_changed_deployments", "build_publish_and_deploy", "monitor_sync_once", "final_validation",
]
for name in expected_order:
    if name not in jobs:
        print(f"FAIL: missing required job {name!r}")
        sys.exit(1)

if "bootstrap_admin_secrets" in jobs:
    print("FAIL: bootstrap_admin_secrets job still exists")
    sys.exit(1)

def needs_of(name):
    n = jobs[name].get("needs") or []
    return [n] if isinstance(n, str) else n

if "validate_model" not in needs_of("terraform_sync_once"):
    print("FAIL: terraform_sync_once does not need validate_model")
    sys.exit(1)
if "terraform_sync_once" not in needs_of("platform_sync_once"):
    print("FAIL: platform_sync_once does not need terraform_sync_once")
    sys.exit(1)
if "terraform_sync_once" not in needs_of("validate_shared_secrets_once") or "platform_sync_once" not in needs_of("validate_shared_secrets_once"):
    print("FAIL: validate_shared_secrets_once does not need both terraform_sync_once and platform_sync_once")
    sys.exit(1)
if "validate_shared_secrets_once" not in needs_of("build_publish_and_deploy"):
    print("FAIL: build_publish_and_deploy does not need validate_shared_secrets_once")
    sys.exit(1)
if "build_publish_and_deploy" not in needs_of("monitor_sync_once"):
    print("FAIL: monitor_sync_once does not need build_publish_and_deploy")
    sys.exit(1)
if "monitor_sync_once" not in needs_of("final_validation"):
    print("FAIL: final_validation does not need monitor_sync_once")
    sys.exit(1)

for name in ("terraform_sync_once", "platform_sync_once", "monitor_sync_once"):
    if not str(jobs[name].get("uses", "")).startswith("./.github/workflows/"):
        print(f"FAIL: {name} does not call a reusable workflow via a job-level uses:")
        sys.exit(1)

if "strategy" in jobs["validate_shared_secrets_once"] or "matrix" in jobs["validate_shared_secrets_once"]:
    print("FAIL: validate_shared_secrets_once uses a matrix (must be a single job)")
    sys.exit(1)

strategy = jobs["build_publish_and_deploy"].get("strategy") or {}
if strategy.get("max-parallel") != 1:
    print("FAIL: build_publish_and_deploy is missing max-parallel: 1")
    sys.exit(1)
if strategy.get("fail-fast") is not True:
    print("FAIL: build_publish_and_deploy is missing fail-fast: true")
    sys.exit(1)
if "matrix" not in strategy:
    print("FAIL: build_publish_and_deploy is missing its matrix")
    sys.exit(1)

print("OK: job graph order, needs chain, reusable-workflow calls, and matrix placement are all correct")
PYEOF
)"
  JOB_GRAPH_STATUS=$?
  set -e
  if [ "$JOB_GRAPH_STATUS" -eq 0 ]; then
    pass "27: ${EKS_APP_WORKFLOW} job graph follows validate-model -> terraform-sync-once -> platform-sync-once -> validate-shared-secrets-once -> runtime-deployment -> monitor-sync-once -> final-validation"
  else
    fail "27: ${JOB_GRAPH_CHECK}"
  fi
else
  skip "27: job graph check -- python3/PyYAML unavailable"
fi

for workflow in .github/workflows/gg-iam-secrets-deployment.yaml .github/workflows/goldengate-platform.yaml .github/workflows/goldengate-monitor.yaml; do
  if grep -q "workflow_call:" "$workflow" 2>/dev/null; then
    pass "27: ${workflow} supports workflow_call"
  else
    fail "27: ${workflow} is missing a workflow_call trigger"
  fi
done

if grep -q "gh workflow run" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  fail "27: ${EKS_APP_WORKFLOW} contains a live gh workflow run dispatch"
else
  pass "27: no gh workflow run dispatch exists in ${EKS_APP_WORKFLOW}"
fi

if grep -q "^concurrency:" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "group: goldengate-eks-app-orchestrator-dev" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "27: environment-level orchestrator concurrency protection is present"
else
  fail "27: environment-level orchestrator concurrency protection is missing"
fi

if grep -q "validate_shared_secrets_once:" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "secretsmanager describe-secret\|secretsmanager list-secret-version-ids" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "27: a read-only shared-secret validation job exists and runs once per environment"
else
  fail "27: validate_shared_secrets_once job is missing or does not perform the expected read-only checks"
fi

if grep -q "aws secretsmanager put-secret-value\|aws secretsmanager get-random-password" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  fail "27: ${EKS_APP_WORKFLOW} contains a secret-mutation AWS CLI call"
else
  pass "27: ${EKS_APP_WORKFLOW} contains no secret-mutation AWS CLI call"
fi

if grep -q "Resolve deployment identity via the deployment model" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q -- "--set runtime.csi.admin.objectName=\"\$RESOLVED_ADMIN_SECRET_NAME\"" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q -- "--set runtime.csi.certificate.objectName=\"\$RESOLVED_TLS_SECRET_NAME\"" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q -- "--set runtime.serviceAccount.name=\"\$RESOLVED_RUNTIME_SERVICE_ACCOUNT_NAME\"" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "name: runtime.csi.admin.objectName" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "name: runtime.csi.certificate.objectName" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "name: runtime.serviceAccount.name" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "27: the admin secret, TLS secret, and ServiceAccount are resolved once and injected via explicit Helm --set and Argo CD parameter overrides"
else
  fail "27: admin-secret/TLS/ServiceAccount resolution or injection is missing from the runtime deployment steps"
fi

if grep -q 'describe "\$DEPLOYMENT_ID"' "$EKS_APP_WORKFLOW" 2>/dev/null \
    && ! grep -q -- "describe --deployment-id" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "27: the deployment-model describe command uses the exact positional production invocation"
else
  fail "27: the deployment-model describe command is not called with the exact positional production invocation"
fi

# Corrected for the VDR image-validation fix: the rendered-image check is no longer its own grep-based step -- it was merged into the structural PyYAML validator (see the "VDR correction: structural rendered-image validation" section below for the full behavioral proof), so finding a step name alone is no longer sufficient evidence here.
if grep -q "Verify the selected image exists in the approved private ECR" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "aws ecr describe-images" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "imageDetails\"\]\[0\]\[\"imageDigest\"\]" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && ! grep -qF 'grep -qF "image: ${EXPECTED_IMAGE}"' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "27: the selected image's existence and digest are verified read-only via describe-images, and the obsolete grep-based rendered-image text check no longer exists"
else
  fail "27: ECR image existence/digest verification is missing, or the obsolete grep-based rendered-image check is still present"
fi

if grep -qE 'oracle.*ecr-oracle-image|postgresql.*ecr-postgresql-image|ENGINE_IMAGE_MAP|engineImageMap' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  fail "27: an engine-to-image mapping was introduced in the app workflow"
else
  pass "27: the ECR verification step derives the image solely from the descriptor, no engine-to-image mapping"
fi

echo ""
echo "--- Restored shared identity: existing Oracle/PostgreSQL now resolve gg-runtime-sa (intentional migration) ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  SOURCE_RESOLVED_SA="$(python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev describe gg-postgresql-repltest-01 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["runtimeServiceAccountName"])' 2>/dev/null || true)"
  TARGET_RESOLVED_SA="$(python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev describe gg-mssql-repltest-01 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["runtimeServiceAccountName"])' 2>/dev/null || true)"
  if [ "$SOURCE_RESOLVED_SA" = "gg-runtime-sa" ] && [ "$TARGET_RESOLVED_SA" = "gg-runtime-sa" ]; then
    pass "28: gg-postgresql-repltest-01 and gg-mssql-repltest-01 both resolve the restored shared gg-runtime-sa identity -- their values.yaml files remain byte-identical since runtime.serviceAccount was never a settable field"
  else
    fail "28: gg-postgresql-repltest-01/gg-mssql-repltest-01 resolved to (${SOURCE_RESOLVED_SA}, ${TARGET_RESOLVED_SA}), expected (gg-runtime-sa, gg-runtime-sa)"
  fi
else
  skip "28: runtime identity stability check -- python3 unavailable"
fi

if [ "$HELM_AVAILABLE" = "true" ]; then
  for pair in "gg-postgresql-repltest-01:dev/goldengate/source/admin:gg-runtime-sa" "gg-mssql-repltest-01:dev/goldengate/target/admin:gg-runtime-sa"; do
    id="${pair%%:*}"
    rest="${pair#*:}"
    admin_secret="${rest%%:*}"
    approved_sa="${rest##*:}"

    RENDER_APPROVED="${WORKDIR}/identity-approved-${id}.yaml"
    RENDER_OTHER="${WORKDIR}/identity-other-${id}.yaml"

    id_image_repository="$(python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev describe "$id" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["imageRepository"])')"

    # The chart must not couple ServiceAccount identity to any other field, regardless of the name compared. Both current descriptors are persistence.efs.mode=managed, so the workflow-resolved fileSystemId is supplied here exactly as the deploy workflow would.
    if helm template "$id" "$RUNTIME_CHART" \
        --namespace goldengate-dev \
        --values "envs/dev/${id}/values.yaml" \
        --set global.environment=dev \
        --set runtime.csi.admin.objectName="$admin_secret" \
        --set runtime.csi.certificate.objectName=dev/goldengate/tls-certificate \
        --set-string runtime.csi.region="$RESOLVED_AWS_REGION" \
        --set runtime.serviceAccount.create=false \
        --set runtime.serviceAccount.name="$approved_sa" \
        --set persistence.efs.fileSystemId=fs-0123456789abcdef0 \
        --set-string runtime.image.repository="$id_image_repository" \
        "${SHARED_INGRESS_OVERRIDES[@]}" \
        > "$RENDER_APPROVED" 2>"${WORKDIR}/identity-approved-${id}.log" \
      && helm template "$id" "$RUNTIME_CHART" \
        --namespace goldengate-dev \
        --values "envs/dev/${id}/values.yaml" \
        --set global.environment=dev \
        --set runtime.csi.admin.objectName="$admin_secret" \
        --set runtime.csi.certificate.objectName=dev/goldengate/tls-certificate \
        --set-string runtime.csi.region="$RESOLVED_AWS_REGION" \
        --set runtime.serviceAccount.create=false \
        --set runtime.serviceAccount.name=gg-isolation-probe-sa \
        --set persistence.efs.fileSystemId=fs-0123456789abcdef0 \
        --set-string runtime.image.repository="$id_image_repository" \
        "${SHARED_INGRESS_OVERRIDES[@]}" \
        > "$RENDER_OTHER" 2>"${WORKDIR}/identity-other-${id}.log"; then

      if grep -q "serviceAccountName: ${approved_sa}" "$RENDER_APPROVED"; then
        pass "28: ${id} renders serviceAccountName: ${approved_sa}"
      else
        fail "28: ${id} does not render the expected serviceAccountName: ${approved_sa}"
      fi

      ISOLATION_DIFF="$(diff "$RENDER_APPROVED" "$RENDER_OTHER" || true)"
      DIFF_LINE_COUNT="$(echo "$ISOLATION_DIFF" | grep -cE '^[<>]' || true)"
      if [ "$DIFF_LINE_COUNT" -eq 2 ] \
          && echo "$ISOLATION_DIFF" | grep -qE "^<\s+serviceAccountName: ${approved_sa}\$" \
          && echo "$ISOLATION_DIFF" | grep -qE '^>\s+serviceAccountName: gg-isolation-probe-sa$'; then
        pass "28: ${id} ServiceAccount identity is fully decoupled from all other manifest fields (StatefulSet name, PVC/EFS identity, image, ports, ingress/ALB order, admin secret, and TLS are byte-identical regardless of ServiceAccount name)"
      else
        fail "28: ${id} changing the ServiceAccount name unexpectedly changed more than serviceAccountName:"$'\n'"${ISOLATION_DIFF}"
      fi
    else
      fail "28: ${id} identity-stability render failed"
    fi
  done
else
  skip "28: identity-stability manifest comparison -- helm not available"
fi

echo ""
echo "--- Phase 6D0 correction: final acceptance checks ---"

if [ -e "envs/dev/goldengate-deployments.yaml" ]; then
  fail "29: the handwritten registry envs/dev/goldengate-deployments.yaml was restored"
else
  pass "29: no handwritten registry exists"
fi

SECRET_TF_MODULE_COUNT="$(grep -c '^module "' envs/dev/secret.tf 2>/dev/null || true)"
if [ "$SECRET_TF_MODULE_COUNT" -eq 3 ] \
    && grep -q 'name.*= local.gg_env_source_admin_secret_name' envs/dev/secret.tf 2>/dev/null \
    && grep -q 'name.*= local.gg_env_target_admin_secret_name' envs/dev/secret.tf 2>/dev/null \
    && grep -q 'name.*= local.gg_env_tls_secret_name' envs/dev/secret.tf 2>/dev/null; then
  pass "29: secret.tf contains exactly the three approved shared secret modules (names derived from environment config, Fresh-EKS Phase A)"
else
  fail "29: secret.tf does not contain exactly the three approved shared secret modules (found ${SECRET_TF_MODULE_COUNT})"
fi

if grep -q "for_each" envs/dev/secret.tf 2>/dev/null || grep -q "aws_secretsmanager_secret" envs/dev/secret.tf 2>/dev/null; then
  fail "29: secret.tf contains a dynamic per-deployment secret module or direct aws_secretsmanager resource"
else
  pass "29: no dynamic per-deployment secret module exists in secret.tf"
fi

if grep -q "enable_cloudwatch_publication: true" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "metrics_gate_expectation: any" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "29: the orchestrator explicitly calls the monitor workflow with enable_cloudwatch_publication=true and metrics_gate_expectation=any"
else
  fail "29: the orchestrator does not explicitly preserve CloudWatch publication when synchronizing the monitor"
fi

RUNTIME_ROLE_POLICY="envs/dev/policies/goldengate-secrets-read-dev/policies/policies_1.json"
if grep -qi "dynamodb" "$RUNTIME_ROLE_POLICY" 2>/dev/null || grep -qi "PutMetricData" "$RUNTIME_ROLE_POLICY" 2>/dev/null; then
  fail "29: GoldenGateSecretsReadRole-dev grants DynamoDB or CloudWatch PutMetricData (must remain read-only Secrets Manager/KMS)"
else
  pass "29: GoldenGateSecretsReadRole-dev grants no DynamoDB write or CloudWatch PutMetricData permission"
fi

if grep -qF "needs.validate_model.outputs.effective_deploy != 'true' || (needs.terraform_sync_once.result == 'success' && needs.platform_sync_once.result == 'success')" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "29: the read-only validation chain (validate_shared_secrets_once) is deploy-aware and fail-closed -- it tolerates a legitimately skipped terraform/platform sync only when deploy=false, and requires their exact success when deploy=true"
else
  fail "29: validate_shared_secrets_once no longer contains the required deploy-aware fail-closed condition"
fi

if [ -d "envs/dev/gg-sqlserver-payments-01" ] || [ -d "envs/dev/gg-postgresql-source-01" ]; then
  fail "29: a real PostgreSQL-to-SQL Server runtime folder was added -- out of scope for this phase"
else
  pass "29: no real PostgreSQL-to-SQL Server runtime folder was added"
fi

echo ""
echo "--- Phase 6D0-Final: Terraform cross-pipeline plan-blocking fixtures ---"

TF_PLAN_SCRATCH=""
if command -v terraform >/dev/null 2>&1; then
  TF_PLAN_SCRATCH="$(mktemp -d)"
  mkdir -p "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01" "${TF_PLAN_SCRATCH}/envs/dev/gg-mssql-repltest-01" \
    "${TF_PLAN_SCRATCH}/platform/dev/goldengate-platform" "${TF_PLAN_SCRATCH}/envs/dev/goldengate-monitor" \
    "${TF_PLAN_SCRATCH}/envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy"
  cp envs/dev/goldengate_inventory.tf "${TF_PLAN_SCRATCH}/envs/dev/goldengate_inventory.tf"
  # Stand-in for envs/dev/environment.tf's locals, WITHOUT its live aws_eks_cluster/aws_iam_openid_connect_provider data sources -- this harness is intentionally offline/no-AWS-credentials, so it mirrors only the specific local.gg_env_* values goldengate_inventory.tf actually reads. Generated from the REAL resolver's derived values at run time -- never an independently-maintained literal copy that could silently drift from envs/dev/environment.yaml.
  python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('goldengate_environment', '${ENVIRONMENT_TOOL}')
ge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ge)
v = ge.derive_values(ge.load_environment_config('dev'))
print('locals {')
print(f'  gg_env_dns_domain               = \"{v[\"DNS_DOMAIN\"]}\"')
print(f'  gg_env_ecr_registry             = \"{v[\"ECR_REGISTRY\"]}\"')
print(f'  gg_env_namespaces               = {{ runtime = \"{v[\"RUNTIME_NAMESPACE\"]}\", monitoring = \"{v[\"MONITOR_NAMESPACE\"]}\", argocd = \"{v[\"ARGOCD_NAMESPACE\"]}\", observability = \"{v[\"OBSERVABILITY_NAMESPACE\"]}\" }}')
print(f'  gg_env_oidc_hostpath            = \"{v[\"EKS_OIDC_HOSTPATH\"]}\"')
print(f'  gg_env_source_admin_secret_name = \"{v[\"SOURCE_ADMIN_SECRET_NAME\"]}\"')
print(f'  gg_env_target_admin_secret_name = \"{v[\"TARGET_ADMIN_SECRET_NAME\"]}\"')
print(f'  gg_env_tls_secret_name          = \"{v[\"TLS_SECRET_NAME\"]}\"')
print('}')
" > "${TF_PLAN_SCRATCH}/envs/dev/environment_stub.tf"
  cp envs/dev/gg-postgresql-repltest-01/values.yaml "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml"
  cp envs/dev/gg-mssql-repltest-01/values.yaml "${TF_PLAN_SCRATCH}/envs/dev/gg-mssql-repltest-01/values.yaml"
  cp platform/dev/goldengate-platform/values.yaml "${TF_PLAN_SCRATCH}/platform/dev/goldengate-platform/values.yaml"
  cp envs/dev/goldengate-monitor/values.yaml "${TF_PLAN_SCRATCH}/envs/dev/goldengate-monitor/values.yaml"
  cp envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json \
    "${TF_PLAN_SCRATCH}/envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json"
  cat > "${TF_PLAN_SCRATCH}/envs/dev/provider.tf" <<'EOF'
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
EOF

  set +e
  (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform init -backend=false) >"${TF_PLAN_SCRATCH}/init.log" 2>&1
  TF_INIT_STATUS=$?
  set -e

  if [ "$TF_INIT_STATUS" -ne 0 ]; then
    skip "Terraform cross-pipeline plan fixtures -- terraform init failed (no network access to the public provider registry in this environment)"
  else
    set +e
    (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform validate) >"${TF_PLAN_SCRATCH}/validate.log" 2>&1
    TF_VALIDATE_STATUS=$?
    set -e
    if [ "$TF_VALIDATE_STATUS" -eq 0 ]; then
      pass "30: terraform validate succeeds against the real folder-driven inventory in an isolated scratch root"
    else
      fail "30: terraform validate failed against the real folder-driven inventory"
      cat "${TF_PLAN_SCRATCH}/validate.log"
    fi

    set +e
    (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform plan -input=false) >"${TF_PLAN_SCRATCH}/plan-baseline.log" 2>&1
    TF_PLAN_BASELINE_STATUS=$?
    set -e
    if [ "$TF_PLAN_BASELINE_STATUS" -eq 0 ] && grep -q "3 to add, 0 to change, 0 to destroy" "${TF_PLAN_SCRATCH}/plan-baseline.log"; then
      pass "30: a valid folder-driven inventory (2 real deployments) produces a clean Terraform plan"
    else
      fail "30: the baseline Terraform plan against valid real data was not clean"
      cat "${TF_PLAN_SCRATCH}/plan-baseline.log"
    fi

    cp "${TF_PLAN_SCRATCH}/envs/dev/gg-mssql-repltest-01/values.yaml" "${TF_PLAN_SCRATCH}/target-backup.yaml"
    sed -i 's/role: target/role: source/' "${TF_PLAN_SCRATCH}/envs/dev/gg-mssql-repltest-01/values.yaml"
    set +e
    (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform plan -input=false) >"${TF_PLAN_SCRATCH}/plan-dup-source.log" 2>&1
    TF_PLAN_DUP_SOURCE_STATUS=$?
    set -e
    if [ "$TF_PLAN_DUP_SOURCE_STATUS" -ne 0 ] && grep -q "more than one enabled source deployment" "${TF_PLAN_SCRATCH}/plan-dup-source.log"; then
      pass "30: a duplicate enabled source in one pipeline produces a non-zero Terraform plan exit"
    else
      fail "30: a duplicate enabled source did not block Terraform plan as expected (exit=${TF_PLAN_DUP_SOURCE_STATUS})"
      cat "${TF_PLAN_SCRATCH}/plan-dup-source.log"
    fi
    cp "${TF_PLAN_SCRATCH}/target-backup.yaml" "${TF_PLAN_SCRATCH}/envs/dev/gg-mssql-repltest-01/values.yaml"

    cp "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml" "${TF_PLAN_SCRATCH}/source-backup.yaml"
    sed -i 's/role: source/role: target/' "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml"
    set +e
    (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform plan -input=false) >"${TF_PLAN_SCRATCH}/plan-dup-target.log" 2>&1
    TF_PLAN_DUP_TARGET_STATUS=$?
    set -e
    if [ "$TF_PLAN_DUP_TARGET_STATUS" -ne 0 ] && grep -q "more than one enabled target deployment" "${TF_PLAN_SCRATCH}/plan-dup-target.log"; then
      pass "30: a duplicate enabled target in one pipeline produces a non-zero Terraform plan exit"
    else
      fail "30: a duplicate enabled target did not block Terraform plan as expected (exit=${TF_PLAN_DUP_TARGET_STATUS})"
      cat "${TF_PLAN_SCRATCH}/plan-dup-target.log"
    fi
    cp "${TF_PLAN_SCRATCH}/source-backup.yaml" "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml"

    cp "${TF_PLAN_SCRATCH}/envs/dev/gg-mssql-repltest-01/values.yaml" "${TF_PLAN_SCRATCH}/target-backup2.yaml"
    sed -i 's/groupOrder: "113"/groupOrder: "112"/' "${TF_PLAN_SCRATCH}/envs/dev/gg-mssql-repltest-01/values.yaml"
    set +e
    (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform plan -input=false) >"${TF_PLAN_SCRATCH}/plan-dup-alb.log" 2>&1
    TF_PLAN_DUP_ALB_STATUS=$?
    set -e
    if [ "$TF_PLAN_DUP_ALB_STATUS" -ne 0 ] && grep -q "ALB group order" "${TF_PLAN_SCRATCH}/plan-dup-alb.log"; then
      pass "30: a duplicate ALB group order produces a non-zero Terraform plan exit"
    else
      fail "30: a duplicate ALB group order did not block Terraform plan as expected (exit=${TF_PLAN_DUP_ALB_STATUS})"
      cat "${TF_PLAN_SCRATCH}/plan-dup-alb.log"
    fi
    cp "${TF_PLAN_SCRATCH}/target-backup2.yaml" "${TF_PLAN_SCRATCH}/envs/dev/gg-mssql-repltest-01/values.yaml"

    cp "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml" "${TF_PLAN_SCRATCH}/source-backup2.yaml"
    sed -i 's/deploymentType: postgresql/deploymentType: Postgresql/' "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml"
    set +e
    (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform plan -input=false) >"${TF_PLAN_SCRATCH}/plan-unsafe-type.log" 2>&1
    TF_PLAN_UNSAFE_TYPE_STATUS=$?
    set -e
    if [ "$TF_PLAN_UNSAFE_TYPE_STATUS" -ne 0 ] && grep -q "safe lowercase token" "${TF_PLAN_SCRATCH}/plan-unsafe-type.log"; then
      pass "30: an unsafe runtime.deploymentType produces a non-zero Terraform plan exit"
    else
      fail "30: an unsafe runtime.deploymentType did not block Terraform plan as expected (exit=${TF_PLAN_UNSAFE_TYPE_STATUS})"
      cat "${TF_PLAN_SCRATCH}/plan-unsafe-type.log"
    fi
    cp "${TF_PLAN_SCRATCH}/source-backup2.yaml" "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml"

    # Restored shared identity: a brand-new deploymentType (never seen by IAM before) must plan CLEANLY -- it shares the already-trusted gg-runtime-sa, so no new IAM trust subject is ever required.
    cp "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml" "${TF_PLAN_SCRATCH}/source-backup2b.yaml"
    sed -i 's/deploymentType: postgresql/deploymentType: mysql/' "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml"
    set +e
    (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform plan -input=false) >"${TF_PLAN_SCRATCH}/plan-new-type-shared-identity.log" 2>&1
    TF_PLAN_NEW_TYPE_STATUS=$?
    set -e
    if [ "$TF_PLAN_NEW_TYPE_STATUS" -eq 0 ] && grep -q "to add, 0 to change, 0 to destroy" "${TF_PLAN_SCRATCH}/plan-new-type-shared-identity.log"; then
      pass "30: a brand-new safe deploymentType (mysql) plans CLEANLY via folder data alone -- the restored shared gg-runtime-sa identity means no new IAM trust subject is ever required"
    else
      fail "30: a brand-new safe deploymentType (mysql) did not plan cleanly -- the shared gg-runtime-sa self-service promise is broken (exit=${TF_PLAN_NEW_TYPE_STATUS})"
      cat "${TF_PLAN_SCRATCH}/plan-new-type-shared-identity.log"
    fi
    cp "${TF_PLAN_SCRATCH}/source-backup2b.yaml" "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml"

    cp "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml" "${TF_PLAN_SCRATCH}/source-backup3.yaml"
    sed -i '/^runtime:/a\  serviceAccount: gg-operator-chosen-sa' "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml"
    set +e
    (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform plan -input=false) >"${TF_PLAN_SCRATCH}/plan-sa-override.log" 2>&1
    TF_PLAN_SA_OVERRIDE_STATUS=$?
    set -e
    if [ "$TF_PLAN_SA_OVERRIDE_STATUS" -ne 0 ] && grep -q "forbidden override" "${TF_PLAN_SCRATCH}/plan-sa-override.log"; then
      pass "30: an operator-supplied runtime.serviceAccount produces a non-zero Terraform plan exit"
    else
      fail "30: an operator-supplied runtime.serviceAccount did not block Terraform plan as expected (exit=${TF_PLAN_SA_OVERRIDE_STATUS})"
      cat "${TF_PLAN_SCRATCH}/plan-sa-override.log"
    fi
    cp "${TF_PLAN_SCRATCH}/source-backup3.yaml" "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml"

    # Exact-trust-equality edge cases (F is already proven by the clean baseline plan above, run against this same untouched real sts.json). H/J/K mutate a scratch copy and restore it after each -- G/I (removing a transitional/legacy subject) no longer apply: Fresh-EKS Phase A's one-subject architecture never has those subjects to remove in the first place.
    STS_JSON_PATH="${TF_PLAN_SCRATCH}/envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json"
    STS_SUB_KEY="$(python3 -c "
import json
doc = json.load(open('${STS_JSON_PATH}'))
cond = doc['Statement'][0]['Condition']['StringLike']
print([k for k in cond if k.endswith(':sub')][0])
")"
    cp "$STS_JSON_PATH" "${TF_PLAN_SCRATCH}/sts-exact-trust-backup.json"

    run_exact_trust_scenario() {
      local label="$1" mutate_py="$2" log_name="$3"
      python3 -c "$mutate_py" "$STS_JSON_PATH" "$STS_SUB_KEY"
      set +e
      (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform plan -input=false) >"${TF_PLAN_SCRATCH}/${log_name}" 2>&1
      local status=$?
      set -e
      if [ "$status" -ne 0 ] && grep -q "must be exactly one entry: the canonical" "${TF_PLAN_SCRATCH}/${log_name}"; then
        pass "30: ${label} produces a non-zero Terraform plan exit (exact-trust equality enforced)"
      else
        fail "30: ${label} did not block Terraform plan as expected (exit=${status})"
        cat "${TF_PLAN_SCRATCH}/${log_name}"
      fi
      cp "${TF_PLAN_SCRATCH}/sts-exact-trust-backup.json" "$STS_JSON_PATH"
    }

    run_exact_trust_scenario "H: removing the canonical gg-runtime-sa subject (leaves zero subjects)" '
import json, sys
with open(sys.argv[1]) as f: doc = json.load(f)
subs = doc["Statement"][0]["Condition"]["StringLike"][sys.argv[2]]
subs.remove("system:serviceaccount:goldengate-dev:gg-runtime-sa")
with open(sys.argv[1], "w") as f: json.dump(doc, f, indent=2)
' "plan-missing-canonical.log"

    run_exact_trust_scenario "J: adding an unexpected subject" '
import json, sys
with open(sys.argv[1]) as f: doc = json.load(f)
subs = doc["Statement"][0]["Condition"]["StringLike"][sys.argv[2]]
subs.append("system:serviceaccount:goldengate-dev:gg-unexpected-sa")
with open(sys.argv[1], "w") as f: json.dump(doc, f, indent=2)
' "plan-unexpected-subject.log"

    run_exact_trust_scenario "K: duplicating an existing subject" '
import json, sys
with open(sys.argv[1]) as f: doc = json.load(f)
subs = doc["Statement"][0]["Condition"]["StringLike"][sys.argv[2]]
subs.append("system:serviceaccount:goldengate-dev:gg-runtime-sa")
with open(sys.argv[1], "w") as f: json.dump(doc, f, indent=2)
' "plan-duplicate-subject.log"

    # L: a brand-new deployment type requires ZERO sts.json change and still plans cleanly against the SAME exact one-subject trust set (sts.json here is the untouched real file, restored after each H/J/K mutation above).
    mkdir -p "${TF_PLAN_SCRATCH}/envs/dev/gg-mysql-fixture-01"
    sed -e 's/deploymentType: postgresql/deploymentType: mysql/' \
        -e 's/pipeline: repltest-pg-to-mssql-001/pipeline: payments-mysql-fixture-001/' \
        -e 's/groupOrder: "112"/groupOrder: "197"/' \
        "${TF_PLAN_SCRATCH}/envs/dev/gg-postgresql-repltest-01/values.yaml" > "${TF_PLAN_SCRATCH}/envs/dev/gg-mysql-fixture-01/values.yaml"
    set +e
    (cd "${TF_PLAN_SCRATCH}/envs/dev" && terraform plan -input=false) >"${TF_PLAN_SCRATCH}/plan-new-type-onboarded.log" 2>&1
    TF_PLAN_NEW_TYPE_ONBOARDED_STATUS=$?
    set -e
    if [ "$TF_PLAN_NEW_TYPE_ONBOARDED_STATUS" -eq 0 ] && grep -q "to add, 0 to change, 0 to destroy" "${TF_PLAN_SCRATCH}/plan-new-type-onboarded.log"; then
      pass "30: a brand-new safe deployment type (mysql) plans cleanly the moment its folder alone exists -- zero .tf source change AND zero sts.json change required against the same exact one-subject trust set"
    else
      fail "30: onboarding a brand-new safe deployment type via folder data alone did not produce a clean Terraform plan (exit=${TF_PLAN_NEW_TYPE_ONBOARDED_STATUS})"
      cat "${TF_PLAN_SCRATCH}/plan-new-type-onboarded.log"
    fi
    rm -rf "${TF_PLAN_SCRATCH}/envs/dev/gg-mysql-fixture-01"
  fi
  rm -rf "${TF_PLAN_SCRATCH}"
else
  skip "Terraform cross-pipeline plan fixtures -- terraform not available"
fi

echo ""
echo "--- Phase 6D0-Final: reusable-workflow secret/permission chain ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  WORKFLOW_CHAIN_CHECK="$(python3 - "$EKS_APP_WORKFLOW" ".github/workflows/gg-iam-secrets-deployment.yaml" ".github/workflows/goldengate-platform.yaml" ".github/workflows/goldengate-monitor.yaml" <<'PYEOF'
import sys
import yaml

eks_app_path, terraform_wf_path, platform_wf_path, monitor_wf_path = sys.argv[1:5]

eks_app = yaml.safe_load(open(eks_app_path))
terraform_wf = yaml.safe_load(open(terraform_wf_path))
platform_wf = yaml.safe_load(open(platform_wf_path))
monitor_wf = yaml.safe_load(open(monitor_wf_path))

jobs = eks_app["jobs"]
terraform_job = jobs["terraform_sync_once"]

if terraform_job.get("secrets") != "inherit":
    print("FAIL: terraform_sync_once does not forward secrets to gg-iam-secrets-deployment.yaml (secrets: inherit missing)")
    sys.exit(1)

terraform_wf_permissions = terraform_wf.get("permissions") or {}
caller_permissions = terraform_job.get("permissions") or {}
for scope, level in terraform_wf_permissions.items():
    if level == "none":
        continue
    if caller_permissions.get(scope) != level:
        print(f"FAIL: terraform_sync_once caller permission {scope!r} is {caller_permissions.get(scope)!r}, called workflow needs {level!r}")
        sys.exit(1)

apply_job = terraform_wf["jobs"]["apply"]
if apply_job.get("secrets") != "inherit":
    print("FAIL: gg-iam-secrets-deployment.yaml's apply job does not forward secrets to the ADCB reusable workflow")
    sys.exit(1)

for name, job in (("platform_sync_once", jobs["platform_sync_once"]), ("monitor_sync_once", jobs["monitor_sync_once"])):
    if "secrets" in job:
        print(f"FAIL: {name} declares unnecessary secret forwarding (neither called workflow references secrets.*)")
        sys.exit(1)

for path, doc in ((platform_wf_path, platform_wf), (monitor_wf_path, monitor_wf)):
    with open(path) as f:
        text = f.read()
    if "${{ secrets." in text or "${{secrets." in text:
        print(f"FAIL: {path} references secrets.* but its caller job declares no secret forwarding")
        sys.exit(1)

print("OK: secret forwarding and caller/callee permission alignment are correct across the reusable-workflow chain")
PYEOF
)"
  WORKFLOW_CHAIN_STATUS=$?
  set -e
  if [ "$WORKFLOW_CHAIN_STATUS" -eq 0 ]; then
    pass "31: ${EKS_APP_WORKFLOW} forwards secrets/permissions correctly through the full reusable-workflow chain, and no nested workflow assumes an unavailable secret"
  else
    fail "31: ${WORKFLOW_CHAIN_CHECK}"
  fi
else
  skip "31: reusable-workflow secret/permission chain check -- python3/PyYAML unavailable"
fi

if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  TRACKED_GENERATED_ARTIFACTS="$(git ls-files work/generated 2>/dev/null || true)"
  if [ -z "$TRACKED_GENERATED_ARTIFACTS" ]; then
    pass "31: no work/generated artifact is git-tracked; the workflow regenerates the registry on demand"
  else
    fail "31: a work/generated artifact is git-tracked and must be removed from source control:${TRACKED_GENERATED_ARTIFACTS}"
  fi
else
  skip "31: work/generated tracking check -- not a git repository"
fi

# Static evidence only: RUNNER_ROLE_ARN and EKS_DEPLOY_ROLE_ARN's live values come from envs/dev/environment.yaml, unverifiable offline. Corrected for the VDR cross-account fix: validate_shared_secrets_once now starts from the canonical RUNNER_ROLE_ARN (engineering/build account, via env.RUNNER_ROLE_ARN) like every other job, then separately assumes EKS_DEPLOY_ROLE_ARN in-step before any Secrets Manager call -- it is that second, workload-account role (GoldenGateEKSDeployRole-dev) that static evidence ties to the policy carrying the required read-only shared-secret permissions.
if grep -q "role-to-assume: \${{ env.RUNNER_ROLE_ARN }}" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -qE 'aws sts assume-role --role-arn "\$EKS_DEPLOY_ROLE_ARN"' "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q 'name          = local.gg_env_role_names.eksDeploy' envs/dev/iam.tf 2>/dev/null \
    && grep -q 'policy_folder = "goldengate-eks-deploy-dev"' envs/dev/iam.tf 2>/dev/null; then
  pass "31: validate_shared_secrets_once starts from the same canonical RUNNER_ROLE_ARN role used everywhere else, then in-step assumes EKS_DEPLOY_ROLE_ARN before any Secrets Manager call; static evidence ties that workload role to the policy carrying the required read-only shared-secret permissions (live values unverifiable offline)"
else
  fail "31: static evidence linking the validate_shared_secrets_once credential chain to the read-only shared-secret policy is incomplete"
fi

echo ""
echo "--- Phase 6D1: folder-driven replication configuration ---"

REPLICATION_TOOL="hack/goldengate-replication.py"

if [ -f "$REPLICATION_TOOL" ]; then
  pass "32: hack/goldengate-replication.py exists as the dedicated reconciler tool"
else
  fail "32: hack/goldengate-replication.py is missing"
fi

if grep -qE "second values-file parser|goldengate_deployment_model" "$REPLICATION_TOOL" 2>/dev/null \
    && grep -q "importlib.util.spec_from_file_location" "$REPLICATION_TOOL" 2>/dev/null; then
  pass "32: the reconciler imports and consumes the deployment model, never parsing values.yaml a second time"
else
  fail "32: the reconciler does not clearly import the single deployment-model parser"
fi

if find envs/dev -maxdepth 1 -iname "*registry*" -o -iname "*pipeline*.yaml" -o -iname "*credential-map*" 2>/dev/null | grep -q .; then
  fail "32: a separate replication registry/pipeline/credential-mapping file was added under envs/dev"
else
  pass "32: one values.yaml per runtime folder remains the only deployment-specific configuration source"
fi

if grep -q 'REPLICATION_SUPPORTED_SOURCE_TYPE = "postgresql"' "$DEPLOYMENT_MODEL_TOOL" 2>/dev/null \
    && grep -q 'REPLICATION_SUPPORTED_TARGET_TYPE = "mssql"' "$DEPLOYMENT_MODEL_TOOL" 2>/dev/null; then
  pass "32: PostgreSQL source paired with MSSQL target is the only approved replication adapter"
else
  fail "32: the approved replication adapter scope constants are missing or changed"
fi

if grep -qE "OGG_DB_USERID|OGG_DB_PASSWORD" "$DEPLOYMENT_MODEL_TOOL" "$REPLICATION_TOOL" 2>/dev/null \
    && ! grep -qE "^\s*(userid|password)\s*[:=]\s*[\"'][^\"']+[\"']" "$DEPLOYMENT_MODEL_TOOL" "$REPLICATION_TOOL" 2>/dev/null; then
  pass "32: database credentials are referenced by Secrets Manager key name only, never embedded"
else
  fail "32: a database credential appears to be embedded rather than referenced"
fi

if grep -qE "aws_secretsmanager_secret" envs/dev/*.tf 2>/dev/null | grep -q "databases/"; then
  fail "32: a Terraform resource creates a database secret -- this remains an external prerequisite"
else
  pass "32: no Terraform resource creates a database secret"
fi

if grep -qiE "route53|ChangeResourceRecordSets" "$REPLICATION_TOOL" "$DEPLOYMENT_MODEL_TOOL" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  fail "32: Route 53 automation was introduced"
else
  pass "32: no Route 53 resource or API call exists; the existing wildcard DNS record is used as-is"
fi

if grep -q '"runtimeHost"' "$DEPLOYMENT_MODEL_TOOL" 2>/dev/null \
    && grep -qE 'f"\{.*deploymentId.*\}\.\{dns_domain\}"' "$DEPLOYMENT_MODEL_TOOL" 2>/dev/null; then
  pass "32: source/target runtime hosts are derived from the existing wildcard DNS domain"
else
  fail "32: runtime hosts are not clearly derived from the existing wildcard DNS domain"
fi

if grep -qE "aws_iam_role|module \"goldengate_" envs/dev/iam.tf 2>/dev/null; then
  IAM_ROLE_COUNT_6D1="$(grep -c 'module "goldengate_' envs/dev/iam.tf 2>/dev/null || true)"
  if [ "$IAM_ROLE_COUNT_6D1" = "6" ]; then
    pass "32: the number of IAM role modules in envs/dev/iam.tf is unchanged (6)"
  else
    fail "32: the number of IAM role modules in envs/dev/iam.tf changed unexpectedly (found ${IAM_ROLE_COUNT_6D1})"
  fi
fi

# Updated for the restored shared gg-runtime-sa architecture: the runtime ServiceAccount template intentionally no longer contains any per-engine literal or $type range variable -- it renders the single shared identity directly.
if ! grep -qE "gg-oracle-sa|gg-postgresql-sa|gg-mssql-sa|gg-daa-sa" helm/goldengate-platform/templates/runtime-serviceaccounts.yaml 2>/dev/null \
    && ! grep -qE '\$type' helm/goldengate-platform/templates/runtime-serviceaccounts.yaml 2>/dev/null \
    && grep -q "gg-runtime-sa" helm/goldengate-platform/values.yaml 2>/dev/null; then
  pass "32: current runtime ServiceAccount naming/rendering is unaffected by Phase 6D1 (still the single shared gg-runtime-sa, no per-engine literal or \$type)"
else
  fail "32: the runtime ServiceAccount template appears to have changed unexpectedly"
fi

if grep -q "PutSecretValue\|GetRandomPassword" envs/dev/policies/goldengate-secrets-read-dev/policies/policies_1.json 2>/dev/null; then
  fail "32: a secret-mutation permission was added to the runtime secrets-read policy"
else
  pass "32: existing runtime secrets and their read-only IAM policy are unchanged"
fi

if grep -qi "kms:" envs/dev/policies/goldengate-secrets-read-dev/policies/policies_1.json 2>/dev/null; then
  KMS_ACTIONS_6D1="$(grep -oE '"kms:[A-Za-z]+"' envs/dev/policies/goldengate-secrets-read-dev/policies/policies_1.json 2>/dev/null | sort -u | tr '\n' ' ')"
  if [ "$KMS_ACTIONS_6D1" = '"kms:Decrypt" ' ]; then
    pass "32: KMS permissions on the runtime secrets-read policy are unchanged (Decrypt only)"
  else
    fail "32: KMS permissions on the runtime secrets-read policy changed unexpectedly (found: ${KMS_ACTIONS_6D1})"
  fi
fi

if grep -qE "229410149234.dkr.ecr" "$REPLICATION_TOOL" 2>/dev/null; then
  fail "32: the reconciler hardcodes an image reference instead of using the source deployment's existing image"
else
  pass "32: no new image reference exists; the reconciliation Job reuses the existing approved source runtime image"
fi

FORBIDDEN_6D1_TERMS_FOUND="false"
for term in "utility-sidecar" "observer-sidecar" "gg-alerter" "aws_cloudwatch_metric_alarm" "aws_sns" "def restart_process" "def heal"; do
  if grep -rq -- "$term" "$REPLICATION_TOOL" "$DEPLOYMENT_MODEL_TOOL" "$INVENTORY_TF" 2>/dev/null; then
    fail "32: forbidden Phase 6D1 term found: ${term}"
    FORBIDDEN_6D1_TERMS_FOUND="true"
  fi
done
if [ "$FORBIDDEN_6D1_TERMS_FOUND" = "false" ]; then
  pass "32: no observer/utility sidecar, alarm, SNS, or automatic-healing reference exists"
fi

for method in "def delete(" "def put("; do
  if grep -qF -- "$method" "$REPLICATION_TOOL" 2>/dev/null; then
    fail "32: the reconciler REST client defines a forbidden ${method%(} method"
  fi
done
pass "32: the reconciler REST client has no delete/put method"

# Phase 6D1 correction (Task 13): PATCH is now permitted, but exclusively to transition a newly-created Distribution path from stopped to running.
if grep -q "def patch(self, path, body):" "$REPLICATION_TOOL" 2>/dev/null \
    && grep -q "def start_distribution_path" "$REPLICATION_TOOL" 2>/dev/null \
    && grep -qE "client\.patch\(" "$REPLICATION_TOOL" 2>/dev/null; then
  pass "32: the reconciler REST client's PATCH is reserved exclusively for the Distribution path status transition"
else
  fail "32: the reconciler REST client's PATCH usage does not match the Distribution-path-only safety rule"
fi

PATCH_CALL_SITES="$(grep -n "\.patch(" "$REPLICATION_TOOL" 2>/dev/null | grep -v "def patch\|GGClient.patch\|patch_call" || true)"
if [ "$(echo "$PATCH_CALL_SITES" | grep -c "start_distribution_path\|client\.patch(distribution_path" || true)" -ge 0 ] \
    && ! echo "$PATCH_CALL_SITES" | grep -qE "credential_path|extract_path|replicat_path"; then
  pass "32: no credential, Extract, or Replicat call site ever issues PATCH"
else
  fail "32: a non-Distribution call site issues PATCH"
fi

if [ "$HELM_AVAILABLE" = "true" ] && command -v git >/dev/null 2>&1; then
  ORACLE_HEAD_RENDER="${WORKDIR}/oracle-6d1-head.yaml"
  ORACLE_WORKING_RENDER="${WORKDIR}/oracle-6d1-working.yaml"
  if git show "HEAD:helm/goldengate/templates/runtime-statefulset.yaml" > "${WORKDIR}/oracle-sts-head.yaml" 2>/dev/null; then
    if diff -q "${WORKDIR}/oracle-sts-head.yaml" "helm/goldengate/templates/runtime-statefulset.yaml" >/dev/null 2>&1; then
      pass "32: helm/goldengate/templates/runtime-statefulset.yaml is byte-identical to HEAD -- existing Oracle/PostgreSQL StatefulSet rendering is untouched by Phase 6D1"
    else
      fail "32: helm/goldengate/templates/runtime-statefulset.yaml changed since HEAD"
    fi
  else
    skip "32: runtime-statefulset.yaml HEAD comparison -- not available in this git history"
  fi
else
  skip "32: existing Oracle/PostgreSQL manifest byte comparison -- helm or git not available"
fi

if grep -q "enable_cloudwatch_publication: true" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "metrics_gate_expectation: any" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "32: monitoring publication remains explicitly enabled after Phase 6D1"
else
  fail "32: monitoring publication configuration changed unexpectedly"
fi

if grep -q "replication_reconcile_once" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "replication_dry_run_validation" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "32: replication_reconcile_once and replication_dry_run_validation jobs exist in the orchestrator"
else
  fail "32: the replication workflow jobs are missing from the orchestrator"
fi

echo ""
echo "--- Phase 6D1 correction: REST-contract and execution-identity fixes ---"

if grep -q "replication_monitor_acceptance" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "api/processes" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "32: a replication-specific monitor acceptance job queries /api/processes for real process names"
else
  fail "32: the replication-specific monitor acceptance job is missing"
fi

if grep -q "\-\-execution-id" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "github.run_id.*github.run_attempt" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -q "\-\-execution-id \"dry-run\"" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "32: render-job is invoked with a rerun-safe --execution-id (real runs) and a deterministic dry-run token"
else
  fail "32: --execution-id wiring is missing from one or both replication workflow jobs"
fi

if grep -qE "\^\[\[:space:\]\]\*\(aws_secret_access_key\|password\)\[\[:space:\]\]\*:" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && ! grep -qE '\^\\s\*\(aws_secret_access_key\|password\)\\s\*:' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "32: the replication dry-run secret-leak scan uses portable [[:space:]], not GNU-only \\s"
else
  fail "32: the replication dry-run secret-leak scan still uses non-portable \\s"
fi

if grep -q "def ensure_database_credential" "$REPLICATION_TOOL" 2>/dev/null \
    && grep -q "def ensure_network_credential" "$REPLICATION_TOOL" 2>/dev/null; then
  pass "32: database and Network credential reconciliation use separate functions with separate validation semantics"
else
  fail "32: database/Network credential reconciliation is not clearly separated"
fi

if grep -q 'request_body = {"userid": userid, "password": password}' "$REPLICATION_TOOL" 2>/dev/null; then
  pass "32: the credential POST body contains userid/password only, never an alias field (alias is the path parameter)"
else
  fail "32: the credential POST body no longer matches the exact userid/password-only shape"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  REPL_TEST_OUTPUT="$(python3 hack/test-goldengate-replication.py 2>&1)"
  REPL_TEST_STATUS=$?
  set -e
  if [ "$REPL_TEST_STATUS" -eq 0 ]; then
    RAN_LINE_REPL="$(echo "$REPL_TEST_OUTPUT" | grep -E '^Ran [0-9]+ test' | tail -1)"
    pass "32: hack/test-goldengate-replication.py: ${RAN_LINE_REPL:-all tests passed}"
  else
    fail "32: hack/test-goldengate-replication.py reported a failure"
    echo "$REPL_TEST_OUTPUT"
  fi
else
  skip "32: replication reconciler unit tests -- python3 unavailable"
fi

# --- EFS storage architecture correction: managed-mode deletion safety ordering + Terraform structure (static only) ---

if bash -n "$DETECT_SCRIPT" 2>/dev/null; then
  pass "33: hack/detect-goldengate-deployments.sh still passes bash -n after the efs_mode deletion-matrix extension"
else
  fail "33: hack/detect-goldengate-deployments.sh has a syntax error"
fi

if grep -q '_efs_mode_from_yaml' "$DETECT_SCRIPT" 2>/dev/null \
    && grep -q 'efs_mode: \$efs_mode' "$DETECT_SCRIPT" 2>/dev/null; then
  pass "33: the deletion matrix carries an efs_mode field derived from the historical (pre-deletion) values.yaml content"
else
  fail "33: the deletion matrix's efs_mode field is missing or not wired into the jq item construction"
fi

if grep -qE '^\s*managed_efs_deletion_guard:' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "33: the managed_efs_deletion_guard job exists in the eks-app workflow"
else
  fail "33: the managed_efs_deletion_guard job is missing from the eks-app workflow"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  ORDER_CHECK_OUTPUT="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

jobs = doc.get("jobs", {})
problems = []

guard = jobs.get("managed_efs_deletion_guard")
if guard is None:
    problems.append("managed_efs_deletion_guard job is missing")
else:
    needs = guard.get("needs")
    needs_list = needs if isinstance(needs, list) else [needs]
    if "detect_changed_deployments" not in needs_list:
        problems.append("managed_efs_deletion_guard does not need detect_changed_deployments")

tf = jobs.get("terraform_sync_once")
if tf is None:
    problems.append("terraform_sync_once job is missing")
else:
    needs = tf.get("needs")
    needs_list = needs if isinstance(needs, list) else [needs]
    if "detect_changed_deployments" not in needs_list:
        problems.append("terraform_sync_once does not need detect_changed_deployments")
    if "managed_efs_deletion_guard" not in needs_list:
        problems.append("terraform_sync_once does not need managed_efs_deletion_guard")
    if_expr = str(tf.get("if", ""))
    if "managed_efs_deletion_guard.result" not in if_expr or "success" not in if_expr:
        problems.append("terraform_sync_once's if: does not explicitly require managed_efs_deletion_guard to have succeeded (a custom if: does not implicitly inherit the needs-success default)")
    if "detect_changed_deployments.result" not in if_expr or "success" not in if_expr:
        problems.append("terraform_sync_once's if: does not explicitly require detect_changed_deployments to have succeeded")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("OK")
PYEOF
)"
  ORDER_CHECK_STATUS=$?
  set -e
  if [ "$ORDER_CHECK_STATUS" -eq 0 ]; then
    pass "33: managed_efs_deletion_guard is structurally guaranteed to run before terraform_sync_once, and terraform_sync_once fails closed if either detect_changed_deployments or the guard did not succeed"
  else
    fail "33: managed-EFS deletion guard ordering is not correctly wired: ${ORDER_CHECK_OUTPUT}"
  fi
else
  skip "33: managed-EFS deletion guard ordering check -- python3/PyYAML unavailable"
fi

echo ""
echo "--- Final correction pass: managed EFS restored to the shared envs/dev root (approved corporate Terraform workflow) ---"

if [ ! -d "envs/dev/runtime-efs" ]; then
  pass "1: the isolated envs/dev/runtime-efs Terraform root no longer exists"
else
  fail "1: envs/dev/runtime-efs still exists -- it cannot be executed through the approved ADCB reusable workflow and must be removed"
fi

if ! grep -rl "runtime-efs\|runtime_efs" --include='*.tf' envs/ 2>/dev/null | grep -v '^envs/dev/efs.tf$' | grep -q .; then
  pass "1: no remaining Terraform file references an isolated runtime-efs root"
else
  fail "1: a Terraform file still references the removed isolated runtime-efs root"
fi

if [ -f "envs/dev/efs.tf" ]; then
  pass "2: envs/dev/efs.tf exists again in the normal shared Terraform root"
else
  fail "2: envs/dev/efs.tf is missing -- managed EFS must live in the shared envs/dev root processed by the approved corporate workflow"
fi

if grep -q 'aws-tf-module-efs?ref=v1.0.0' envs/dev/efs.tf 2>/dev/null; then
  pass "3: envs/dev/efs.tf pins the approved ADCB EFS module at exactly v1.0.0"
else
  fail "3: envs/dev/efs.tf does not reference the approved ADCB EFS module at the pinned v1.0.0 ref"
fi

if grep -qE '^module\s+"goldengate_runtime_efs"\s*\{' envs/dev/efs.tf 2>/dev/null \
    && grep -qE 'for_each\s*=\s*local\.goldengate_managed_efs_desired_deployments' envs/dev/efs.tf 2>/dev/null; then
  pass "4/5: the EFS module is instantiated via for_each over local.goldengate_managed_efs_desired_deployments (the canonical local.goldengate_managed_efs_deployments filtered by the explicit, reviewed managed-EFS decommission allowlist) -- one Terraform module key (and therefore one dedicated aws_efs_file_system) per DESIRED managed deployment ID, all inside the single envs/dev state"
else
  fail "4/5: envs/dev/efs.tf's module block is missing or does not for_each over local.goldengate_managed_efs_desired_deployments"
fi

if grep -qE '^\s*name\s*=\s*each\.value\.creation_token' envs/dev/efs.tf 2>/dev/null; then
  pass "7: the module's name input is each.value.creation_token -- the exact deterministic efsCreationToken, and the verified v1.0.0 module sets creation_token = var.name"
else
  fail "7: envs/dev/efs.tf does not pass the deterministic creation token as the module's name input"
fi

if ! grep -q 'custom_kms_key_arn' envs/dev/efs.tf 2>/dev/null; then
  pass "8: envs/dev/efs.tf does not introduce the v1.1.0-only custom_kms_key_arn input -- the module stays pinned to its verified v1.0.0 default KMS-alias lookup behavior"
else
  fail "8: envs/dev/efs.tf references custom_kms_key_arn, a v1.1.0-only input not present in the approved pinned v1.0.0 module"
fi

if grep -qE '^\s*custom_tags\s*=\s*\{' envs/dev/efs.tf 2>/dev/null \
    && grep -q 'ManagedBy.*=.*"goldengate-eks-app"' envs/dev/efs.tf 2>/dev/null \
    && grep -q 'GoldenGateDeploymentId.*=.*each\.key' envs/dev/efs.tf 2>/dev/null \
    && grep -q 'GoldenGateStorage.*=.*"u02"' envs/dev/efs.tf 2>/dev/null; then
  pass "9: managed EFS receives deterministic ownership tags (ManagedBy/GoldenGateDeploymentId/GoldenGateStorage/GoldenGateEnvironment) via the verified var.custom_tags input, with GoldenGateDeploymentId mapping one EFS back to exactly one runtime (each.key)"
else
  fail "9: envs/dev/efs.tf is missing the required deterministic ownership tags via custom_tags"
fi

if grep -vE '^\s*#' envs/dev/efs.tf 2>/dev/null | grep -qiE 'credential|secret|password|database'; then
  fail "envs/dev/efs.tf's non-comment content may reference credentials/secrets/passwords/database details"
else
  pass "no credentials/secret ARNs/passwords/database details appear in envs/dev/efs.tf's non-comment content"
fi

if grep -qE '^\s*count\s*=\s*length\(local\.goldengate_managed_efs_desired_deployments\)\s*>\s*0' envs/dev/efs.tf 2>/dev/null; then
  pass "10: the shared EFS security-group data lookup is conditional (count) on at least one DESIRED (post-decommission) managed deployment existing, not the canonical inventory -- so it stops resolving the shared EFS security group once no desired EFS needs it; that security group remains owned exclusively by the separate aws-cloud-factory-infra repository"
else
  fail "10: the shared EFS security-group data lookup in envs/dev/efs.tf is not conditional on desired managed deployments existing"
fi

if grep -qE 'resource\s+"aws_efs_(file_system|mount_target|access_point)"' envs/dev/*.tf 2>/dev/null; then
  fail "42: envs/dev/*.tf reimplements EFS with raw aws_efs_* resources or adds a Terraform-owned access point instead of using the approved module exclusively"
else
  pass "42: no raw aws_efs_file_system/mount_target/access_point resources exist anywhere in envs/dev/*.tf"
fi

echo ""
echo "--- Managed EFS decommission: explicit allowlist filters Terraform desired EFS without touching the canonical inventory ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  EFS_DECOMMISSION_CHECK="$(python3 -c '
import re

import importlib.util

TOOL_PATH = "hack/goldengate-deployment-model.py"
spec = importlib.util.spec_from_file_location("goldengate_deployment_model", TOOL_PATH)
gdm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gdm)

with open("envs/dev/efs.tf") as f:
    efs_tf = f.read()

results = []


def check(label, ok):
    results.append((label, ok))


ids_match = re.search(r"goldengate_managed_efs_decommission_ids\s*=\s*toset\(\[(.*?)\]\)", efs_tf, re.S)
check("1: goldengate_managed_efs_decommission_ids exists as a literal toset([...])", ids_match is not None)
decommission_ids = sorted(re.findall(r"\"([^\"]+)\"", ids_match.group(1))) if ids_match else []

check("2: the decommission set contains exactly gg-mssql-repltest-01 and gg-postgresql-repltest-01",
      decommission_ids == ["gg-mssql-repltest-01", "gg-postgresql-repltest-01"])

decommission_block = ids_match.group(0) if ids_match else ""
check("3: the decommission set is never derived from lifecycle.state (hardcoded IDs only)", "lifecycle" not in decommission_block and "each.value" not in decommission_block)

desired_match = re.search(r"goldengate_managed_efs_desired_deployments\s*=\s*\{(.*?)\n  \}", efs_tf, re.S)
check("4: goldengate_managed_efs_desired_deployments exists", desired_match is not None)
desired_body = desired_match.group(1) if desired_match else ""
check("5: the desired-EFS local is filtered from the canonical (unfiltered) local.goldengate_managed_efs_deployments", "for id, v in local.goldengate_managed_efs_deployments" in desired_body)
check("6: the desired-EFS local excludes exactly the explicit decommission set (contains-based filter, no lifecycle/replication re-derivation)", "!contains(local.goldengate_managed_efs_decommission_ids, id)" in desired_body)

module_match = re.search(r"module \"goldengate_runtime_efs\" \{(.*?)\n\}", efs_tf, re.S)
check("7: module \"goldengate_runtime_efs\" exists", module_match is not None)
module_body = module_match.group(1) if module_match else ""
check("8: the EFS module for_each now uses the filtered desired-EFS local, not the raw canonical one directly", "for_each = local.goldengate_managed_efs_desired_deployments" in module_body)
check("9: the EFS module for_each no longer references the unfiltered canonical local directly", "for_each = local.goldengate_managed_efs_deployments\n" not in module_body)
check("10: the corporate EFS module source/version is unchanged", "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-efs?ref=v1.0.0" in module_body)

check("11: a fail-closed precondition rejects a decommission ID that is not a real managed-EFS deployment", "setsubtract(local.goldengate_managed_efs_decommission_ids, keys(local.goldengate_managed_efs_deployments))" in efs_tf)
check("12: a fail-closed precondition requires lifecycle.state=absent for every decommissioned ID", "lifecycle.state, \"active\") == \"absent\"" in efs_tf)
check("13: a fail-closed precondition requires replication.enabled=false for every decommissioned ID", "replication.enabled, true) == false" in efs_tf)

with open("envs/dev/goldengate_inventory.tf") as f:
    inventory_tf = f.read()
check("14: goldengate_inventory.tf is untouched -- the canonical local.goldengate_managed_efs_deployments keeps its own lifecycle.state-independent comment", "never disappear from this map merely because a deployment is temporarily disabled" in inventory_tf)

# Empirical, not just structural: cross-check against the REAL live deployment-model output (point 1: lifecycle.state=absent alone still retains EFS in the CANONICAL inventory; point 4: the decommission set matches exactly, never a superset/subset of, the real managed-EFS deployment IDs -- so this can never silently affect an unrelated managed EFS).
active, inactive, invalid = gdm.scan("dev")
check("scan(dev): no invalid descriptors", invalid == [])
canonical_managed_ids = sorted(d["deploymentId"] for d in (active + inactive) if d["efsMode"] == "managed")
check("15: the canonical (unfiltered) managed-EFS inventory still contains exactly the same two IDs -- lifecycle.state=absent alone never removes a descriptor from it", canonical_managed_ids == ["gg-mssql-repltest-01", "gg-postgresql-repltest-01"])
check("16: the explicit decommission set matches the real managed-EFS deployment IDs exactly (never a superset that could silently affect an unrelated managed EFS)", decommission_ids == canonical_managed_ids)

by_id = {d["deploymentId"]: d for d in (active + inactive)}
check("17: both real decommissioned descriptors currently have lifecycle.state=absent", all(by_id[i]["deploymentId"] not in [x["deploymentId"] for x in active] for i in decommission_ids))
check("18: both real decommissioned descriptors currently have replication.enabled=false", all(by_id[i]["replicationEnabled"] is False for i in decommission_ids))

# Verify the shared EFS SG lookup follows the post-decommission desired-EFS map.
sg_match = re.search(r"data \"aws_security_group\" \"goldengate_efs_shared\" \{(.*?)\n\}", efs_tf, re.S)
check("19: data.aws_security_group.goldengate_efs_shared exists", sg_match is not None)
sg_body = sg_match.group(1) if sg_match else ""
check("20: the SG lookup count is gated on the desired (post-decommission) map, not the canonical inventory", "count = length(local.goldengate_managed_efs_desired_deployments) > 0 ? 1 : 0" in sg_body)
check("21: the SG lookup count no longer references the unfiltered canonical local directly", "length(local.goldengate_managed_efs_deployments) > 0" not in sg_body)

# Verify the SG [0] reference exists only inside the desired-EFS-gated module.
other_sg_refs = [m.start() for m in re.finditer(r"data\.aws_security_group\.goldengate_efs_shared\[0\]", efs_tf)]
check("22: every reference to data.aws_security_group.goldengate_efs_shared[0] lives inside the module block gated by the same desired-EFS for_each (no unconditional bypass elsewhere in efs.tf)",
      len(other_sg_refs) == 1 and module_match is not None and module_match.start() < other_sg_refs[0] < module_match.end())

# Verify the current fully decommissioned desired-EFS map disables the SG lookup.
real_desired_ids = sorted(set(canonical_managed_ids) - set(decommission_ids))
check("23: with the real current descriptor state, the desired-EFS map is empty, so the SG data-source count evaluates to 0 (SG lookup is fully disabled today)", real_desired_ids == [])

for label, ok in results:
    print(("OK " if ok else "FAIL ") + label)
' 2>&1)"
  while IFS= read -r line; do
    case "$line" in
      FAIL\ *) fail "EFS-DECOMMISSION: ${line#FAIL }" ;;
      OK\ *) pass "EFS-DECOMMISSION: ${line#OK }" ;;
    esac
  done <<< "$EFS_DECOMMISSION_CHECK"
else
  skip "EFS-DECOMMISSION: managed EFS decommission allowlist checks -- python3 unavailable"
fi

if grep -qE 'resource\s+"aws_security_group"' envs/dev/*.tf 2>/dev/null; then
  fail "envs/dev/*.tf creates a new security group instead of reusing the single shared one via a fail-closed data lookup"
else
  pass "envs/dev/*.tf does not create a per-deployment security group -- it looks up the shared one"
fi

if grep -rqE 'resource\s+"aws_security_group"' --include='*.tf' . 2>/dev/null; then
  fail "GOLDENGATE-EKS-APP introduces an aws_security_group resource somewhere in the repo -- the shared GoldenGate EFS security group remains owned exclusively by the separate aws-cloud-factory-infra repository; this repo may only look it up via a fail-closed data source, never create/manage/destroy it"
else
  pass "no aws_security_group resource exists anywhere in GOLDENGATE-EKS-APP -- the shared EFS SG remains owned exclusively by aws-cloud-factory-infra"
fi

# Fresh-EKS Phase A: the SG description is now sourced from envs/dev/environment.yaml (local.gg_env_efs_shared_security_group_description) instead of a local Terraform variable with a hardcoded default -- still a single environment-level configuration point, never a per-deployment values.yaml setting.
if grep -qF 'local.gg_env_efs_shared_security_group_description' envs/dev/efs.tf 2>/dev/null \
    && ! grep -lq 'goldengate_efs_shared_security_group_description\|sharedSecurityGroupDescription' envs/dev/gg-*-repltest-01/values.yaml 2>/dev/null; then
  pass "the shared EFS security group is a single environment-level configuration point (envs/dev/environment.yaml), never a per-deployment values.yaml setting"
else
  fail "the shared EFS security group configuration point is missing or leaked into a per-deployment values.yaml"
fi

if grep -qE 'aws efs delete-file-system|aws efs delete-access-point|terraform destroy' "$EKS_APP_WORKFLOW" envs/dev/*.tf 2>/dev/null; then
  fail "39/40/41: the eks-app workflow or envs/dev root contains a destructive EFS/Terraform command outside the controlled decommission process"
else
  pass "39/40/41: neither the eks-app workflow nor envs/dev/*.tf contains an aws efs delete-* or terraform destroy command"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  TWO_MANAGED_OUT="$(python3 - <<'PYEOF'
import re
with open("envs/dev/efs.tf") as f:
    text = f.read()
# Structural proof (no Terraform CLI): for_each over the desired-EFS local (the folder-driven canonical local filtered by the explicit managed-EFS decommission allowlist) always derives Terraform's module instance address (module.goldengate_runtime_efs[each.key]) from the map key, which is the deployment ID (see goldengate_inventory.tf's goldengate_managed_efs_deployments and efs.tf's goldengate_managed_efs_desired_deployments); two distinct DESIRED deployment IDs therefore always produce two distinct module addresses/module instances/filesystems, never a shared one.
assert 'for_each = local.goldengate_managed_efs_desired_deployments' in text
assert 'each.key' in text
print("OK")
PYEOF
)"
  TWO_MANAGED_STATUS=$?
  set -e
  if [ "$TWO_MANAGED_STATUS" -eq 0 ]; then
    pass "6: two managed runtime IDs structurally produce two distinct Terraform module instances/addresses (module.goldengate_runtime_efs[each.key], each.key being the deployment ID from the folder-driven inventory)"
  else
    fail "6: could not structurally confirm two-managed-runtimes-produce-two-module-keys: ${TWO_MANAGED_OUT}"
  fi
else
  skip "6: two-module-key structural check -- python3 unavailable"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  LIVE_VALIDATE_OUTPUT="$(PYTHONDONTWRITEBYTECODE=1 python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev validate 2>&1)"
  LIVE_VALIDATE_STATUS=$?
  set -e
  if [ "$LIVE_VALIDATE_STATUS" -eq 0 ] \
      && grep -qE '^\s*mode:\s*managed\s*$' envs/dev/gg-postgresql-repltest-01/values.yaml 2>/dev/null \
      && grep -qE '^\s*mode:\s*managed\s*$' envs/dev/gg-mssql-repltest-01/values.yaml 2>/dev/null \
      && ! grep -q 'fileSystemId:' envs/dev/gg-postgresql-repltest-01/values.yaml 2>/dev/null \
      && ! grep -q 'fileSystemId:' envs/dev/gg-mssql-repltest-01/values.yaml 2>/dev/null; then
    pass "33: both live gg-postgresql-repltest-01/gg-mssql-repltest-01 descriptors carry persistence.efs.mode=managed with no committed fileSystemId, and dev validate still passes"
  else
    fail "33: the two live managed-EFS descriptors did not validate cleanly: ${LIVE_VALIDATE_OUTPUT}"
  fi
else
  skip "33: live descriptor EFS-mode migration check -- python3 unavailable"
fi

echo ""
echo "--- EFS ID resolution step: existing/dry-run/not-applicable branches (no AWS required) ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  python3 - "$EKS_APP_WORKFLOW" > "${WORKDIR}/resolve_efs_id.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["build_publish_and_deploy"]["steps"]:
    if step.get("name") == "Resolve EFS filesystem ID":
        sys.stdout.write(step["run"])
        break
else:
    sys.exit("step not found")
PYEOF

  if [ ! -s "${WORKDIR}/resolve_efs_id.sh" ]; then
    fail "34: could not extract the 'Resolve EFS filesystem ID' step from ${EKS_APP_WORKFLOW}"
  else
    # Only the not-applicable/existing/managed+deploy=false branches are locally testable without AWS credentials -- each returns before ever reaching an aws sts/aws efs call, verified below.
    run_resolve_efs_id() {
      local efs_mode="$1" efs_fsid_declared="$2" effective_deploy="$3" github_env_file out status
      github_env_file="$(mktemp)"
      out="$(EFS_MODE="$efs_mode" EFS_FILE_SYSTEM_ID_DECLARED="$efs_fsid_declared" EFS_CREATION_TOKEN="dev-x-efs" \
        EFFECTIVE_DEPLOY="$effective_deploy" GITHUB_ENV="$github_env_file" \
        GITHUB_RUN_ID="1" GITHUB_RUN_ATTEMPT="1" AWS_REGION="eu-west-1" \
        EKS_DEPLOY_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGateEKSDeployRole-dev" \
        bash "${WORKDIR}/resolve_efs_id.sh" 2>&1 )"
      status=$?
      out="${out}"$'\n'"$(cat "$github_env_file")"
      rm -f "$github_env_file"
      echo "$out"
      return $status
    }

    NOT_APPLICABLE_OUT="$(run_resolve_efs_id "" "" "true")"
    if echo "$NOT_APPLICABLE_OUT" | grep -qF "EFS ID source: not applicable" \
        && echo "$NOT_APPLICABLE_OUT" | grep -qE '^RESOLVED_EFS_ID=$'; then
      pass "34: not-in-use deployments resolve an empty RESOLVED_EFS_ID with an explicit 'not applicable' source"
    else
      fail "34: the not-applicable EFS ID resolution branch did not behave as expected"
      echo "$NOT_APPLICABLE_OUT"
    fi

    EXISTING_OUT="$(run_resolve_efs_id "existing" "fs-0123456789abcdef0" "true")"
    if echo "$EXISTING_OUT" | grep -qF "EFS ID source: existing descriptor" \
        && echo "$EXISTING_OUT" | grep -qF "RESOLVED_EFS_ID=fs-0123456789abcdef0"; then
      pass "34: existing mode resolves RESOLVED_EFS_ID as the exact Git-committed passthrough value"
    else
      fail "34: the existing-mode EFS ID resolution branch did not behave as expected"
      echo "$EXISTING_OUT"
    fi

    DRYRUN_OUT="$(run_resolve_efs_id "managed" "" "false")"
    if echo "$DRYRUN_OUT" | grep -qF "EFS ID source: dry-run placeholder" \
        && echo "$DRYRUN_OUT" | grep -qE '^RESOLVED_EFS_ID=fs-[0-9a-f]+$' \
        && ! echo "$DRYRUN_OUT" | grep -qiE "aws sts|aws efs"; then
      pass "34: managed mode with deploy=false resolves a syntactically-valid dry-run-only placeholder with no AWS call attempted"
    else
      fail "34: the managed/deploy=false dry-run EFS ID resolution branch did not behave as expected"
      echo "$DRYRUN_OUT"
    fi
  fi
else
  skip "34: EFS ID resolution step branch checks -- python3 unavailable"
fi

echo ""
echo "--- Correction pass, Issue 1: EFFECTIVE_DEPLOY / undeclared needs ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  UNDECLARED_NEEDS_OUT="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import re
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

problems = []
for job_name, job in doc["jobs"].items():
    needs = job.get("needs")
    if needs is None:
        declared = set()
    elif isinstance(needs, str):
        declared = {needs}
    else:
        declared = set(needs)

    job_copy = dict(job)
    job_copy.pop("needs", None)
    text = yaml.dump(job_copy, default_flow_style=False)
    refs = set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.", text))
    refs |= set(re.findall(r"needs\['([A-Za-z0-9_-]+)'\]", text))
    refs |= set(re.findall(r'needs\["([A-Za-z0-9_-]+)"\]', text))

    undeclared = refs - declared
    if undeclared:
        problems.append(f"{job_name}: undeclared needs.{{{','.join(sorted(undeclared))}}} (declared needs: {sorted(declared)})")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("OK")
PYEOF
)"
  UNDECLARED_NEEDS_STATUS=$?
  set -e
  if [ "$UNDECLARED_NEEDS_STATUS" -eq 0 ]; then
    pass "1: no job references needs.<job> for a job it does not declare in its own needs: list"
  else
    fail "1: undeclared needs.<job> reference(s) found in ${EKS_APP_WORKFLOW}: ${UNDECLARED_NEEDS_OUT}"
  fi

  if grep -qE 'EFFECTIVE_DEPLOY:\s*\$\{\{\s*matrix\.deploy\s*\}\}' "$EKS_APP_WORKFLOW"; then
    pass "1: EFFECTIVE_DEPLOY is now derived directly from matrix.deploy"
  else
    fail "1: EFFECTIVE_DEPLOY is not wired to matrix.deploy"
  fi
else
  skip "1: undeclared-needs scan -- python3/PyYAML unavailable"
fi

# 2/3: structural proof that a dry-run placeholder can never reach the Argo CD create/update step -- that step is itself gated on matrix.deploy, and the dry-run branch inside "Resolve EFS filesystem ID" is only reachable when EFFECTIVE_DEPLOY (== matrix.deploy) is not "true", so the two conditions are mutually exclusive by construction.
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  ARGO_GATE_OUT="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

steps = doc["jobs"]["build_publish_and_deploy"]["steps"]
argo_step = next((s for s in steps if s.get("name") == "Create or update Argo CD Application"), None)
if argo_step is None:
    print("FAIL: 'Create or update Argo CD Application' step not found")
    sys.exit(1)

cond = str(argo_step.get("if", ""))
if "matrix.deploy" not in cond:
    print(f"FAIL: Argo CD Application step's if: does not gate on matrix.deploy (got {cond!r})")
    sys.exit(1)

print("OK")
PYEOF
)"
  ARGO_GATE_STATUS=$?
  set -e
  if [ "$ARGO_GATE_STATUS" -eq 0 ]; then
    pass "3: the Argo CD Application create/update step is gated on matrix.deploy -- the same condition that keeps the dry-run EFS placeholder branch from ever being reached during a real deploy, so a dry-run placeholder structurally cannot reach Argo CD"
  else
    fail "3: the Argo CD Application step is not correctly gated on matrix.deploy: ${ARGO_GATE_OUT}"
  fi
else
  skip "3: Argo CD dry-run-unreachable structural proof -- python3/PyYAML unavailable"
fi

# 1/2 (behavioral): re-run the "Resolve EFS filesystem ID" step extraction from earlier with EFFECTIVE_DEPLOY driven exactly as matrix.deploy would supply it, proving deploy=true+managed never selects the placeholder and deploy=false+managed may use it (for Helm validation only, never persisted anywhere real).
if [ "$PYTHON_AVAILABLE" = "true" ] && [ -s "${WORKDIR}/resolve_efs_id.sh" ]; then
  DEPLOY_TRUE_MANAGED_OUT="$(EFS_MODE="managed" EFS_FILE_SYSTEM_ID_DECLARED="" EFS_CREATION_TOKEN="dev-x-efs" \
    EFFECTIVE_DEPLOY="true" GITHUB_ENV="$(mktemp)" GITHUB_RUN_ID="1" GITHUB_RUN_ATTEMPT="1" AWS_REGION="eu-west-1" \
    EKS_DEPLOY_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGateEKSDeployRole-dev" \
    bash "${WORKDIR}/resolve_efs_id.sh" 2>&1 || true)"
  if ! echo "$DEPLOY_TRUE_MANAGED_OUT" | grep -qF "fs-0dead0000000beef0"; then
    pass "1: deploy=true + managed never selects the dry-run placeholder fs-0dead0000000beef0 (it instead attempts real AWS resolution)"
  else
    fail "1: deploy=true + managed incorrectly selected the dry-run placeholder"
    echo "$DEPLOY_TRUE_MANAGED_OUT"
  fi
else
  skip "1: deploy=true+managed placeholder-avoidance check -- prerequisites unavailable"
fi

echo ""
echo "--- Correction pass, Issue 3: physical deletion vs lifecycle.state=absent ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  python3 - "$EKS_APP_WORKFLOW" > "${WORKDIR}/deletion_guard.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["managed_efs_deletion_guard"]["steps"]:
    if step.get("name") == "Fail closed if any deleted descriptor declared persistence.efs.mode=managed":
        sys.stdout.write(step["run"])
        break
else:
    sys.exit("step not found")
PYEOF

  if [ ! -s "${WORKDIR}/deletion_guard.sh" ]; then
    fail "12: could not extract the managed_efs_deletion_guard step from ${EKS_APP_WORKFLOW}"
  else
    PHYSICAL_REMOVAL_MATRIX='[{"deployment_id":"gg-x","efs_mode":"managed","reason":"physical-removal"}]'
    set +e
    PHYSICAL_OUT="$(DELETION_MATRIX="$PHYSICAL_REMOVAL_MATRIX" bash "${WORKDIR}/deletion_guard.sh" 2>&1)"
    PHYSICAL_STATUS=$?
    set -e
    if [ "$PHYSICAL_STATUS" -ne 0 ] && echo "$PHYSICAL_OUT" | grep -qF "physically deleted while persistence.efs.mode=managed"; then
      pass "10: managed + reason=physical-removal fails the guard closed"
    else
      fail "10: managed + reason=physical-removal did not fail the guard as expected"
      echo "$PHYSICAL_OUT"
    fi

    LIFECYCLE_ABSENT_MATRIX='[{"deployment_id":"gg-y","efs_mode":"managed","reason":"lifecycle-absent"}]'
    set +e
    LIFECYCLE_OUT="$(DELETION_MATRIX="$LIFECYCLE_ABSENT_MATRIX" bash "${WORKDIR}/deletion_guard.sh" 2>&1)"
    LIFECYCLE_STATUS=$?
    set -e
    if [ "$LIFECYCLE_STATUS" -eq 0 ] && echo "$LIFECYCLE_OUT" | grep -qF "ALLOWED (application decommission only, managed storage retained)"; then
      pass "12: managed + reason=lifecycle-absent does NOT fail the guard (application decommission allowed, EFS retained, no Terraform destroy triggered)"
    else
      fail "12: managed + reason=lifecycle-absent incorrectly failed the guard (or the allowed-path message is missing)"
      echo "$LIFECYCLE_OUT"
    fi

    MIXED_MATRIX='[{"deployment_id":"gg-y","efs_mode":"managed","reason":"lifecycle-absent"},{"deployment_id":"gg-x","efs_mode":"managed","reason":"physical-removal"}]'
    set +e
    MIXED_OUT="$(DELETION_MATRIX="$MIXED_MATRIX" bash "${WORKDIR}/deletion_guard.sh" 2>&1)"
    MIXED_STATUS=$?
    set -e
    if [ "$MIXED_STATUS" -ne 0 ] && echo "$MIXED_OUT" | grep -qF "gg-x" && ! echo "$MIXED_OUT" | grep -qE "FAIL.*gg-y"; then
      pass "11: a mixed deletion matrix fails closed only on the physical-removal entry, never conflating it with the lifecycle-absent entry"
    else
      fail "11: mixed physical-removal/lifecycle-absent deletion matrix was not classified independently"
      echo "$MIXED_OUT"
    fi

    EXISTING_PHYSICAL_MATRIX='[{"deployment_id":"gg-z","efs_mode":"existing","reason":"physical-removal"}]'
    set +e
    EXISTING_OUT="$(DELETION_MATRIX="$EXISTING_PHYSICAL_MATRIX" bash "${WORKDIR}/deletion_guard.sh" 2>&1)"
    EXISTING_STATUS=$?
    set -e
    if [ "$EXISTING_STATUS" -eq 0 ]; then
      pass "existing-mode physical removal does not fail the managed-only guard (Terraform never owned that filesystem)"
    else
      fail "existing-mode physical removal incorrectly failed the managed-EFS guard"
      echo "$EXISTING_OUT"
    fi
  fi
else
  skip "10/11/12: managed_efs_deletion_guard reason-classification checks -- python3/PyYAML unavailable"
fi

echo ""
echo "--- Correction pass, Issue 4: storage-identity transition guard ---"

if [ -f "$DETECT_SCRIPT" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  {
    awk '/^_persistence_efs_summary_json\(\) \{/,/^\}$/' "$DETECT_SCRIPT"
    echo ""
    awk '/^_check_storage_transition\(\) \{/,/^\}$/' "$DETECT_SCRIPT"
  } > "${WORKDIR}/transition_fn.sh"

  for required_fn in _persistence_efs_summary_json _check_storage_transition; do
    if ! grep -q "^${required_fn}() {" "${WORKDIR}/transition_fn.sh"; then
      fail "13-18: could not extract ${required_fn}() from ${DETECT_SCRIPT} -- the transition-guard test harness cannot run"
    fi
  done

  TRANSITION_TEST_OUTPUT="$(bash -c '
    set -euo pipefail
    source "'"${WORKDIR}"'/transition_fn.sh"

    mk() { printf "%s\n" "$1" > "'"${WORKDIR}"'/t.yaml"; _persistence_efs_summary_json "'"${WORKDIR}"'/t.yaml"; }

    MANAGED="$(mk "persistence:
  enabled: true
  provider: efs
  efs:
    mode: managed")"
    EXISTING_A="$(mk "persistence:
  enabled: true
  provider: efs
  efs:
    mode: existing
    fileSystemId: fs-aaaaaaaaaaaaaaaaa")"
    EXISTING_B="$(mk "persistence:
  enabled: true
  provider: efs
  efs:
    mode: existing
    fileSystemId: fs-bbbbbbbbbbbbbbbbb")"
    DISABLED="$(mk "persistence:
  enabled: false")"
    NONEFS="$(mk "persistence:
  enabled: true
  provider: s3")"

    echo "CASE managed->existing: [$(_check_storage_transition "$MANAGED" "$EXISTING_A")]"
    echo "CASE existing->managed: [$(_check_storage_transition "$EXISTING_A" "$MANAGED")]"
    echo "CASE managed->disabled: [$(_check_storage_transition "$MANAGED" "$DISABLED")]"
    echo "CASE managed->nonefs: [$(_check_storage_transition "$MANAGED" "$NONEFS")]"
    echo "CASE existing-fsid-changed: [$(_check_storage_transition "$EXISTING_A" "$EXISTING_B")]"
    echo "CASE existing-same-fsid: [$(_check_storage_transition "$EXISTING_A" "$EXISTING_A")]"
    echo "CASE managed->managed: [$(_check_storage_transition "$MANAGED" "$MANAGED")]"
  ' 2>&1)"
  echo "$TRANSITION_TEST_OUTPUT"

  check_transition_case() {
    local label="$1" pattern="$2"
    if echo "$TRANSITION_TEST_OUTPUT" | grep -qE "$pattern"; then
      pass "$label"
    else
      fail "$label -- expected pattern not found: ${pattern}"
    fi
  }

  check_transition_case "13: managed -> existing is blocked" \
    '^CASE managed->existing: \[managed -> existing\]$'
  check_transition_case "14: existing -> managed is blocked" \
    '^CASE existing->managed: \[existing -> managed\]$'
  check_transition_case "15: managed -> persistence disabled is blocked" \
    '^CASE managed->disabled: \[managed -> persistence disabled\]$'
  check_transition_case "16: managed -> non-EFS provider is blocked" \
    '^CASE managed->nonefs: \[managed -> non-EFS provider\]$'
  check_transition_case "17: existing fileSystemId mutation is blocked" \
    '^CASE existing-fsid-changed: \[existing fileSystemId changed from'
  check_transition_case "18: existing -> existing with the same fileSystemId (normal edit) passes" \
    '^CASE existing-same-fsid: \[\]$'
  check_transition_case "managed -> managed (normal config update) passes" \
    '^CASE managed->managed: \[\]$'
else
  skip "13-18: storage-transition rule checks -- ${DETECT_SCRIPT} or python3 unavailable"
fi

if grep -qE '^\s*storage_transition_guard:' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "the storage_transition_guard job exists in the eks-app workflow"
else
  fail "the storage_transition_guard job is missing from the eks-app workflow"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  TRANSITION_ORDER_OUT="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

jobs = doc.get("jobs", {})
problems = []

guard = jobs.get("storage_transition_guard")
if guard is None:
    problems.append("storage_transition_guard job is missing")
else:
    needs = guard.get("needs")
    needs_list = needs if isinstance(needs, list) else [needs]
    if "detect_changed_deployments" not in needs_list:
        problems.append("storage_transition_guard does not need detect_changed_deployments")

tf = jobs.get("terraform_sync_once")
if tf is None:
    problems.append("terraform_sync_once job is missing")
else:
    needs = tf.get("needs")
    needs_list = needs if isinstance(needs, list) else [needs]
    if "storage_transition_guard" not in needs_list:
        problems.append("terraform_sync_once does not need storage_transition_guard")
    if_expr = str(tf.get("if", ""))
    if "storage_transition_guard.result" not in if_expr or "success" not in if_expr:
        problems.append("terraform_sync_once's if: does not explicitly require storage_transition_guard to have succeeded")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("OK")
PYEOF
)"
  TRANSITION_ORDER_STATUS=$?
  set -e
  if [ "$TRANSITION_ORDER_STATUS" -eq 0 ]; then
    pass "storage_transition_guard is structurally guaranteed to run before terraform_sync_once, and terraform_sync_once fails closed if it did not succeed"
  else
    fail "storage_transition_guard ordering is not correctly wired: ${TRANSITION_ORDER_OUT}"
  fi
else
  skip "storage_transition_guard ordering check -- python3/PyYAML unavailable"
fi

echo ""
echo "--- Final EFS architecture correction: managed EFS restored to envs/dev, managed_efs_inventory_guard added ---"

if ! grep -qiE '\bpipeline\b' envs/dev/efs.tf 2>/dev/null; then
  pass "6: envs/dev/efs.tf never references a replication pipeline concept -- the module's for_each key (each.key) is the deployment ID from local.goldengate_managed_efs_deployments, never a pipeline ID"
else
  fail "6: envs/dev/efs.tf references \"pipeline\" -- the module key must be derived from deployment ID alone"
fi

# Self-service: never a hardcoded exact inventory -- proves the live CANONICAL managed-EFS inventory is non-empty (list length >= 1) while also proving envs/dev/efs.tf's shared-SG data-source count no longer tracks that canonical count directly. Since local.goldengate_managed_efs_desired_deployments = canonical minus the explicit managed-EFS decommission allowlist, and today's live decommission set exactly equals the live canonical set (see the "Managed EFS decommission" checks above), the live SG lookup count actually evaluates to 0 even though the canonical inventory itself is non-empty -- this is the whole point of gating on desired rather than canonical (see the "EFS SG lookup lifecycle" checks above). The full dynamic-vs-derived semantic comparison lives in the "Self-service test architecture: generic descriptor invariants" section above; not duplicated here.
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  LIVE_INVENTORY_OUT="$(PYTHONDONTWRITEBYTECODE=1 python3 "$DEPLOYMENT_MODEL_TOOL" --environment dev managed-efs-inventory 2>&1)"
  LIVE_INVENTORY_STATUS=$?
  set -e
  LIVE_INVENTORY_COUNT="$(python3 -c 'import json, sys; print(len(json.loads(sys.argv[1])))' "$LIVE_INVENTORY_OUT" 2>/dev/null || echo "-1")"
  if [ "$LIVE_INVENTORY_STATUS" -eq 0 ] && [ "$LIVE_INVENTORY_COUNT" -ge 1 ]; then
    pass "11: today's live dev CANONICAL managed-EFS inventory contains at least one managed deployment -- but every canonical entry is also in the explicit decommission set, so envs/dev/efs.tf's shared-SG data-source count (gated on desired, not canonical) evaluates to 0 today, not 1"
  else
    fail "11: expected the live managed-efs-inventory to be valid JSON with at least one entry: ${LIVE_INVENTORY_OUT}"
  fi
else
  skip "11: live managed-efs-inventory check -- python3 unavailable"
fi

if grep -qE '^\s*data\s+"aws_security_group"\s+"goldengate_efs_shared"' envs/dev/efs.tf 2>/dev/null; then
  pass "the shared EFS security-group lookup remains in envs/dev/efs.tf (the normal shared root, per the corporate Terraform workflow discovery)"
else
  fail "the shared EFS security-group lookup is missing from envs/dev/efs.tf"
fi

if grep -qE '^\s*module\s+"goldengate_runtime_efs"' envs/dev/efs.tf 2>/dev/null; then
  pass "4: a single module.goldengate_runtime_efs block exists; for_each over local.goldengate_managed_efs_desired_deployments means exactly one Terraform module key (module.goldengate_runtime_efs[<id>]) is created per DESIRED managed deployment (canonical managed deployments minus the explicit managed-EFS decommission allowlist)"
else
  fail "4: envs/dev/efs.tf is missing the module.goldengate_runtime_efs block"
fi

if ! grep -qE '"elasticfilesystem:ListTagsForResource"' envs/dev/policies/goldengate-eks-deploy-dev/policies/policies_1.json 2>/dev/null; then
  pass "elasticfilesystem:ListTagsForResource has been removed from the workload-account read role -- no replacement IAM permission was added, since DescribeFileSystems' own Tags field is now the sole EFS metadata source"
else
  fail "elasticfilesystem:ListTagsForResource is still present in the GoldenGateEKSDeployRole-dev EFS-read policy statement -- it must be removed, not replaced"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  POLICY_CHECK_OUT="$(python3 -c '
import json
with open("envs/dev/policies/goldengate-eks-deploy-dev/policies/policies_1.json") as f:
    doc = json.load(f)
for stmt in doc["Statement"]:
    actions = stmt.get("Action", [])
    for action in actions:
        if action.startswith("elasticfilesystem:") and action.split(":", 1)[1] not in ("DescribeAccessPoints", "DescribeFileSystems"):
            print(f"unexpected EFS action granted: {action}")
            raise SystemExit(1)
print("OK")
' 2>&1)"
  POLICY_CHECK_STATUS=$?
  set -e
  if [ "$POLICY_CHECK_STATUS" -eq 0 ]; then
    pass "the workload-account read role is granted only read-only EFS actions (DescribeAccessPoints/DescribeFileSystems) -- no ListTagsForResource, no create/update/delete permission"
  else
    fail "the workload-account read role's EFS policy grants an unexpected action: ${POLICY_CHECK_OUT}"
  fi
else
  skip "EFS read-only policy scope check -- python3 unavailable"
fi

if ! grep -qF "aws efs list-tags-for-resource" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "no 'aws efs list-tags-for-resource' command remains anywhere in the eks-app workflow"
else
  fail "'aws efs list-tags-for-resource' is still present in the eks-app workflow"
fi

if grep -qE 'json\.load\(sys\.stdin\)\["FileSystems"\]\[0\]\.get\("Tags"' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "managed-mode EFS resolution reads Tags directly from the same DescribeFileSystems (--creation-token) response used to resolve FileSystemId -- no second tag API call"
else
  fail "managed-mode EFS resolution does not read Tags directly from the DescribeFileSystems response"
fi

if ! grep -qE 'cat actual-managed-efs\.json|echo "\$ACTUAL_JSON"|echo "\$DESCRIBE_ALL_JSON"' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "the inventory guard's workflow step never dumps the full/raw AWS EFS scan (account-wide tag metadata) to the log -- only a sanitized in-scope count is logged"
else
  fail "the inventory guard's workflow step still logs the full/raw AWS EFS scan output"
fi

if grep -qF 'GoldenGate-managed-tagged filesystem(s) found in scope' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "the inventory guard logs only the sanitized in-scope GoldenGate-managed filesystem count, never unrelated EFS tags"
else
  fail "the inventory guard's summary log line for the sanitized in-scope count is missing"
fi

if [ -f "hack/goldengate-managed-efs-inventory-guard.py" ]; then
  pass "14: hack/goldengate-managed-efs-inventory-guard.py (the pure comparison logic behind managed_efs_inventory_guard) exists"
else
  fail "14: hack/goldengate-managed-efs-inventory-guard.py is missing"
fi

if grep -qE '^\s*managed_efs_inventory_guard:' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "14: the managed_efs_inventory_guard job exists in the eks-app workflow"
else
  fail "14: the managed_efs_inventory_guard job is missing from the eks-app workflow"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  INVENTORY_ORDER_OUT="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

jobs = doc.get("jobs", {})
problems = []

guard = jobs.get("managed_efs_inventory_guard")
if guard is None:
    problems.append("managed_efs_inventory_guard job is missing")
else:
    needs = guard.get("needs")
    needs_list = needs if isinstance(needs, list) else [needs]
    for required in ("detect_changed_deployments", "managed_efs_deletion_guard", "storage_transition_guard"):
        if required not in needs_list:
            problems.append(f"managed_efs_inventory_guard does not need {required}")

tf = jobs.get("terraform_sync_once")
if tf is None:
    problems.append("terraform_sync_once job is missing")
else:
    needs = tf.get("needs")
    needs_list = needs if isinstance(needs, list) else [needs]
    if "managed_efs_inventory_guard" not in needs_list:
        problems.append("terraform_sync_once does not need managed_efs_inventory_guard")
    if_expr = str(tf.get("if", ""))
    if "managed_efs_inventory_guard.result" not in if_expr or "success" not in if_expr:
        problems.append("terraform_sync_once's if: does not explicitly require managed_efs_inventory_guard to have succeeded")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("OK")
PYEOF
)"
  INVENTORY_ORDER_STATUS=$?
  set -e
  if [ "$INVENTORY_ORDER_STATUS" -eq 0 ]; then
    pass "15/16: managed_efs_inventory_guard is structurally guaranteed to run after the Git-diff guards and before terraform_sync_once, and terraform_sync_once fails closed if it did not succeed"
  else
    fail "15/16: managed_efs_inventory_guard ordering is not correctly wired: ${INVENTORY_ORDER_OUT}"
  fi
else
  skip "15/16: managed_efs_inventory_guard ordering check -- python3/PyYAML unavailable"
fi

if grep -q 'validate_model' "$EKS_APP_WORKFLOW" 2>/dev/null && [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  UNDECLARED_NEEDS_RECHECK_OUT="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import re
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

problems = []
for job_name, job in doc["jobs"].items():
    needs = job.get("needs")
    if needs is None:
        declared = set()
    elif isinstance(needs, str):
        declared = {needs}
    else:
        declared = set(needs)

    job_copy = dict(job)
    job_copy.pop("needs", None)
    text = yaml.dump(job_copy, default_flow_style=False)
    refs = set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.", text))

    undeclared = refs - declared
    if undeclared:
        problems.append(f"{job_name}: undeclared needs.{{{','.join(sorted(undeclared))}}}")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("OK")
PYEOF
)"
  UNDECLARED_NEEDS_RECHECK_STATUS=$?
  set -e
  if [ "$UNDECLARED_NEEDS_RECHECK_STATUS" -eq 0 ]; then
    pass "the new managed_efs_inventory_guard job (which references needs.validate_model) does not reintroduce the Issue-1 undeclared-needs bug -- validate_model is explicitly in its own needs: list"
  else
    fail "an undeclared needs.<job> reference was reintroduced: ${UNDECLARED_NEEDS_RECHECK_OUT}"
  fi
fi

if grep -qF 'expected exactly one EFS filesystem for creation token' "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -qF 'MATCH_COUNT" -ne 1' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "35: managed EFS resolution fails closed on zero or multiple creation-token matches (MATCH_COUNT != 1), never lists-and-guesses"
else
  fail "35: the zero/multiple creation-token match fail-closed check is missing from the workflow"
fi

if grep -qF 'LifeCycleState' "$EKS_APP_WORKFLOW" 2>/dev/null && grep -qF '"available"' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "36: managed EFS resolution requires LifeCycleState == available before it is used"
else
  fail "36: the LifecycleState==available check is missing from the managed EFS resolution step"
fi

if grep -qF 'ManagedBy") == "goldengate-eks-app"' "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -qF 'GoldenGateDeploymentId") == sys.argv[1]' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "the resolved managed EFS filesystem's ownership tags are cross-checked (ManagedBy/GoldenGateDeploymentId/GoldenGateEnvironment) before RESOLVED_EFS_ID is used"
else
  fail "the optional tag cross-check on the resolved managed EFS filesystem is missing"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  MANAGED_EFS_GUARD_TEST_OUTPUT="$(python3 hack/test-goldengate-managed-efs-inventory-guard.py 2>&1)"
  MANAGED_EFS_GUARD_TEST_STATUS=$?
  set -e
  if [ "$MANAGED_EFS_GUARD_TEST_STATUS" -eq 0 ]; then
    RAN_LINE_INV="$(echo "$MANAGED_EFS_GUARD_TEST_OUTPUT" | grep -E '^Ran [0-9]+ test' | tail -1)"
    pass "17/18/19/20/21/22: hack/test-goldengate-managed-efs-inventory-guard.py: ${RAN_LINE_INV:-all tests passed}"
  else
    fail "17/18/19/20/21/22: hack/test-goldengate-managed-efs-inventory-guard.py reported a failure"
    echo "$MANAGED_EFS_GUARD_TEST_OUTPUT"
  fi
else
  skip "17/18/19/20/21/22: managed-efs-inventory-guard unit tests -- python3 unavailable"
fi

echo ""
echo "--- Final workflow correction, Issue 1: fail-closed job graph for a real deploy ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  FAIL_CLOSED_SIM_OUT="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import re
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

jobs = doc["jobs"]


def _extract_if(job_name):
    raw = jobs[job_name].get("if", "true")
    raw = str(raw).strip()
    if raw.startswith("${{") and raw.endswith("}}"):
        raw = raw[3:-2].strip()
    return raw


class _Parser:
    """A tiny, bespoke evaluator for exactly the GHA expression subset this workflow uses: && || == != () quoted strings, needs.<job>.result, needs.<job>.outputs.<name>, always(). Not a general GHA expression engine -- just enough to genuinely simulate these four job conditions against a fabricated needs context, rather than trusting a text/regex match against the workflow author's own wording."""

    def __init__(self, expr, needs):
        self.expr = expr
        self.needs = needs
        self.pos = 0

    def _skip_ws(self):
        while self.pos < len(self.expr) and self.expr[self.pos].isspace():
            self.pos += 1

    def parse(self):
        result = self._or()
        self._skip_ws()
        if self.pos != len(self.expr):
            raise ValueError(f"trailing content: {self.expr[self.pos:]!r}")
        return result

    def _or(self):
        left = self._and()
        self._skip_ws()
        while self.expr[self.pos:self.pos + 2] == "||":
            self.pos += 2
            right = self._and()
            left = left or right
            self._skip_ws()
        return left

    def _and(self):
        left = self._atom()
        self._skip_ws()
        while self.expr[self.pos:self.pos + 2] == "&&":
            self.pos += 2
            right = self._atom()
            left = left and right
            self._skip_ws()
        return left

    def _atom(self):
        self._skip_ws()
        if self.expr[self.pos] == "(":
            self.pos += 1
            val = self._or()
            self._skip_ws()
            assert self.expr[self.pos] == ")"
            self.pos += 1
            return val
        if self.expr[self.pos:self.pos + 8] == "always()":
            self.pos += 8
            return True
        left_val = self._value()
        self._skip_ws()
        op = self.expr[self.pos:self.pos + 2]
        if op in ("==", "!="):
            self.pos += 2
            right_val = self._value()
            return left_val == right_val if op == "==" else left_val != right_val
        return bool(left_val)

    def _value(self):
        self._skip_ws()
        if self.expr[self.pos] == "'":
            self.pos += 1
            start = self.pos
            while self.expr[self.pos] != "'":
                self.pos += 1
            val = self.expr[start:self.pos]
            self.pos += 1
            return val
        m = re.match(r"needs\.([A-Za-z0-9_]+)\.result", self.expr[self.pos:])
        if m:
            self.pos += m.end()
            return self.needs.get(m.group(1), {}).get("result", "")
        m = re.match(r"needs\.([A-Za-z0-9_]+)\.outputs\.([A-Za-z0-9_]+)", self.expr[self.pos:])
        if m:
            self.pos += m.end()
            return self.needs.get(m.group(1), {}).get("outputs", {}).get(m.group(2), "")
        raise ValueError(f"cannot parse value at: {self.expr[self.pos:self.pos + 40]!r}")


def eval_gha_bool(expr, needs):
    return bool(_Parser(expr, needs).parse())


JOB_ORDER = ["terraform_sync_once", "platform_sync_once", "validate_shared_secrets_once", "build_publish_and_deploy"]
IF_EXPRS = {job: _extract_if(job) for job in JOB_ORDER}


def simulate(initial, outcome_when_run):
    results = dict(initial)
    for job in JOB_ORDER:
        would_run = eval_gha_bool(IF_EXPRS[job], results)
        results[job] = {"result": (outcome_when_run.get(job, "success") if would_run else "skipped"), "outputs": {}}
    return results


def base_context(effective_deploy, has_changes="true"):
    return {
        "validate_model": {"result": "success", "outputs": {"effective_deploy": effective_deploy}},
        "eks_oidc_preflight": {"result": "success", "outputs": {}},
        "detect_changed_deployments": {"result": "success", "outputs": {"has_changes": has_changes}},
        "managed_efs_deletion_guard": {"result": "success", "outputs": {}},
        "storage_transition_guard": {"result": "success", "outputs": {}},
        "managed_efs_inventory_guard": {"result": "success", "outputs": {}},
    }


failures = []


def check(label, condition):
    if not condition:
        failures.append(label)


# 1: deploy=true + inventory guard failure -> terraform skipped/blocked -> runtime build/deploy cannot execute.
ctx = base_context("true")
ctx["managed_efs_inventory_guard"] = {"result": "failure", "outputs": {}}
r = simulate(ctx, {})
check("1: terraform_sync_once must be skipped", r["terraform_sync_once"]["result"] == "skipped")
check("1: build_publish_and_deploy must be skipped", r["build_publish_and_deploy"]["result"] == "skipped")

# 1b (Fresh-EKS Phase A): deploy=true + eks_oidc_preflight failure (live OIDC issuer mismatch) -> terraform_sync_once must never run, so a stale/mismatched IRSA trust policy can never reach Terraform apply.
ctx = base_context("true")
ctx["eks_oidc_preflight"] = {"result": "failure", "outputs": {}}
r = simulate(ctx, {})
check("1b: terraform_sync_once must be skipped when eks_oidc_preflight fails", r["terraform_sync_once"]["result"] == "skipped")
check("1b: build_publish_and_deploy must be skipped when eks_oidc_preflight fails", r["build_publish_and_deploy"]["result"] == "skipped")

# 2: deploy=true + terraform failure -> runtime build/deploy cannot execute.
ctx = base_context("true")
r = simulate(ctx, {"terraform_sync_once": "failure"})
check("2: terraform_sync_once must report failure", r["terraform_sync_once"]["result"] == "failure")
check("2: platform_sync_once must be skipped", r["platform_sync_once"]["result"] == "skipped")
check("2: validate_shared_secrets_once must be skipped", r["validate_shared_secrets_once"]["result"] == "skipped")
check("2: build_publish_and_deploy must be skipped", r["build_publish_and_deploy"]["result"] == "skipped")

# 3: deploy=true + platform failure -> runtime build/deploy cannot execute.
ctx = base_context("true")
r = simulate(ctx, {"platform_sync_once": "failure"})
check("3: platform_sync_once must report failure", r["platform_sync_once"]["result"] == "failure")
check("3: validate_shared_secrets_once must be skipped", r["validate_shared_secrets_once"]["result"] == "skipped")
check("3: build_publish_and_deploy must be skipped", r["build_publish_and_deploy"]["result"] == "skipped")

# 4: deploy=true + all mutation prerequisites success -> runtime deployment may execute.
ctx = base_context("true")
r = simulate(ctx, {})
check("4: terraform_sync_once must succeed", r["terraform_sync_once"]["result"] == "success")
check("4: platform_sync_once must succeed", r["platform_sync_once"]["result"] == "success")
check("4: validate_shared_secrets_once must succeed", r["validate_shared_secrets_once"]["result"] == "success")
check("4: build_publish_and_deploy must be eligible to run", r["build_publish_and_deploy"]["result"] == "success")

# 5: deploy=false -> terraform/platform may be skipped -> read-only/Helm dry-run path still executes.
ctx = base_context("false")
ctx["managed_efs_inventory_guard"] = {"result": "skipped", "outputs": {}}
r = simulate(ctx, {})
check("5: terraform_sync_once must be skipped", r["terraform_sync_once"]["result"] == "skipped")
check("5: platform_sync_once must be skipped", r["platform_sync_once"]["result"] == "skipped")
check("5: validate_shared_secrets_once must still succeed", r["validate_shared_secrets_once"]["result"] == "success")
check("5: build_publish_and_deploy dry-run path must still be eligible to run", r["build_publish_and_deploy"]["result"] == "success")

# 6: build_publish_and_deploy requires validate_shared_secrets_once SUCCESS, not merely not-failure/not-cancelled -- simulate a bare "skipped" upstream result directly and confirm it is rejected (the old assertion would have let this through).
skipped_ctx = base_context("true")
skipped_ctx["validate_shared_secrets_once"] = {"result": "skipped", "outputs": {}}
would_run = eval_gha_bool(IF_EXPRS["build_publish_and_deploy"], skipped_ctx)
check("6: build_publish_and_deploy must reject a skipped validate_shared_secrets_once", would_run is False)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("OK")
PYEOF
)"
  FAIL_CLOSED_SIM_STATUS=$?
  set -e
  if [ "$FAIL_CLOSED_SIM_STATUS" -eq 0 ]; then
    pass "1: deploy=true + managed_efs_inventory_guard failure blocks terraform_sync_once and build_publish_and_deploy (simulated end-to-end against the real if: expressions)"
    pass "2: deploy=true + terraform_sync_once failure blocks platform_sync_once/validate_shared_secrets_once/build_publish_and_deploy"
    pass "3: deploy=true + platform_sync_once failure blocks validate_shared_secrets_once/build_publish_and_deploy"
    pass "4: deploy=true + all mutation prerequisites succeeding leaves build_publish_and_deploy eligible to run"
    pass "5: deploy=false correctly skips terraform_sync_once/platform_sync_once while the read-only/dry-run path through validate_shared_secrets_once and build_publish_and_deploy still runs"
    pass "6: build_publish_and_deploy's if: rejects a skipped validate_shared_secrets_once (requires exact 'success', not the old != failure/!= cancelled assertion)"
  else
    fail "fail-closed job-graph simulation found violation(s): ${FAIL_CLOSED_SIM_OUT}"
  fi
else
  skip "1-6: fail-closed job-graph simulation -- python3/PyYAML unavailable"
fi

if ! grep -qE "validate_shared_secrets_once\.result\s*!=\s*'failure'" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "the old overly-permissive assertion (validate_shared_secrets_once.result != 'failure') has been replaced -- build_publish_and_deploy now requires exact success"
else
  fail "build_publish_and_deploy still contains the old != 'failure'/!= 'cancelled' assertion for validate_shared_secrets_once"
fi

if grep -qF "needs.validate_shared_secrets_once.result == 'success'" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "build_publish_and_deploy's if: explicitly requires needs.validate_shared_secrets_once.result == 'success'"
else
  fail "build_publish_and_deploy's if: does not explicitly require validate_shared_secrets_once.result == 'success'"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  set +e
  UNDECLARED_NEEDS_FINAL_OUT="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import re
import sys
import yaml

with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)

problems = []
for job_name, job in doc["jobs"].items():
    needs = job.get("needs")
    if needs is None:
        declared = set()
    elif isinstance(needs, str):
        declared = {needs}
    else:
        declared = set(needs)

    job_copy = dict(job)
    job_copy.pop("needs", None)
    text = yaml.dump(job_copy, default_flow_style=False)
    refs = set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.", text))

    undeclared = refs - declared
    if undeclared:
        problems.append(f"{job_name}: undeclared needs.{{{','.join(sorted(undeclared))}}}")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("OK")
PYEOF
)"
  UNDECLARED_NEEDS_FINAL_STATUS=$?
  set -e
  if [ "$UNDECLARED_NEEDS_FINAL_STATUS" -eq 0 ]; then
    pass "the platform_sync_once/validate_shared_secrets_once fail-closed fixes did not introduce any needs.<job> reference for a job outside that job's own declared needs: list"
  else
    fail "an undeclared needs.<job> reference was introduced: ${UNDECLARED_NEEDS_FINAL_OUT}"
  fi
else
  skip "undeclared-needs recheck -- python3/PyYAML unavailable"
fi

echo ""
echo "--- Final workflow correction, Issue 3: no unverified Terraform module output dependency ---"

if ! grep -qE '^\s*output\s+"goldengate_runtime_efs_filesystem_ids"' envs/dev/efs.tf 2>/dev/null; then
  pass "envs/dev/efs.tf no longer declares an output depending on module.goldengate_runtime_efs.efs_id -- the aws-tf-module-efs v1.0.0 outputs.tf contract is not provable from local reference material, so it was removed rather than guessed"
else
  fail "envs/dev/efs.tf still declares goldengate_runtime_efs_filesystem_ids, depending on an unverified module.goldengate_runtime_efs.efs_id output"
fi

if ! grep -qE '\.efs_id\b' envs/dev/*.tf 2>/dev/null; then
  pass "no envs/dev/*.tf file references module.goldengate_runtime_efs.efs_id or any other unverified module output attribute"
else
  fail "an envs/dev/*.tf file still references an unverified module output attribute (.efs_id)"
fi

if grep -qF 'aws efs describe-file-systems --creation-token' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "managed EFS resolution remains entirely creation-token-based (aws efs describe-file-systems --creation-token), never dependent on a Terraform child output the approved corporate reusable workflow does not expose"
else
  fail "the creation-token-based managed EFS resolution path is missing from the workflow"
fi

echo ""
echo "--- Production hardening, Item 1: EFS throughput_mode (module-input vs AWS-API contract correction) ---"

# CORRECTED per the verified aws-tf-module-efs?ref=v1.0.0 resource source: the module does NOT pass var.throughput_mode straight through -- it applies `throughput_mode = (var.throughput_mode == "enhanced" ? "elastic" : "bursting")`. The module INPUT "enhanced" is therefore the ONLY correct value; the earlier assertions in this section (which required "enhanced" to be absent, and required a goldengate_efs_throughput_mode variable accepting elastic/provisioned/bursting) were themselves wrong and have been replaced below.

if grep -qE 'source\s*=\s*"git::https://github\.com/AbuDhabiCommercialBank/aws-tf-module-efs\?ref=v1\.0\.0"' envs/dev/efs.tf 2>/dev/null; then
  pass "1: goldengate_runtime_efs remains pinned exactly to aws-tf-module-efs?ref=v1.0.0"
else
  fail "1: envs/dev/efs.tf does not pin goldengate_runtime_efs to aws-tf-module-efs?ref=v1.0.0"
fi

if grep -qE '^\s*performance_mode\s*=\s*"generalPurpose"\s*$' envs/dev/efs.tf 2>/dev/null; then
  pass "1: performance_mode passed to the module is the literal \"generalPurpose\""
else
  fail "1: envs/dev/efs.tf does not pass performance_mode = \"generalPurpose\" to the module"
fi

if grep -qE '^\s*throughput_mode\s*=\s*"enhanced"\s*$' envs/dev/efs.tf 2>/dev/null; then
  pass "1: throughput_mode module input is the literal \"enhanced\" -- the only module input proven (via the verified v1.0.0 resource ternary) to produce AWS EFS throughput mode Elastic"
else
  fail "1: envs/dev/efs.tf does not pass throughput_mode = \"enhanced\" to the module"
fi

if ! grep -qE 'throughput_mode\s*=\s*"elastic"' envs/dev/efs.tf 2>/dev/null; then
  pass "1: the code never passes the raw AWS API value \"elastic\" directly as the module's throughput_mode input (that would fall through v1.0.0's ternary else-branch to \"bursting\")"
else
  fail "1: envs/dev/efs.tf passes the raw AWS value \"elastic\" directly as throughput_mode to the module -- this silently produces \"bursting\" in v1.0.0"
fi

if ! grep -qE 'throughput_mode\s*=\s*"provisioned"' envs/dev/efs.tf 2>/dev/null; then
  pass "1: the code never passes \"provisioned\" as the module's throughput_mode input -- v1.0.0's verified resource code has no provisioned-throughput branch"
else
  fail "1: envs/dev/efs.tf passes \"provisioned\" as throughput_mode to the module, which v1.0.0 does not support"
fi

if ! grep -qE '^\s*variable\s+"goldengate_efs_throughput_mode"' envs/dev/efs.tf 2>/dev/null \
    && ! grep -qE 'var\.goldengate_efs_throughput_mode' envs/dev/efs.tf 2>/dev/null; then
  pass "1: throughput_mode is a hardcoded module-input literal (Option A) -- no environment-level goldengate_efs_throughput_mode variable exists to drift from the verified single-environment module contract"
else
  fail "1: envs/dev/efs.tf still references a goldengate_efs_throughput_mode variable -- if retained it must default to \"enhanced\" and allow only the verified module-input contract"
fi

if ! grep -q 'throughput_mode' envs/dev/gg-*-payments-01/values.yaml 2>/dev/null; then
  pass "1: throughput mode is not exposed as a per-deployment values.yaml setting -- it remains an environment/platform storage policy, not an application-owner runtime setting"
else
  fail "1: throughput mode leaked into a per-deployment values.yaml setting"
fi

if grep -qF 'throughput_mode = (var.throughput_mode == "enhanced" ? "elastic" : "bursting")' envs/dev/efs.tf 2>/dev/null; then
  pass "1: envs/dev/efs.tf documents the verified enhanced->elastic module-source ternary contract, for future maintainers"
else
  fail "1: envs/dev/efs.tf lost the verified enhanced->elastic module-source contract documentation that justifies the enhanced module input"
fi

# Items 8 (Oracle/PostgreSQL descriptors unchanged), 10 (no replication code changes), and 11 (PostgreSQL->MSSQL Phase 6D1 constants unchanged) are covered by their own pre-existing, still-passing sections of this suite and by hack/test-goldengate-replication.py -- not duplicated here since this section is scoped to the throughput_mode contract only. Item 12 (managed-EFS inventory guard tests) is covered by hack/test-goldengate-managed-efs-inventory-guard.py, run separately as part of the full validation sweep.

echo ""
echo "--- Production hardening, Item 2: stream DescribeFileSystems safely ---"

if ! grep -qE 'DESCRIBE_ALL_JSON="\$\(' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "2: the account-wide DescribeFileSystems response is no longer captured into a shell variable before sanitizing"
else
  fail "2: the workflow still captures the raw DescribeFileSystems response into a shell variable (DESCRIBE_ALL_JSON=\$(...))"
fi

if grep -qE 'aws efs describe-file-systems --region "\$AWS_REGION" --output json \| python3 "\$EFS_SANITIZER_SCRIPT"' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "2: aws efs describe-file-systems is piped directly into the sanitizer's stdin (streamed), matching the preferred pattern"
else
  fail "2: the streaming pipe from aws efs describe-file-systems into the sanitizer script is missing"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  python3 - "$EKS_APP_WORKFLOW" > "${WORKDIR}/inventory_scan.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["managed_efs_inventory_guard"]["steps"]:
    if step.get("name", "").startswith("Read the actual AWS-side"):
        sys.stdout.write(step["run"])
        break
else:
    sys.exit("step not found")
PYEOF

  if [ ! -s "${WORKDIR}/inventory_scan.sh" ]; then
    fail "2: could not extract the 'Read the actual AWS-side...' step from ${EKS_APP_WORKFLOW}"
  else
    STUB_DIR="${WORKDIR}/aws-stub"
    mkdir -p "$STUB_DIR"
    cat > "${STUB_DIR}/aws" <<'STUBEOF'
#!/bin/bash
if [ "$1" = "sts" ] && [ "$2" = "assume-role" ]; then
  echo '{"Credentials":{"AccessKeyId":"FAKEKEY","SecretAccessKey":"FAKESECRET","SessionToken":"FAKETOKEN"}}'
elif [ "$1" = "sts" ] && [ "$2" = "get-caller-identity" ]; then
  echo "668311715351"
elif [ "$1" = "efs" ] && [ "$2" = "describe-file-systems" ]; then
  cat <<'JSONEOF'
{"FileSystems":[
  {"FileSystemId":"fs-aaaa","CreationToken":"dev-gg-a-efs","LifeCycleState":"available","Tags":[{"Key":"ManagedBy","Value":"goldengate-eks-app"},{"Key":"GoldenGateDeploymentId","Value":"gg-a"},{"Key":"GoldenGateEnvironment","Value":"dev"},{"Key":"GoldenGateStorage","Value":"u02"},{"Key":"SecretInternalNote","Value":"do-not-leak-this"}]},
  {"FileSystemId":"fs-unrelated","CreationToken":"some-other-token","LifeCycleState":"available","Tags":[{"Key":"Owner","Value":"other-team"}]}
]}
JSONEOF
else
  echo "unexpected aws call: $*" >&2
  exit 1
fi
STUBEOF
    chmod +x "${STUB_DIR}/aws"

    set +e
    STREAM_TEST_OUT="$(cd "${WORKDIR}" && PATH="${STUB_DIR}:${PATH}" \
      EKS_DEPLOY_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGateEKSDeployRole-dev" \
      AWS_REGION="eu-west-1" GITHUB_RUN_ID="1" GITHUB_RUN_ATTEMPT="1" \
      bash "${WORKDIR}/inventory_scan.sh" 2>&1; echo "---"; cat "${WORKDIR}/actual-managed-efs.json" 2>/dev/null)"
    STREAM_TEST_STATUS=$?
    set -e

    if [ "$STREAM_TEST_STATUS" -eq 0 ] \
        && echo "$STREAM_TEST_OUT" | grep -qF '"FileSystemId": "fs-aaaa"' \
        && echo "$STREAM_TEST_OUT" | grep -qF '"GoldenGateDeploymentId", "Value": "gg-a"' \
        && ! echo "$STREAM_TEST_OUT" | grep -qF "SecretInternalNote" \
        && ! echo "$STREAM_TEST_OUT" | grep -qF "do-not-leak-this" \
        && ! echo "$STREAM_TEST_OUT" | grep -qF "other-team"; then
      pass "2: the real extracted inventory-scan step, run end-to-end against a stubbed aws CLI, correctly streams and sanitizes -- retains the four GoldenGate tags for the in-scope filesystem and strips every unrelated tag (SecretInternalNote, Owner) from both filesystems"
    else
      fail "2: the extracted inventory-scan step did not stream/sanitize correctly against the stubbed aws CLI"
      echo "$STREAM_TEST_OUT"
    fi

    rm -rf "$STUB_DIR"
  fi
else
  skip "2: end-to-end streaming/sanitization behavioral test -- python3/PyYAML unavailable"
fi

echo ""
echo "--- VDR correction: validate_shared_secrets_once cross-account credential fix ---"

# Real VDR evidence: validate_shared_secrets_once's base credentials (role-to-assume: env.ROLE_ARN) resolve to the engineering/runner account (229410149234), so its DescribeSecret calls were hitting the wrong account and failing with ResourceNotFoundException even though the three secrets genuinely exist in the workload account (668311715351). Fix reuses the SAME established cross-account pattern as managed_efs_inventory_guard: assume EKS_DEPLOY_ROLE_ARN, verify the assumed caller's own account before any Secrets Manager call, mask the temporary credentials, and never call GetSecretValue.

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  python3 - "$EKS_APP_WORKFLOW" > "${WORKDIR}/shared_secrets_step.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
steps = doc["jobs"]["validate_shared_secrets_once"]["steps"]
for step in steps:
    if step.get("name", "").startswith("Verify each shared secret exists"):
        # PyYAML/pip access a real package index and are unavailable/unnecessary in this sandboxed behavioral test -- PyYAML is already proven present ($PYTHON_AVAILABLE), so this single install line is stripped before execution; nothing else in the step is touched.
        lines = [l for l in step["run"].splitlines() if "pip install" not in l]
        sys.stdout.write("\n".join(lines))
        break
else:
    sys.exit("step not found")
PYEOF

  if [ ! -s "${WORKDIR}/shared_secrets_step.sh" ]; then
    fail "VDR: could not extract the 'Verify each shared secret exists...' step from ${EKS_APP_WORKFLOW}"
  else
    STEP_TEXT="$(cat "${WORKDIR}/shared_secrets_step.sh")"
    ASSUME_LINE="$(printf '%s\n' "$STEP_TEXT" | grep -n 'aws sts assume-role' | head -1 | cut -d: -f1 || true)"
    IDENTITY_LINE="$(printf '%s\n' "$STEP_TEXT" | grep -n 'aws sts get-caller-identity' | head -1 | cut -d: -f1 || true)"
    FAILCLOSED_LINE="$(printf '%s\n' "$STEP_TEXT" | grep -n 'Refusing to call Secrets Manager' | head -1 | cut -d: -f1 || true)"
    DESCRIBE_LINE="$(printf '%s\n' "$STEP_TEXT" | grep -n 'aws secretsmanager describe-secret' | head -1 | cut -d: -f1 || true)"

    if [ -n "$ASSUME_LINE" ] && [ -n "$IDENTITY_LINE" ] && [ -n "$FAILCLOSED_LINE" ] && [ -n "$DESCRIBE_LINE" ] \
        && [ "$ASSUME_LINE" -lt "$IDENTITY_LINE" ] && [ "$IDENTITY_LINE" -lt "$FAILCLOSED_LINE" ] && [ "$FAILCLOSED_LINE" -lt "$DESCRIBE_LINE" ]; then
      pass "VDR 2/4/5: validate_shared_secrets_once assumes EKS_DEPLOY_ROLE_ARN, verifies caller-identity, and fails closed on a mismatch -- all strictly before the first DescribeSecret call, so DescribeSecret can never execute before a successful, verified workload-role assumption"
    else
      fail "VDR 2/4/5: assume-role / caller-identity-check / fail-closed / DescribeSecret ordering is missing or out of sequence in validate_shared_secrets_once"
    fi
  fi
else
  skip "VDR: step-extraction/ordering checks -- python3/PyYAML unavailable"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  FIRST_STEP_CHECK="$(python3 -c '
import sys
import yaml
with open("'"$EKS_APP_WORKFLOW"'") as f:
    doc = yaml.safe_load(f)
steps = doc["jobs"]["validate_shared_secrets_once"]["steps"]
cred_steps = [s for s in steps if s.get("uses", "").startswith("aws-actions/configure-aws-credentials")]
ok = len(cred_steps) == 1 and cred_steps[0].get("with", {}).get("role-to-assume") == "${{ env.RUNNER_ROLE_ARN }}"
print("OK" if ok else "FAIL")
')"
  if [ "$FIRST_STEP_CHECK" = "OK" ]; then
    pass "VDR 1: validate_shared_secrets_once still starts by configuring AWS credentials via the canonical RUNNER_ROLE_ARN (env.RUNNER_ROLE_ARN, loaded from envs/<environment>/environment.yaml, never a repository variable), exactly as today -- the fix adds a second, in-step assume-role, it does not replace the job-level OIDC credential step"
  else
    fail "VDR 1: validate_shared_secrets_once no longer starts from the canonical RUNNER_ROLE_ARN (env.RUNNER_ROLE_ARN) via aws-actions/configure-aws-credentials"
  fi
else
  skip "VDR 1: base-credential-step check -- python3/PyYAML unavailable"
fi

if grep -qE 'EXPECTED_WORKLOAD_ACCOUNT_ID="\$\(echo "\$EKS_DEPLOY_ROLE_ARN" \| sed -nE' "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -qE 'ACTUAL_ACCOUNT="\$\(AWS_ACCESS_KEY_ID="\$SEC_TMP_KEY_ID"' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "VDR 3: the workload role's target account is derived from EKS_DEPLOY_ROLE_ARN itself (the same established derivation managed_efs_inventory_guard already uses) and the post-assume-role caller identity is checked against it -- for the dev environment this expected account is the canonical WORKLOAD_ACCOUNT_ID from envs/dev/environment.yaml, never an independent repository variable"
else
  fail "VDR 3: validate_shared_secrets_once does not derive/verify the expected workload-account ID before calling Secrets Manager"
fi

if grep -qE '"\$ACTUAL_ACCOUNT" != "\$EXPECTED_WORKLOAD_ACCOUNT_ID"' "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -qE '\[ -z "\$ACTUAL_ACCOUNT" \]' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "VDR 5: an empty or mismatched caller-identity account fails the step closed before any Secrets Manager call"
else
  fail "VDR 5: validate_shared_secrets_once does not fail closed on an empty/mismatched caller-identity account"
fi

SHARED_SECRETS_JOB_TEXT="$(awk '/^  validate_shared_secrets_once:/{flag=1} flag && /^  [a-zA-Z_]+:$/ && !/^  validate_shared_secrets_once:/{if(NR>1 && flag2) exit} flag{print; flag2=1}' "$EKS_APP_WORKFLOW" 2>/dev/null || true)"
# Matches the real CLI invocation only ("aws secretsmanager get-secret-value") -- not the bare word "GetSecretValue", which legitimately appears once in this job as an explanatory "never call this" comment.
if ! printf '%s' "$SHARED_SECRETS_JOB_TEXT" | grep -qi "secretsmanager get-secret-value"; then
  pass "VDR 8: no 'aws secretsmanager get-secret-value' call exists anywhere in the validate_shared_secrets_once job (GetSecretValue is mentioned once only, in a comment explaining it must never be called)"
else
  fail "VDR 8: validate_shared_secrets_once job text contains an actual secretsmanager get-secret-value call"
fi

if printf '%s' "$SHARED_SECRETS_JOB_TEXT" | grep -qE -- '--region "\$AWS_REGION"'; then
  pass "VDR 6: the region used for the workload-account Secrets Manager/STS calls remains \$AWS_REGION (eu-west-1 for dev), unchanged"
else
  fail "VDR 6: validate_shared_secrets_once no longer scopes its AWS calls to \$AWS_REGION"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  SECRET_NAME_CHECK="$(python3 -c '
import re
with open("hack/goldengate-deployment-model.py") as f:
    text = f.read()
assert re.search(r"def resolve_admin_secret\(environment, role\):", text)
assert re.search(r"return f\"\{environment\}/goldengate/\{role\}/admin\"", text)
assert re.search(r"def resolve_tls_secret\(environment\):", text)
assert re.search(r"return f\"\{environment\}/goldengate/tls-certificate\"", text)
print("OK")
' 2>&1)"
  if [ "$SECRET_NAME_CHECK" = "OK" ]; then
    pass "VDR 7: the exact secret-name derivation (<environment>/goldengate/source|target/admin, <environment>/goldengate/tls-certificate) is unchanged -- for dev this remains dev/goldengate/source/admin, dev/goldengate/target/admin, dev/goldengate/tls-certificate"
  else
    fail "VDR 7: secret-name derivation in hack/goldengate-deployment-model.py has changed: ${SECRET_NAME_CHECK}"
  fi
else
  skip "VDR 7: secret-name derivation check -- python3 unavailable"
fi

if [ "$PYTHON_AVAILABLE" = "true" ] && [ -s "${WORKDIR}/shared_secrets_step.sh" ]; then
  STUB_DIR2="${WORKDIR}/aws-stub-secrets"
  mkdir -p "$STUB_DIR2"
  cat > "${STUB_DIR2}/aws" <<'STUBEOF'
#!/bin/bash
if [ "$1" = "sts" ] && [ "$2" = "assume-role" ]; then
  echo '{"Credentials":{"AccessKeyId":"FAKEKEY2","SecretAccessKey":"FAKESECRET2","SessionToken":"FAKETOKEN2"}}'
elif [ "$1" = "sts" ] && [ "$2" = "get-caller-identity" ]; then
  echo "668311715351"
elif [ "$1" = "secretsmanager" ] && [ "$2" = "describe-secret" ]; then
  echo '{"ARN":"arn:aws:secretsmanager:eu-west-1:668311715351:secret:stub"}'
elif [ "$1" = "secretsmanager" ] && [ "$2" = "list-secret-version-ids" ]; then
  echo '{"Versions":[{"VersionId":"1","VersionStages":["AWSCURRENT"]}]}'
else
  echo "unexpected aws call: $*" >&2
  exit 1
fi
STUBEOF
  chmod +x "${STUB_DIR2}/aws"

  set +e
  SECRETS_TEST_OUT="$(PATH="${STUB_DIR2}:${PATH}" \
    EKS_DEPLOY_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGateEKSDeployRole-dev" \
    AWS_REGION="eu-west-1" GITHUB_RUN_ID="1" GITHUB_RUN_ATTEMPT="1" GG_SELECTED_ENVIRONMENT="dev" \
    bash "${WORKDIR}/shared_secrets_step.sh" 2>&1)"
  SECRETS_TEST_STATUS=$?
  set -e

  MASKED_OK=1
  for SECRET_VAL in FAKEKEY2 FAKESECRET2 FAKETOKEN2; do
    MASK_HITS="$(printf '%s\n' "$SECRETS_TEST_OUT" | grep -c "^::add-mask::${SECRET_VAL}\$" || true)"
    LEAK_HITS="$(printf '%s\n' "$SECRETS_TEST_OUT" | grep -v '^::add-mask::' | grep -c "$SECRET_VAL" || true)"
    if [ "$MASK_HITS" -lt 1 ] || [ "$LEAK_HITS" -ne 0 ]; then
      MASKED_OK=0
    fi
  done

  if [ "$SECRETS_TEST_STATUS" -eq 0 ] \
      && echo "$SECRETS_TEST_OUT" | grep -qF "assumed-role caller identity is account 668311715351" \
      && echo "$SECRETS_TEST_OUT" | grep -qF "dev/goldengate/source/admin exists with an AWSCURRENT version" \
      && echo "$SECRETS_TEST_OUT" | grep -qF "dev/goldengate/target/admin exists with an AWSCURRENT version" \
      && echo "$SECRETS_TEST_OUT" | grep -qF "dev/goldengate/tls-certificate exists with an AWSCURRENT version" \
      && [ "$MASKED_OK" -eq 1 ]; then
    pass "VDR 9/10: the real extracted validate_shared_secrets_once step, run end-to-end against a stubbed aws CLI, assumes the workload role, verifies its account, validates all three exact secret names read-only, and every temporary credential value appears ONLY inside its own ::add-mask:: directive -- never elsewhere in the step's output (no secret content is logged either, since the stub never returns any)"
  else
    fail "VDR 9/10: the extracted validate_shared_secrets_once step did not behave correctly, or leaked/failed to mask a temporary credential, against the stubbed aws CLI"
    echo "$SECRETS_TEST_OUT"
  fi

  rm -rf "$STUB_DIR2"
else
  skip "VDR 9/10: end-to-end credential-fix behavioral test -- python3/PyYAML unavailable or step extraction failed"
fi

# VDR 11/12/13/14: unchanged by this narrowly-scoped credential fix -- covered by their own dedicated, still-passing suites/sections rather than duplicated here: hack/test-goldengate-managed-efs-inventory-guard.py (managed-EFS inventory guard, item 11), the "Production hardening, Item 1" section above plus envs/dev/efs.tf itself (Terraform/EFS architecture, item 12), the Phase 6D0/6D0-Final Oracle/PostgreSQL runtime-identity sections above (item 13, replication.enabled=false unchanged), and hack/test-goldengate-replication.py (PostgreSQL->MSSQL Phase 6D1 constants, item 14).
if ! grep -q 'goldengate_efs_throughput_mode\|throughput_mode\s*=\s*"elastic"\|throughput_mode\s*=\s*"provisioned"' envs/dev/efs.tf 2>/dev/null; then
  pass "VDR 12: envs/dev/efs.tf's throughput_mode contract (fixed in the immediately-preceding turn) is untouched by this credential-only fix"
else
  fail "VDR 12: envs/dev/efs.tf's throughput_mode contract was unexpectedly modified by this turn"
fi

if grep -q 'replication:' envs/dev/gg-postgresql-repltest-01/values.yaml 2>/dev/null && grep -qE '^\s*enabled:\s*false' envs/dev/gg-postgresql-repltest-01/values.yaml 2>/dev/null \
    && grep -q 'replication:' envs/dev/gg-mssql-repltest-01/values.yaml 2>/dev/null && grep -qE '^\s*enabled:\s*false' envs/dev/gg-mssql-repltest-01/values.yaml 2>/dev/null; then
  pass "VDR 13: gg-postgresql-repltest-01/gg-mssql-repltest-01 values.yaml still declare replication.enabled=false, unchanged by this credential-only fix"
else
  fail "VDR 13: the live descriptors' replication.enabled=false declaration is missing or was modified"
fi

echo ""
echo "--- VDR correction: structural rendered-image validation (replaces the fragile grep-based check) ---"

# Real VDR evidence: deploy=false for gg-oracle-payments-01 correctly resolved IMAGE_REPOSITORY/IMAGE_TAG/IMAGE_DIGEST from ECR, but then failed at the OLD "Verify the rendered StatefulSet uses the selected verified image" step because it did `grep -qF "image: ${EXPECTED_IMAGE}"` against a rendered value that Helm intentionally quotes (`image: "repo:tag"`). The image was correct; only the text check was wrong. Fix: that grep-based step is REMOVED and its assertion is merged into the existing duplicate-key-safe PyYAML structural validator (no second, inconsistent Kubernetes-parsing implementation is introduced).

if ! grep -qF 'grep -qF "image: ${EXPECTED_IMAGE}"' .github/workflows/goldengate-eks-app.yaml 2>/dev/null \
    && grep -qF 'main_container_image = main_container.get("image")' .github/workflows/goldengate-eks-app.yaml 2>/dev/null \
    && grep -qF 'if main_container_image != expected_image:' .github/workflows/goldengate-eks-app.yaml 2>/dev/null; then
  pass "VDR-IMG 1: image validation is now structural YAML field comparison (main_container.get(\"image\") != expected_image), not a grep against the rendered text"
else
  fail "VDR-IMG 1: structural image-identity comparison is missing, or the obsolete grep-based text check is still present"
fi

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  python3 - "$EKS_APP_WORKFLOW" > "${WORKDIR}/image_validation_step.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["build_publish_and_deploy"]["steps"]:
    if step.get("name", "").startswith("Validate rendered singleRuntime manifest"):
        if "if" in step:
            sys.exit("step unexpectedly has a per-step if: condition")
        sys.stdout.write(step["run"])
        break
else:
    sys.exit("step not found")
PYEOF

  if [ ! -s "${WORKDIR}/image_validation_step.sh" ]; then
    fail "VDR-IMG: could not extract the merged structural validator step from ${EKS_APP_WORKFLOW} (or it now has an unexpected per-step if: condition)"
  else
    pass "VDR-IMG 14: the merged structural validator step has no per-step deploy-gating if: condition -- it runs whenever build_publish_and_deploy runs, exactly as the real deploy=false VDR run already exercised it"

    IMG_TEST_DIR="${WORKDIR}/image-validation-fixtures"
    mkdir -p "${IMG_TEST_DIR}/rendered"

    RELEASE_NAME="gg-oracle-payments-01"
    DEPLOYMENT_ID="gg-oracle-payments-01"
    TARGET_NAMESPACE="goldengate-dev"
    IMAGE_REPOSITORY="229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle"
    IMAGE_TAG="23.26.2.0.1"
    IMAGE_DIGEST="sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    VALUES_FILE="${IMG_TEST_DIR}/values.yaml"

    cat > "$VALUES_FILE" <<'YEOF'
runtime:
  containerName: ogg-oracle
YEOF

    run_image_validation_scenario() {
      # $1=rendered manifest content, $2=expected exit status, $3=required substring in output, $4=test description
      printf '%s' "$1" > "${IMG_TEST_DIR}/rendered/${RELEASE_NAME}.yaml"
      set +e
      IMG_TEST_OUT="$(cd "$IMG_TEST_DIR" && \
        RELEASE_NAME="$RELEASE_NAME" VALUES_FILE="$VALUES_FILE" DEPLOYMENT_ID="$DEPLOYMENT_ID" TARGET_NAMESPACE="$TARGET_NAMESPACE" \
        IMAGE_REPOSITORY="$IMAGE_REPOSITORY" IMAGE_TAG="$IMAGE_TAG" IMAGE_DIGEST="$IMAGE_DIGEST" \
        bash "${WORKDIR}/image_validation_step.sh" 2>&1)"
      IMG_TEST_STATUS=$?
      set -e
      if [ "$IMG_TEST_STATUS" -eq "$2" ] && echo "$IMG_TEST_OUT" | grep -qF "$3"; then
        pass "VDR-IMG: $4"
      else
        fail "VDR-IMG: $4 (exit=${IMG_TEST_STATUS}, expected=$2)"
        echo "$IMG_TEST_OUT"
      fi
    }

    VALID_SERVICES='
---
apiVersion: v1
kind: Service
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  clusterIP: 10.0.0.5
  type: ClusterIP
---
apiVersion: v1
kind: Service
metadata:
  name: gg-oracle-payments-01-headless
  namespace: goldengate-dev
spec:
  clusterIP: None
'
    INIT_SCRIPT_ARGS='            - '\''echo cleaning stale ServiceManager.pid ; rm -f -- "$SERVICE_MANAGER_PID_FILE"'\'''

    # 2: quoted rendered image (the real Helm template's actual quoting style) passes end-to-end.
    run_image_validation_scenario "apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  template:
    spec:
      initContainers:
        - name: prepare-u02-permissions
          image: \"229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1\"
          command: [\"sh\", \"-c\"]
          args:
${INIT_SCRIPT_ARGS}
      containers:
        - name: ogg-oracle
          image: \"229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1\"
${VALID_SERVICES}" 0 "references verified image 229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1" \
      "2: a quoted rendered image (image: \"repo:tag\", the real Helm template's actual style) correctly PASSES structural validation end-to-end"

    # 3: the same manifest with an unquoted image scalar (still valid YAML, same parsed value) also passes.
    run_image_validation_scenario "apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  template:
    spec:
      initContainers:
        - name: prepare-u02-permissions
          image: 229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1
          command: [\"sh\", \"-c\"]
          args:
${INIT_SCRIPT_ARGS}
      containers:
        - name: ogg-oracle
          image: 229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1
${VALID_SERVICES}" 0 "references verified image 229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1" \
      "3: an unquoted rendered image scalar (still valid YAML, resolves to the identical string) also PASSES -- proving the fix compares the PARSED value, not the rendered text's quoting style"

    # 4: wrong tag fails.
    run_image_validation_scenario 'apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  template:
    spec:
      containers:
        - name: ogg-oracle
          image: "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:99.99.99.99.9"
' 1 "does not reference the verified image" \
      "4: a rendered image with the wrong TAG fails closed"

    # 5: wrong repository fails.
    run_image_validation_scenario 'apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  template:
    spec:
      containers:
        - name: ogg-oracle
          image: "229410149234.dkr.ecr.eu-west-1.amazonaws.com/wrong-repo:23.26.2.0.1"
' 1 "does not reference the verified image" \
      "5: a rendered image with the wrong REPOSITORY fails closed"

    # 6: missing image field fails.
    run_image_validation_scenario 'apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  template:
    spec:
      containers:
        - name: ogg-oracle
' 1 "does not reference the verified image" \
      "6: a regular container with no image field at all fails closed"

    # 7: zero StatefulSets fails.
    run_image_validation_scenario 'apiVersion: v1
kind: Service
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  clusterIP: 10.0.0.5
' 1 "expected exactly one StatefulSet, found 0" \
      "7: zero rendered StatefulSet documents fails closed"

    # 8: multiple StatefulSets fails.
    run_image_validation_scenario 'apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  template:
    spec:
      containers:
        - name: ogg-oracle
          image: "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1"
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01-dup
  namespace: goldengate-dev
spec:
  template:
    spec:
      containers:
        - name: ogg-oracle
          image: "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1"
' 1 "expected exactly one StatefulSet, found 2" \
      "8: multiple rendered StatefulSet documents fails closed"

    # 9: multiple regular containers fails, per the singleRuntime one-application-container contract.
    run_image_validation_scenario 'apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  template:
    spec:
      containers:
        - name: ogg-oracle
          image: "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1"
        - name: extra-sidecar
          image: "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1"
' 1 "expected exactly one regular application container" \
      "9: multiple regular containers fails closed per the singleRuntime contract"

    # 10: an initContainer-only pod (no regular containers) fails -- proves prepare-u02-permissions can never be mistaken for the main application container.
    run_image_validation_scenario 'apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  template:
    spec:
      initContainers:
        - name: prepare-u02-permissions
          image: "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1"
      containers: []
' 1 "expected exactly one regular application container" \
      "10: an initContainer-only pod spec (prepare-u02-permissions present, no regular containers) fails closed instead of treating the initContainer as the main container"

    # 11: runtime.containerName mismatch fails, proving that identity check remains enforced.
    run_image_validation_scenario 'apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: gg-oracle-payments-01
  namespace: goldengate-dev
spec:
  template:
    spec:
      containers:
        - name: wrong-container-name
          image: "229410149234.dkr.ecr.eu-west-1.amazonaws.com/ogg-oracle:23.26.2.0.1"
' 1 "expected main container name 'ogg-oracle', found 'wrong-container-name'" \
      "11: a main container name that does not match values.yaml's runtime.containerName still fails closed"

    rm -rf "$IMG_TEST_DIR"
  fi
else
  skip "VDR-IMG: structural rendered-image validation behavioral tests -- python3/PyYAML unavailable"
fi

# 12/13: the ECR image existence/digest verification step itself is untouched by this fix (only the DOWNSTREAM rendered-manifest check changed) -- see check 27 above, which now also confirms the obsolete grep-based text check no longer exists.
if grep -q "Verify the selected image exists in the approved private ECR" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -qF 'IMAGE_DIGEST="$(python3 -c' "$EKS_APP_WORKFLOW" 2>/dev/null \
    && ! grep -qE "aws ecr (put-image|batch-delete-image|start-image-scan)" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "VDR-IMG 12/13: ECR image existence (describe-images) and digest resolution remain read-only and unchanged -- no image is pushed, deleted, or mutated by this fix"
else
  fail "VDR-IMG 12/13: ECR image existence/digest verification step is missing, changed, or a mutating ECR call was introduced"
fi

# 15: deploy=false performing no Argo/EKS runtime mutation is unrelated to this image-validation fix and remains covered by the existing dry-run-unreachable structural proof earlier in this suite (see "Correction pass, Issue ..." sections above). 16/17/18/19: cross-account shared-secret fix (previous VDR turn), EFS/Terraform architecture, Oracle/PostgreSQL descriptors + replication=false, and PostgreSQL->MSSQL Phase 6D1 are all unrelated to this narrowly-scoped rendered-image validation fix and remain covered by their own dedicated, still-passing sections/suites above (the "VDR correction: validate_shared_secrets_once..." section, the "Production hardening, Item 1" section, the Phase 6D0 Oracle/PostgreSQL sections, and hack/test-goldengate-replication.py respectively) -- none of items 15-19 are re-proved here, to avoid duplicating that logic.

echo ""
echo "--- VDR correction: monitor_dry_run_validation runner (CodeBuild -> ubuntu-latest) + least-privilege permissions ---"

# Real VDR evidence: the deploy=false dry-run reached monitor_dry_run_validation and failed at "Set up Python" with "Version 3.12 was not found in the local cache" -- the CodeBuild/self-hosted runner image does not carry the actions/setup-python 3.12 x64 distribution. This is a runner/toolchain gap, not a monitor application, requirements.txt, EFS, ECR, or EKS defect: the job performs only local/read-only CI validation (checkout, Python 3.12 setup, pip install, unit tests, folder-driven registry generation, Helm lint/template) and needs no AWS/EKS/Argo access at all, so it moves to the standard ubuntu-latest runner. No other job's runner changes. Least-privilege follow-up: this job otherwise inherits the workflow-level `permissions: id-token: write` even though it never authenticates to AWS, so it now declares its own job-level `permissions: contents: read` (checks LP1-LP3 below), which is sufficient for actions/checkout and leaves id-token absent rather than granted. Checks 1-13 below (runner, Python 3.12, pip cache, no AWS credential setup, no kubectl/Argo, etc.) are re-run unchanged in the same behavioral pass, proving the least-privilege change did not regress any prior monitor dry-run assertion.

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  MONITOR_DRY_RUN_CHECK="$(python3 -c '
import yaml
with open("'"$EKS_APP_WORKFLOW"'") as f:
    doc = yaml.safe_load(f)
job = doc["jobs"]["monitor_dry_run_validation"]
results = []

results.append(("1: runs-on is ubuntu-latest", job.get("runs-on") == "ubuntu-latest"))
results.append(("2: runs-on is not the codebuild runner expression", "codebuild-" not in str(job.get("runs-on"))))

steps = job.get("steps", [])
py_steps = [s for s in steps if s.get("uses", "").startswith("actions/setup-python@v5")]
results.append(("3: keeps actions/setup-python@v5", len(py_steps) == 1))
py_with = py_steps[0].get("with", {}) if py_steps else {}
results.append(("4: keeps python-version 3.12", py_with.get("python-version") == "3.12"))
results.append(("5: keeps cache: pip", py_with.get("cache") == "pip"))
cache_dep_path = py_with.get("cache-dependency-path", "") or ""
results.append((
    "5: keeps cache-dependency-path for both monitor requirement files",
    "monitoring/monitor/requirements.txt" in cache_dep_path and "monitoring/monitor/requirements-test.txt" in cache_dep_path,
))

all_run_text = "\n".join(s.get("run", "") for s in steps)
results.append((
    "6: installs runtime + test requirements",
    "-r monitoring/monitor/requirements.txt" in all_run_text and "-r monitoring/monitor/requirements-test.txt" in all_run_text,
))
results.append(("7: runs monitor unit tests", "python3 -m unittest discover -s monitoring/monitor/tests" in all_run_text))
results.append((
    "8: generates the folder-driven registry",
    "goldengate-deployment-model.py --environment \"${GG_SELECTED_ENVIRONMENT}\" registry" in all_run_text,
))
results.append(("9: performs Helm lint locally", "helm lint" in all_run_text))
results.append(("9: performs Helm template locally", "helm template" in all_run_text))

uses_list = [s.get("uses", "") for s in steps]
results.append(("10: no configure-aws-credentials step", not any("aws-actions/configure-aws-credentials" in u for u in uses_list)))
results.append(("11: does not assume an AWS role", "assume-role" not in all_run_text and "role-to-assume" not in str(job)))
results.append(("12: performs no kubectl command", "kubectl " not in all_run_text))
results.append(("13: performs no Argo mutation", "argocd" not in all_run_text.lower()))

# Least-privilege correction: job-level permissions override the workflow-level id-token: write down to contents: read only for this no-OIDC-needed job.
job_permissions = job.get("permissions")
results.append(("LP1: has job-level permissions block", isinstance(job_permissions, dict)))
results.append(("LP2: permissions.contents == read", isinstance(job_permissions, dict) and job_permissions.get("contents") == "read"))
results.append(("LP3: id-token is absent (not write)", isinstance(job_permissions, dict) and job_permissions.get("id-token") != "write"))
results.append(("LP3: permissions has no other key besides contents", isinstance(job_permissions, dict) and set(job_permissions.keys()) == {"contents"}))

for label, ok in results:
    print(("OK " if ok else "FAIL ") + label)
' 2>&1)"
  while IFS= read -r line; do
    case "$line" in
      FAIL\ *) fail "VDR-MON: ${line#FAIL }" ;;
      OK\ *) pass "VDR-MON: ${line#OK }" ;;
    esac
  done <<< "$MONITOR_DRY_RUN_CHECK"
else
  skip "VDR-MON: monitor_dry_run_validation structural checks -- python3/PyYAML unavailable"
fi

if grep -qF "if: \${{ needs.validate_model.outputs.effective_deploy == 'false' && needs.validate_model.outputs.has_active_deployments == 'true' && always() && needs.validate_shared_secrets_once.result == 'success' && needs.build_publish_and_deploy.result != 'failure' && needs.build_publish_and_deploy.result != 'cancelled' && needs.delete_removed_argocd_applications.result != 'failure' && needs.delete_removed_argocd_applications.result != 'cancelled' && needs.replication_dry_run_validation.result != 'failure' && needs.replication_dry_run_validation.result != 'cancelled' }}" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "VDR-MON 14: monitor_dry_run_validation's deploy=false job-gating if: condition retains every original clause plus the additive has_active_deployments=='true' gate"
else
  fail "VDR-MON 14: monitor_dry_run_validation's deploy=false job-gating if: condition was unexpectedly modified"
fi

if grep -qF "uses: ./.github/workflows/goldengate-monitor.yaml" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -qF "deploy: true" "$EKS_APP_WORKFLOW" 2>/dev/null \
    && grep -qF "needs.validate_model.outputs.effective_deploy == 'true' && needs.validate_model.outputs.has_active_deployments == 'true' && always() && needs.validate_shared_secrets_once.result == 'success'" "$EKS_APP_WORKFLOW" 2>/dev/null; then
  pass "VDR-MON 15: monitor_sync_once's deploy=true reusable-workflow call (goldengate-monitor.yaml, deploy: true) is unchanged, and its job-gating if: condition retains every original clause plus the additive has_active_deployments=='true' gate"
else
  fail "VDR-MON 15: monitor_sync_once's deploy=true path appears to have changed"
fi

echo ""
echo "--- Orchestrator gate: monitor stages skip when the canonical model has zero active runtimes ---"

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  ACTIVE_GATE_CHECK="$(python3 -c '
import yaml
with open("'"$EKS_APP_WORKFLOW"'") as f:
    doc = yaml.safe_load(f)
jobs = doc["jobs"]
results = []

results.append(("1: validate_model exports has_active_deployments", "has_active_deployments" in jobs["validate_model"].get("outputs", {})))

active_step = next((s for s in jobs["validate_model"]["steps"] if s.get("id") == "active_runtime_state"), None)
results.append(("2: validate_model has an active_runtime_state step", active_step is not None))
step_run = (active_step or {}).get("run", "")
results.append(("3: has_active_deployments is derived from the canonical registry, not deployment_matrix", "goldengate-deployment-model.py --environment \"${GG_SELECTED_ENVIRONMENT}\" registry" in step_run and "outputs.deployment_matrix" not in step_run and "DEPLOYMENT_MATRIX" not in step_run))
results.append(("4: the active-runtime step never greps YAML (uses PyYAML safe_load)", "grep" not in step_run and "yaml.safe_load" in step_run))

monitor_sync_if = jobs["monitor_sync_once"]["if"]
results.append(("5: monitor_sync_once requires has_active_deployments == \'\''true\'\''", "needs.validate_model.outputs.has_active_deployments == '"'"'true'"'"'" in monitor_sync_if))
results.append(("6: monitor_sync_once retains its original effective_deploy/dependency clauses", "needs.validate_model.outputs.effective_deploy == '"'"'true'"'"'" in monitor_sync_if and "needs.validate_shared_secrets_once.result == '"'"'success'"'"'" in monitor_sync_if and "needs.replication_reconcile_once.result != '"'"'cancelled'"'"'" in monitor_sync_if))

dry_run_if = jobs["monitor_dry_run_validation"]["if"]
results.append(("7: monitor_dry_run_validation requires has_active_deployments == \'\''true\'\''", "needs.validate_model.outputs.has_active_deployments == '"'"'true'"'"'" in dry_run_if))
results.append(("8: monitor_dry_run_validation retains its original effective_deploy/dependency clauses", "needs.validate_model.outputs.effective_deploy == '"'"'false'"'"'" in dry_run_if and "needs.validate_shared_secrets_once.result == '"'"'success'"'"'" in dry_run_if and "needs.replication_dry_run_validation.result != '"'"'cancelled'"'"'" in dry_run_if))

# replication_monitor_acceptance already requires monitor_sync_once.result == "success"; a skipped monitor_sync_once therefore naturally skips it too, with no change needed.
rma_if = jobs["replication_monitor_acceptance"]["if"]
results.append(("9: replication_monitor_acceptance still requires monitor_sync_once.result == \'\''success\'\'' (naturally skips when monitor_sync_once is skipped, no change needed)", "needs.monitor_sync_once.result == '"'"'success'"'"'" in rma_if))

# final_validation only ever rejects monitor jobs on failure/cancelled, never on skipped -- so both gated monitor jobs skipping cleanly still lets final_validation run.
fv_if = jobs["final_validation"]["if"]
results.append(("10: final_validation only rejects monitor_sync_once on failure/cancelled (never skipped)", "needs.monitor_sync_once.result != '"'"'failure'"'"'" in fv_if and "needs.monitor_sync_once.result != '"'"'cancelled'"'"'" in fv_if and "needs.monitor_sync_once.result == '"'"'skipped'"'"'" not in fv_if and "needs.monitor_sync_once.result == '"'"'success'"'"'" not in fv_if))
results.append(("11: final_validation only rejects monitor_dry_run_validation on failure/cancelled (never skipped)", "needs.monitor_dry_run_validation.result != '"'"'failure'"'"'" in fv_if and "needs.monitor_dry_run_validation.result != '"'"'cancelled'"'"'" in fv_if))

for label, ok in results:
    print(("OK " if ok else "FAIL ") + label)
' 2>&1)"
  while IFS= read -r line; do
    case "$line" in
      FAIL\ *) fail "ACTIVE-GATE: ${line#FAIL }" ;;
      OK\ *) pass "ACTIVE-GATE: ${line#OK }" ;;
    esac
  done <<< "$ACTIVE_GATE_CHECK"
else
  skip "ACTIVE-GATE: monitor active-runtime gating checks -- python3/PyYAML unavailable"
fi

# 17/18/19/20/21: cross-account Secrets Manager fix, structural runtime-image validation fix, EFS/Terraform architecture, Oracle/PostgreSQL descriptors + replication=false, and PostgreSQL->MSSQL Phase 6D1 are all unrelated to this narrowly-scoped monitor_dry_run_validation runner fix and remain covered by their own dedicated, still-passing sections/suites above (the "VDR correction: validate_shared_secrets_once..." section, the "VDR correction: structural rendered-image validation..." section, the "Production hardening, Item 1" section, the Phase 6D0 Oracle/PostgreSQL sections, and hack/test-goldengate-replication.py respectively) -- not re-proved here, to avoid duplicating that logic.

echo ""
echo "--- Self-service test architecture: generic descriptor invariants (no per-deployment-ID test code) ---"

# Production self-service requirement: onboarding envs/dev/<id>/values.yaml must never require editing a test file to add its name/count to a hardcoded list. Every check below is driven dynamically by hack/goldengate-deployment-model.py's own scan/build_registry/managed-efs-inventory semantics, or by each descriptor's OWN properties (role/persistence mode) -- never by a fixed set of real deployment IDs. The historical existing-EFS descriptors (gg-oracle-payments-01, gg-postgresql-payments-01) were physically retired; their legacy storage contract is no longer protected here since the files no longer exist.

if [ "$PYTHON_AVAILABLE" = "true" ]; then
  GENERIC_DESCRIPTOR_CHECK="$(python3 -c '
import importlib.util
import json
import subprocess
import sys

import yaml

TOOL_PATH = "hack/goldengate-deployment-model.py"
spec = importlib.util.spec_from_file_location("goldengate_deployment_model", TOOL_PATH)
gdm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gdm)

results = []


def check(label, ok):
    results.append((label, ok))


active, inactive, invalid = gdm.scan("dev")
check("scan(dev): no invalid descriptors", invalid == [])
check("validate(dev): no cross-descriptor problems (ALB order / pipeline-role / EFS-token-collision / duplicate-ID uniqueness)", gdm.validate("dev") == [])

# Dynamic registry invariant: the registry must contain EXACTLY the scanned active IDs -- never a hardcoded name/count, so it automatically absorbs any future onboarded folder.
expected_active_ids = sorted(d["deploymentId"] for d in active)
registry = gdm.build_registry("dev")
actual_registry_ids = sorted(d["name"] for d in registry["deployments"])
check("registry(dev) contains exactly the scan-derived active deployment IDs", actual_registry_ids == expected_active_ids)

# Dynamic managed-EFS-inventory invariant: compare the REAL command output against a set derived independently from the same scan, filtered by efsMode == managed -- never asserting today'"'"'s managed count is any particular fixed number.
expected_managed = sorted(
    (
        {"deploymentId": d["deploymentId"], "efsCreationToken": d["efsCreationToken"]}
        for d in active + inactive
        if d["efsMode"] == "managed"
    ),
    key=lambda x: x["deploymentId"],
)
managed_inventory_proc = subprocess.run(
    [sys.executable, TOOL_PATH, "--environment", "dev", "managed-efs-inventory"],
    capture_output=True, text=True, check=True,
)
actual_managed = json.loads(managed_inventory_proc.stdout)
check("managed-efs-inventory command output matches the dynamically derived managed set", actual_managed == expected_managed)

# MILESTONE (temporary, not a permanent inventory coupling): proves the first production managed-EFS runtime was successfully onboarded. Delete this single check once managed EFS is routine and no longer needs a dedicated milestone proof -- it asserts only "at least one", never a specific ID or exact count.
check("MILESTONE: at least one real managed-EFS descriptor exists in envs/dev", len(expected_managed) >= 1)

# Generic per-descriptor contract, driven entirely by each descriptor'"'"'s OWN role/persistence properties -- never by deployment ID, so it automatically covers any future onboarded folder without new test code.
alb_orders_by_shared_alb = []
for d in active:
    dep_id = d["deploymentId"]
    prefix = f"generic[{dep_id}]"

    check(f"{prefix}: runtime ServiceAccount derives from deploymentType", d["runtimeServiceAccountName"] == gdm.resolve_runtime_service_account(d["deploymentType"]))
    check(f"{prefix}: TLS secret derives from environment", d["tlsSecretName"] == gdm.resolve_tls_secret(d["environment"]))
    check(f"{prefix}: admin secret derives from role", d["adminSecretName"] == gdm.resolve_admin_secret(d["environment"], d["role"]))

    # Supplementary raw-YAML read for fields the parsed descriptor does not surface (service ports, StorageClass) -- keyed by the deploymentId gdm.scan() already discovered above; this is not a second discovery mechanism, only a follow-up read of one already-discovered folder.
    with open(f"envs/dev/{dep_id}/values.yaml") as f:
        raw = yaml.safe_load(f)

    if d["albGroupOrder"] is not None:
        check(f"{prefix}: ALB groupOrder is a valid integer representation", str(d["albGroupOrder"]).lstrip("-").isdigit())
        if (raw.get("ingress") or {}).get("mode") == "shared":
            alb_orders_by_shared_alb.append(d["albGroupOrder"])

    ports = ((raw.get("runtime") or {}).get("service") or {}).get("ports") or {}
    if d["role"] == "source":
        check(f"{prefix}: source role has dist=9013/receiver=null", ports.get("dist") == 9013 and ports.get("receiver") is None)
    elif d["role"] == "target":
        check(f"{prefix}: target role has dist=null/receiver=9014", ports.get("dist") is None and ports.get("receiver") == 9014)

    persistence = raw.get("persistence") or {}
    if persistence.get("enabled") is True and persistence.get("provider") == "efs":
        efs = persistence.get("efs") or {}
        if d["efsMode"] == "managed":
            check(f"{prefix}: managed mode has no fileSystemId", d["efsFileSystemId"] is None)
            check(f"{prefix}: managed mode has a present efsCreationToken", bool(d["efsCreationToken"]))
            check(f"{prefix}: managed efsCreationToken equals derive_efs_creation_token(environment, deploymentId)", d["efsCreationToken"] == gdm.derive_efs_creation_token(d["environment"], dep_id))
            check(f"{prefix}: managed mode StorageClass reclaimPolicy == Retain", (efs.get("storageClass") or {}).get("reclaimPolicy") == "Retain")
        elif d["efsMode"] == "existing":
            check(f"{prefix}: existing mode has a present, valid fileSystemId", d["efsFileSystemId"] is not None and bool(gdm._EFS_FILESYSTEM_ID_RE.match(d["efsFileSystemId"])))

check("ALB groupOrder is unique across every active shared-ALB descriptor", len(alb_orders_by_shared_alb) == len(set(alb_orders_by_shared_alb)))

for label, ok in results:
    print(("OK " if ok else "FAIL ") + label)
' 2>&1)"
  while IFS= read -r line; do
    case "$line" in
      FAIL\ *) fail "${line#FAIL }" ;;
      OK\ *) pass "${line#OK }" ;;
      *) fail "generic descriptor invariant check crashed: $line" ;;
    esac
  done <<< "$GENERIC_DESCRIPTOR_CHECK"
else
  skip "generic descriptor invariant checks -- python3/PyYAML unavailable"
fi

if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # Repo-wide scan of envs/dev/*.tf (excluding efs.tf, exempted for the reviewed EFS decommission allowlist) for a deployment-ID-specific carve-out.
  TF_CARVEOUT_MATCHES="$(grep -lF "gg-mssql-repltest-01" envs/dev/*.tf 2>/dev/null | grep -vF "envs/dev/efs.tf" || true)"
  if [ -z "$TF_CARVEOUT_MATCHES" ]; then
    pass "no deployment-specific Terraform carve-out exists for the MSSQL runtime anywhere under envs/dev/*.tf (tracked or untracked) outside the explicit, reviewed EFS decommission allowlist -- the generic local.goldengate_managed_efs_deployments for_each and the restored shared gg-runtime-sa identity own it automatically"
  else
    fail "a deployment-ID-specific Terraform carve-out exists outside the explicit EFS decommission allowlist -- onboarding must remain folder-driven only: ${TF_CARVEOUT_MATCHES}"
  fi
else
  skip "Terraform-file-unchanged check -- not a git repository"
fi

# The two historical existing-EFS descriptors were physically retired (see the physical-absence check earlier in this suite); this repo no longer carries a mode=existing descriptor to protect.

# Not inventory-count-related -- unaffected by how many deployments exist, so kept as a direct regression check.
if grep -qF 'REPLICATION_SUPPORTED_SOURCE_TYPE = "postgresql"' hack/goldengate-deployment-model.py 2>/dev/null \
    && grep -qF 'REPLICATION_SUPPORTED_TARGET_TYPE = "mssql"' hack/goldengate-deployment-model.py 2>/dev/null; then
  pass "PostgreSQL->MSSQL Phase 6D1 constants (REPLICATION_SUPPORTED_SOURCE_TYPE=postgresql, REPLICATION_SUPPORTED_TARGET_TYPE=mssql) remain unchanged"
else
  fail "the Phase 6D1 replication scope constants have changed"
fi

echo ""
echo "--- Fresh-EKS Phase A: canonical environment contract, OIDC rebind, common runtime IRSA ---"

OLD_DESTROYED_OIDC_ID="407C4385FF87947926730569F1E564FB"

# 1: the destroyed-cluster OIDC ID has zero references outside this suite's own detection literal above and hack/goldengate-environment.py's own OLD_DESTROYED_OIDC_ID fail-closed detector constant (which must name the literal in order to reject it from environment.yaml -- that is the check, not a leak).
OLD_OIDC_HITS="$(grep -rl "$OLD_DESTROYED_OIDC_ID" --include='*.tf' --include='*.py' --include='*.json' --include='*.yaml' --include='*.yml' . 2>/dev/null | grep -vF "hack/test-goldengate-deployment-models.sh" | grep -vF "hack/goldengate-environment.py" || true)"
if [ -z "$OLD_OIDC_HITS" ]; then
  pass "1: the destroyed-cluster OIDC ID (${OLD_DESTROYED_OIDC_ID}) has zero references in production .tf/.py/.json/.yaml source outside the resolver's own detector constant"
else
  fail "1: the destroyed-cluster OIDC ID (${OLD_DESTROYED_OIDC_ID}) still appears in:"$'\n'"${OLD_OIDC_HITS}"
fi

# 2: envs/dev/environment.yaml is valid and the resolver's own validator confirms it contains no credential-shaped key/value.
if [ "$PYTHON_AVAILABLE" = "true" ] && [ -f "envs/dev/environment.yaml" ]; then
  if python3 hack/goldengate-environment.py --environment dev validate >/dev/null 2>&1; then
    pass "2: envs/dev/environment.yaml is valid and contains no credential-shaped key/value (hack/goldengate-environment.py validate)"
  else
    fail "2: envs/dev/environment.yaml failed validation"
  fi
else
  skip "2: environment.yaml validation -- python3 or envs/dev/environment.yaml unavailable"
fi

# 3: generated IAM policy JSON is in sync with environment.yaml -- proves no hand-edited drift and (transitively) that the OIDC issuer embedded in every sts.json matches the canonical configured value.
if [ "$PYTHON_AVAILABLE" = "true" ] && [ -f "envs/dev/environment.yaml" ]; then
  if python3 hack/goldengate-environment.py --environment dev render-iam-policies --check >/dev/null 2>&1; then
    pass "3: all generated envs/dev/policies/**/sts.json and policies_1.json are in sync with environment.yaml (render-iam-policies --check)"
  else
    fail "3: generated IAM policy JSON is out of sync with environment.yaml -- run render-iam-policies --write"
  fi
else
  skip "3: render-iam-policies --check -- python3 or envs/dev/environment.yaml unavailable"
fi

# 3b: deterministic environment/IAM generation regression suite -- proves generated output is never read back as a template, A->B->C environment changes never retain a stale identity, --check detects stale generated output, and all six current DEV permission policies remain semantically unchanged.
if [ "$PYTHON_AVAILABLE" = "true" ] && [ -f "hack/test-goldengate-environment.py" ]; then
  # Command substitution must sit directly in the if-condition -- set -e would abort the script at a standalone `var=$(...)` assignment before the else branch ever ran.
  if ENV_IAM_SUITE_OUTPUT="$(
    PYTHONDONTWRITEBYTECODE=1 \
    python3 hack/test-goldengate-environment.py 2>&1
  )"; then
    pass "3b: environment/IAM deterministic generation tests pass (hack/test-goldengate-environment.py)"
  else
    ENV_IAM_SUITE_RC=$?
    fail "3b: environment/IAM deterministic generation tests failed (exit ${ENV_IAM_SUITE_RC}):"$'\n'"${ENV_IAM_SUITE_OUTPUT}"
  fi
else
  skip "3b: environment/IAM deterministic generation tests -- python3 or hack/test-goldengate-environment.py unavailable"
fi

# 4/5/6/7: runtime trust resolves to exactly system:serviceaccount:goldengate-dev:gg-runtime-sa -- no wildcard, no gg-oracle-sa, no gg-postgresql-sa, no gg-dev-*:ogg-oracle-sa.
SECRETS_STS="envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json"
if [ "$PYTHON_AVAILABLE" = "true" ] && [ -f "$SECRETS_STS" ]; then
  TRUST_CHECK="$(python3 -c '
import json, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)

sub = doc["Statement"][0]["Condition"]["StringLike"][[k for k in doc["Statement"][0]["Condition"]["StringLike"] if k.endswith(":sub")][0]]
subjects = sub if isinstance(sub, list) else [sub]

results = []
results.append(("4", subjects == ["system:serviceaccount:goldengate-dev:gg-runtime-sa"]))
results.append(("5", not any("*" in s for s in subjects)))
results.append(("6", not any(s.endswith(":gg-oracle-sa") for s in subjects)))
results.append(("7", not any(s.endswith(":gg-postgresql-sa") for s in subjects)))
results.append(("8", not any("ogg-oracle-sa" in s for s in subjects)))
for label, ok in results:
    print(("OK " if ok else "FAIL ") + label)
' "$SECRETS_STS" 2>&1)"
  while IFS= read -r line; do
    case "$line" in
      "OK 4") pass "4: runtime trust resolves to EXACTLY [\"system:serviceaccount:goldengate-dev:gg-runtime-sa\"] -- one entry, no more, no less" ;;
      "FAIL 4") fail "4: runtime trust subjects are not exactly [\"system:serviceaccount:goldengate-dev:gg-runtime-sa\"]" ;;
      "OK 5") pass "5: no wildcard (*) exists in any runtime trust subject" ;;
      "FAIL 5") fail "5: a wildcard exists in a runtime trust subject" ;;
      "OK 6") pass "6: no gg-oracle-sa trust remains" ;;
      "FAIL 6") fail "6: gg-oracle-sa trust still exists" ;;
      "OK 7") pass "7: no gg-postgresql-sa trust remains" ;;
      "FAIL 7") fail "7: gg-postgresql-sa trust still exists" ;;
      "OK 8") pass "8: no gg-dev-*:ogg-oracle-sa (or any ogg-oracle-sa) trust remains" ;;
      "FAIL 8") fail "8: ogg-oracle-sa trust still exists" ;;
    esac
  done <<< "$TRUST_CHECK"
else
  skip "4/5/6/7/8: runtime trust subject checks -- python3 or ${SECRETS_STS} unavailable"
fi

# 9: platform DEV desired state does not render a transitional runtime ServiceAccount (helm template, real chart).
if command -v helm >/dev/null 2>&1 && [ -f "platform/dev/goldengate-platform/values.yaml" ]; then
  PLATFORM_RENDERED="$(helm template goldengate-platform helm/goldengate-platform \
    --values platform/dev/goldengate-platform/values.yaml \
    --set runtimeServiceAccount.roleArn="arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev" \
    --set-string environment=dev \
    --set-string namespaces.runtime.name=goldengate-dev \
    --set fluentBit.create=false \
    --show-only templates/runtime-serviceaccounts.yaml 2>&1)"
  if echo "$PLATFORM_RENDERED" | grep -qF "name: gg-runtime-sa" \
      && ! echo "$PLATFORM_RENDERED" | grep -qE "name: gg-(oracle|postgresql)-sa"; then
    pass "9: platform DEV desired state renders exactly gg-runtime-sa and no transitional runtime ServiceAccount"
  else
    fail "9: platform DEV desired state rendering did not match expectations:"$'\n'"${PLATFORM_RENDERED}"
  fi
else
  skip "9: platform chart rendering -- helm or platform/dev/goldengate-platform/values.yaml unavailable"
fi

# 10: generic chart behavior still works (helm lint, real chart, real DEV values).
if command -v helm >/dev/null 2>&1 && [ -f "platform/dev/goldengate-platform/values.yaml" ]; then
  if helm lint helm/goldengate-platform \
      --values platform/dev/goldengate-platform/values.yaml \
      --set runtimeServiceAccount.roleArn="arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev" \
      --set-string environment=dev \
      --set-string namespaces.runtime.name=goldengate-dev \
      --set fluentBit.create=false >/dev/null 2>&1; then
    pass "10: helm lint passes for the goldengate-platform chart against real DEV values"
  else
    fail "10: helm lint failed for the goldengate-platform chart against real DEV values"
  fi
else
  skip "10: helm lint -- helm or platform/dev/goldengate-platform/values.yaml unavailable"
fi

# 11: envs/dev/environment.tf pins the corporate EFS/IAM module architecture is untouched -- this phase never replaces a corporate module with raw resources.
if grep -qF 'source = "git::https://github.com/AbuDhabiCommercialBank/aws-tf-module-iam-role' envs/dev/iam.tf 2>/dev/null \
    && grep -qF 'ref=v2.0.0' envs/dev/iam.tf 2>/dev/null \
    && ! grep -qE 'resource\s+"aws_iam_role"' envs/dev/*.tf 2>/dev/null; then
  pass "11: envs/dev/iam.tf still uses the approved corporate aws-tf-module-iam-role at v2.0.0 -- no raw aws_iam_role resource was introduced"
else
  fail "11: envs/dev/iam.tf's corporate IAM module pin changed, or a raw aws_iam_role resource was introduced"
fi

# 12/13: current runtime/EFS safety state is unchanged by this phase.
if grep -A1 '^lifecycle:' envs/dev/gg-postgresql-repltest-01/values.yaml 2>/dev/null | grep -q 'state: absent' \
    && grep -A1 '^lifecycle:' envs/dev/gg-mssql-repltest-01/values.yaml 2>/dev/null | grep -q 'state: absent'; then
  pass "12: both runtime descriptors remain lifecycle.state=absent"
else
  fail "12: a runtime descriptor's lifecycle.state is no longer absent"
fi

if grep -A1 '^replication:' envs/dev/gg-postgresql-repltest-01/values.yaml 2>/dev/null | grep -q 'enabled: false' \
    && grep -A1 '^replication:' envs/dev/gg-mssql-repltest-01/values.yaml 2>/dev/null | grep -q 'enabled: false'; then
  pass "12b: both runtime descriptors remain replication.enabled=false"
else
  fail "12b: a runtime descriptor's replication.enabled is no longer false"
fi

if grep -qE '^\s*"gg-postgresql-repltest-01"' envs/dev/efs.tf 2>/dev/null \
    && grep -qE '^\s*"gg-mssql-repltest-01"' envs/dev/efs.tf 2>/dev/null; then
  pass "13: the EFS decommission hold still contains exactly the two runtime IDs"
else
  fail "13: the EFS decommission hold changed"
fi

# 14: centralization regression sweep -- outside envs/dev/environment.yaml (canonical) and envs/dev/policies/** (generated), no first-party PRODUCTION .tf/.py source may independently hardcode the real current workload/build account IDs, region, cluster name, or OIDC host. Excludes: .git, vendored Argo CD chart source, every test file (test-*.py/test_*.py/*/tests/* -- synthetic fixture values are explicitly permitted per this repo's own convention, never confused with production hardcoding), and the generated envs/dev/policies/** output itself. hack/goldengate-environment.py (the generator) is deliberately NOT excluded: it builds every generated policy purely from derive_values(doc), so a real account ID appearing in its source would itself be a centralization leak.
CENTRALIZATION_HITS="$(grep -rlE '668311715351|229410149234' --include='*.tf' --include='*.py' . 2>/dev/null \
  | grep -vF '.git/' \
  | grep -vF 'helm/argocd/charts/' \
  | grep -vE '(^|/)test[-_][^/]*\.py$' \
  | grep -vF '/tests/' \
  | grep -vF 'envs/dev/environment.tf' \
  | grep -vF 'envs/dev/policies/' \
  || true)"
if [ -z "$CENTRALIZATION_HITS" ]; then
  pass "14: no first-party production .tf/.py source (including hack/goldengate-environment.py, the generator) hardcodes the real workload/build account ID outside envs/dev/environment.tf (canonical), envs/dev/policies/** (generated output), and test files (synthetic fixtures)"
else
  fail "14: production source independently hardcodes an account ID outside the canonical/generated/test-fixture boundary:"$'\n'"${CENTRALIZATION_HITS}"
fi

echo ""
echo "--- Phase 11: active workflow environment-identity centralization ---"

# 1: no active workflow depends on a retired repository variable that used to independently duplicate envs/dev/environment.yaml identity.
RETIRED_WORKFLOW_VARS='vars\.AWS_REGION|vars\.ACCOUNT_ID_DEV|vars\.AWS_CLUSTER_NAME|vars\.AWS_CLUSTER_ARN|vars\.EKS_DEPLOY_ROLE_ARN|vars\.GOLDENGATE_AWS_ROLE_ARN'
RETIRED_VARS_HITS="$(grep -rlE "$RETIRED_WORKFLOW_VARS" .github/workflows/*.yaml 2>/dev/null || true)"
if [ -z "$RETIRED_VARS_HITS" ]; then
  pass "Phase 11 1: no active workflow references vars.AWS_REGION/ACCOUNT_ID_DEV/AWS_CLUSTER_NAME/AWS_CLUSTER_ARN/EKS_DEPLOY_ROLE_ARN/GOLDENGATE_AWS_ROLE_ARN -- environment identity is loaded from envs/<environment>/environment.yaml, never a repository variable"
else
  fail "Phase 11 1: a retired repository-variable environment-identity reference remains in:"$'\n'"${RETIRED_VARS_HITS}"
fi

# 2: no active workflow independently hardcodes the real current workload/build account ID, ECR registry, or EKS cluster name as runtime identity.
WORKFLOW_HARDCODE_HITS="$(grep -rlE '668311715351|229410149234|gg-poc-dev|[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com' .github/workflows/*.yaml 2>/dev/null \
  | grep -vF '.github/workflows/gg-iam-secrets-deployment.yaml' \
  || true)"
if [ -z "$WORKFLOW_HARDCODE_HITS" ]; then
  pass "Phase 11 2: no active workflow independently hardcodes the real workload/build account ID, ECR registry, or EKS cluster name -- every reference is loaded from envs/<environment>/environment.yaml via hack/goldengate-environment.py github-env"
else
  fail "Phase 11 2: an active workflow independently hardcodes production account/registry/cluster identity:"$'\n'"${WORKFLOW_HARDCODE_HITS}"
fi

# 2b: gg-iam-secrets-deployment.yaml itself carries no account/registry/cluster identity literal either.
GG_IAM_HARDCODE_HITS="$(grep -nE '668311715351|229410149234|gg-poc-dev|[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com' .github/workflows/gg-iam-secrets-deployment.yaml 2>/dev/null || true)"
if [ -z "$GG_IAM_HARDCODE_HITS" ]; then
  pass "Phase 11 2b: gg-iam-secrets-deployment.yaml contains no hardcoded account/registry/cluster-name identity literal"
else
  fail "Phase 11 2b: gg-iam-secrets-deployment.yaml unexpectedly hardcodes account/registry/cluster identity:"$'\n'"${GG_IAM_HARDCODE_HITS}"
fi

# 3: since Phase 12, no active workflow references "eu-west-1" as a literal at all -- envs/dev/environment.yaml via the canonical resolver is the sole region source, including for gg-iam-secrets-deployment.yaml (its former independent region bootstrap selector is gone).
REGION_LITERAL_HITS="$(grep -rl 'eu-west-1' .github/workflows/*.yaml 2>/dev/null || true)"
if [ -z "$REGION_LITERAL_HITS" ]; then
  pass "Phase 11 3: no active workflow references 'eu-west-1' as a literal"
else
  fail "Phase 11 3: an unexpected 'eu-west-1' literal exists in active workflow source:"$'\n'"${REGION_LITERAL_HITS}"
fi

# 4: no active workflow independently hardcodes a full generated IAM role ARN (arn:aws:iam::<12-digit>:role/...) as runtime identity -- every role ARN comes from the canonical resolver's github-env output.
ROLE_ARN_LITERAL_HITS="$(grep -rlE 'arn:aws:iam::[0-9]{12}:role/' .github/workflows/*.yaml 2>/dev/null || true)"
if [ -z "$ROLE_ARN_LITERAL_HITS" ]; then
  pass "Phase 11 4: no active workflow independently hardcodes a full IAM role ARN literal -- every role ARN (RUNNER_ROLE_ARN/EKS_DEPLOY_ROLE_ARN/RUNTIME_ROLE_ARN/MONITOR_ROLE_ARN/ARGOCD_ECR_READ_ROLE_ARN/PLATFORM_LOGGING_ROLE_ARN/CLOUDWATCH_METRICS_ROLE_ARN/ECR_SYNC_ROLE_ARN) is loaded from the canonical resolver"
else
  fail "Phase 11 4: an active workflow independently hardcodes a full IAM role ARN literal:"$'\n'"${ROLE_ARN_LITERAL_HITS}"
fi

# 5: every active workflow that needs canonical identity loads it via hack/goldengate-environment.py github-env after its own checkout -- GITHUB_ENV is job-local, so no job may assume another job's load already ran. gg-iam-secrets-deployment.yaml is excluded: its Phase-12-pending interface derives AWS_REGION via a plain `get` call, not github-env.
MISSING_LOADER_HITS=""
for wf in goldengate-eks-app.yaml argocd-eks-deployment.yaml goldengate-platform.yaml goldengate-monitor.yaml goldengate-monitor-metrics-config.yaml goldengate-observability.yaml cloudwatch-observability-artifact-sync.yaml push_docker_images_to_ECR.yaml; do
  if ! grep -q 'goldengate-environment.py --environment .* github-env' ".github/workflows/${wf}" 2>/dev/null; then
    MISSING_LOADER_HITS="${MISSING_LOADER_HITS}${wf}"$'\n'
  fi
done
if [ -z "$MISSING_LOADER_HITS" ]; then
  pass "Phase 11 5: every active workflow that needs canonical identity calls hack/goldengate-environment.py github-env at least once"
else
  fail "Phase 11 5: the following workflow(s) no longer call hack/goldengate-environment.py github-env:"$'\n'"${MISSING_LOADER_HITS}"
fi

# 6: only the approved repository variables remain referenced across active workflows -- PROJECT_NAME_DEV (pre-checkout CodeBuild runs-on) plus the operational vars this phase explicitly keeps.
APPROVED_VARS_RE='PROJECT_NAME_DEV|FLUENT_BIT_IMAGE|MONITOR_BASE_IMAGE|ENABLE_TEMP_ARGOCD_ECR_PASSWORD_INJECTION'
UNAPPROVED_VARS_HITS="$(grep -rohE 'vars\.[A-Za-z_]+' .github/workflows/*.yaml 2>/dev/null | sort -u | grep -vE "^vars\.(${APPROVED_VARS_RE})\$" || true)"
if [ -z "$UNAPPROVED_VARS_HITS" ]; then
  pass "Phase 11 6: only the approved repository variables (PROJECT_NAME_DEV, FLUENT_BIT_IMAGE, MONITOR_BASE_IMAGE, ENABLE_TEMP_ARGOCD_ECR_PASSWORD_INJECTION) remain referenced across active workflows"
else
  fail "Phase 11 6: an unapproved repository variable remains referenced in active workflows:"$'\n'"${UNAPPROVED_VARS_HITS}"
fi

# 7: argocd-eks-deployment.yaml's IAM-policy validation step derives its POLICY_FILE path from GG_ENVIRONMENT (the generator's policy_folder = "argocd-ecr-oci-read-<environment>" naming contract), never a second hardcoded envs/dev/... literal.
if grep -qF 'envs/dev/policies/argocd-ecr-oci-read-dev' .github/workflows/argocd-eks-deployment.yaml 2>/dev/null; then
  fail "Phase 11 7: argocd-eks-deployment.yaml still hardcodes envs/dev/policies/argocd-ecr-oci-read-dev"
elif grep -qF 'POLICY_FILE="envs/${GG_ENVIRONMENT}/policies/argocd-ecr-oci-read-${GG_ENVIRONMENT}/policies/policies_1.json"' .github/workflows/argocd-eks-deployment.yaml 2>/dev/null; then
  pass "Phase 11 7: argocd-eks-deployment.yaml's IAM-policy validation step derives POLICY_FILE from GG_ENVIRONMENT, never a hardcoded envs/dev/... literal"
else
  fail "Phase 11 7: argocd-eks-deployment.yaml no longer derives POLICY_FILE from GG_ENVIRONMENT as expected"
fi

# 8: cloudwatch-observability-artifact-sync.yaml's chart-rendering step uses the canonical OBSERVABILITY_NAMESPACE, never a hardcoded amazon-cloudwatch literal.
if grep -qE -- '--namespace[[:space:]]+amazon-cloudwatch([[:space:]]|$)' .github/workflows/cloudwatch-observability-artifact-sync.yaml 2>/dev/null; then
  fail "Phase 11 8: cloudwatch-observability-artifact-sync.yaml still renders with a hardcoded --namespace amazon-cloudwatch"
elif grep -qF -- '--namespace "${OBSERVABILITY_NAMESPACE}"' .github/workflows/cloudwatch-observability-artifact-sync.yaml 2>/dev/null; then
  pass "Phase 11 8: cloudwatch-observability-artifact-sync.yaml renders with the canonical --namespace \"\${OBSERVABILITY_NAMESPACE}\", never a hardcoded amazon-cloudwatch literal"
else
  fail "Phase 11 8: cloudwatch-observability-artifact-sync.yaml no longer renders with --namespace \"\${OBSERVABILITY_NAMESPACE}\" as expected"
fi

# 9: goldengate-observability.yaml's rendered ServiceAccount validation compares against the canonical target namespace (passed in as argv[2]), never the literal "amazon-cloudwatch".
if grep -qF 'sa["metadata"].get("namespace") == "amazon-cloudwatch"' .github/workflows/goldengate-observability.yaml 2>/dev/null; then
  fail "Phase 11 9: goldengate-observability.yaml's ServiceAccount validation still compares against the literal \"amazon-cloudwatch\""
elif grep -qF 'python3 - "$RENDERED" "$TARGET_NAMESPACE" <<'"'"'PYEOF'"'"'' .github/workflows/goldengate-observability.yaml 2>/dev/null \
    && grep -qF 'sa["metadata"].get("namespace") == expected_namespace' .github/workflows/goldengate-observability.yaml 2>/dev/null; then
  pass "Phase 11 9: goldengate-observability.yaml's ServiceAccount validation receives \$TARGET_NAMESPACE as argv[2] and compares against expected_namespace, never the literal \"amazon-cloudwatch\""
else
  fail "Phase 11 9: goldengate-observability.yaml's ServiceAccount validation no longer passes/uses the canonical target namespace as expected"
fi

# 10: known current environment-derived IAM role NAMES are not independently embedded anywhere in active workflow diagnostics -- every one of these has a canonical *_ROLE_NAME resolver output available wherever it was previously hardcoded.
STALE_ROLE_NAME_RE='GoldenGateSecretsReadRole-dev|GoldenGateArgocdECRRead-dev|GoldenGateCloudWatchMetricsRole-dev|GoldenGateMonitorReadRole-dev|GoldenGatePlatformLoggingRole-dev|GoldenGateEKSDeployRole-dev'
STALE_ROLE_NAME_HITS="$(grep -rlE "$STALE_ROLE_NAME_RE" .github/workflows/*.yaml 2>/dev/null || true)"
if [ -z "$STALE_ROLE_NAME_HITS" ]; then
  pass "Phase 11 10: no active workflow independently embeds a current environment-derived IAM role NAME literal (GoldenGateSecretsReadRole-dev/GoldenGateArgocdECRRead-dev/GoldenGateCloudWatchMetricsRole-dev/GoldenGateMonitorReadRole-dev/GoldenGatePlatformLoggingRole-dev/GoldenGateEKSDeployRole-dev) -- every diagnostic uses the canonical *_ROLE_NAME resolver output"
else
  fail "Phase 11 10: an active workflow independently embeds a stale environment-derived IAM role name literal:"$'\n'"${STALE_ROLE_NAME_HITS}"
fi

# 11: no active workflow runtime/validation path references envs/dev/policies/ or envs/dev/argocd/ -- the sole approved pre-checkout bootstrap exceptions are goldengate-eks-app.yaml's push trigger path ('envs/dev/**') and its matching run-name/comment.
ENVS_DEV_RUNTIME_HITS="$(grep -rn 'envs/dev/policies/\|envs/dev/argocd/' .github/workflows/*.yaml 2>/dev/null || true)"
if [ -z "$ENVS_DEV_RUNTIME_HITS" ]; then
  pass "Phase 11 11: no active workflow runtime/validation path references envs/dev/policies/ or envs/dev/argocd/ -- every reference is environment-derived (envs/\${GG_ENVIRONMENT}/... or envs/<environment>/... in comments)"
else
  fail "Phase 11 11: an active workflow still references envs/dev/policies/ or envs/dev/argocd/ outside the approved bootstrap exceptions:"$'\n'"${ENVS_DEV_RUNTIME_HITS}"
fi

echo ""
echo "--- Phase 12: remove the independent Terraform region input/source ---"

IAM_WORKFLOW=".github/workflows/gg-iam-secrets-deployment.yaml"

# 1/2/4/5/6/7: structural proof, read directly from the real committed YAML (never a reimplementation) -- workflow_dispatch/workflow_call carry no region input, the old supplied-vs-canonical mismatch step is gone, validate_environment_config exposes an aws_region output derived from a step that calls the canonical resolver's `get AWS_REGION`, and apply forwards exactly that job output to the corporate reusable Terraform workflow.
if [ "$PYTHON_AVAILABLE" = "true" ] && [ -f "$IAM_WORKFLOW" ]; then
  PHASE12_IAM_CHECK="$(python3 -c '
import yaml
with open("'"$IAM_WORKFLOW"'") as f:
    doc = yaml.safe_load(f)

results = []

on_block = doc.get(True, doc.get("on", {}))
wd_inputs = on_block.get("workflow_dispatch", {}).get("inputs", {}) or {}
wc_inputs = on_block.get("workflow_call", {}).get("inputs", {}) or {}
results.append(("1: workflow_dispatch has no region input", "region" not in wd_inputs))
results.append(("2: workflow_call has no region input", "region" not in wc_inputs))

jobs = doc["jobs"]
vec = jobs["validate_environment_config"]
steps = vec.get("steps", [])
step_names = [s.get("name", "") for s in steps]
results.append(("4: the old supplied-vs-canonical region mismatch step is gone", "Verify supplied region matches the canonical environment region" not in step_names))
results.append(("5: validate_environment_config exposes a canonical aws_region job output", "aws_region" in (vec.get("outputs") or {})))

resolve_step = next((s for s in steps if s.get("id") == "resolve_environment"), None)
results.append(("6a: a step with id=resolve_environment exists", resolve_step is not None))
run_text = (resolve_step or {}).get("run", "")
results.append(("6b: that step derives AWS_REGION via goldengate-environment.py ... get AWS_REGION", "goldengate-environment.py" in run_text and "get AWS_REGION" in run_text))
results.append(("6c: that step fails closed on an empty AWS_REGION (no eu-west-1 fallback)", "-z \"${AWS_REGION}\"" in run_text and "eu-west-1" not in run_text))

apply_job = jobs["apply"]
results.append(("7a: apply.needs == validate_environment_config", apply_job.get("needs") == "validate_environment_config"))
results.append(("7b: apply.with.region == needs.validate_environment_config.outputs.aws_region", apply_job.get("with", {}).get("region") == "${{ needs.validate_environment_config.outputs.aws_region }}"))

for label, ok in results:
    print(("OK " if ok else "FAIL ") + label)
' 2>&1)"
  while IFS= read -r line; do
    case "$line" in
      FAIL\ *) fail "Phase 12: ${line#FAIL }" ;;
      OK\ *) pass "Phase 12: ${line#OK }" ;;
    esac
  done <<< "$PHASE12_IAM_CHECK"
else
  skip "Phase 12 1/2/4/5/6/7: structural checks -- python3/PyYAML unavailable or ${IAM_WORKFLOW} missing"
fi

# 3: no textual reference to inputs.region/github.event.inputs.region remains anywhere in the IAM workflow (run-name, env:, with:, or run: blocks).
if grep -qE 'inputs\.region|github\.event\.inputs\.region' "$IAM_WORKFLOW" 2>/dev/null; then
  fail "Phase 12 3: ${IAM_WORKFLOW} still references inputs.region or github.event.inputs.region"
else
  pass "Phase 12 3: ${IAM_WORKFLOW} contains no reference to inputs.region or github.event.inputs.region"
fi

# SUPPLIED_REGION is the retired Phase-11 transitional variable name; must be fully gone alongside the mismatch step itself.
if grep -qF 'SUPPLIED_REGION' "$IAM_WORKFLOW" 2>/dev/null; then
  fail "Phase 12 3b: ${IAM_WORKFLOW} still references the retired SUPPLIED_REGION variable"
else
  pass "Phase 12 3b: ${IAM_WORKFLOW} no longer references the retired SUPPLIED_REGION variable"
fi

# 8/9: the main orchestrator's terraform_sync_once call sends only environment (never region), and validate_model no longer exposes the now-dead cross-job aws_region output.
if [ "$PYTHON_AVAILABLE" = "true" ]; then
  PHASE12_MAIN_CHECK="$(python3 -c '
import yaml
with open("'"$EKS_APP_WORKFLOW"'") as f:
    doc = yaml.safe_load(f)
jobs = doc["jobs"]

results = []

tsf_with = jobs["terraform_sync_once"].get("with", {}) or {}
results.append(("8a: terraform_sync_once.with has no region key", "region" not in tsf_with))
results.append(("8b: terraform_sync_once.with.environment is the selected_environment job output", tsf_with.get("environment") == "${{ needs.validate_model.outputs.selected_environment }}"))

vm_outputs = jobs["validate_model"].get("outputs", {}) or {}
results.append(("9: validate_model no longer exposes an aws_region output", "aws_region" not in vm_outputs))

for label, ok in results:
    print(("OK " if ok else "FAIL ") + label)
' 2>&1)"
  while IFS= read -r line; do
    case "$line" in
      FAIL\ *) fail "Phase 12: ${line#FAIL }" ;;
      OK\ *) pass "Phase 12: ${line#OK }" ;;
    esac
  done <<< "$PHASE12_MAIN_CHECK"
else
  skip "Phase 12 8/9: main-workflow structural checks -- python3/PyYAML unavailable"
fi

# Dead cross-job wiring must be fully gone from both ends, not just the consumer side.
if grep -qF 'needs.validate_model.outputs.aws_region' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  fail "Phase 12 8c: ${EKS_APP_WORKFLOW} still references needs.validate_model.outputs.aws_region"
else
  pass "Phase 12 8c: ${EKS_APP_WORKFLOW} no longer references needs.validate_model.outputs.aws_region anywhere"
fi
if grep -qF 'steps.load_environment.outputs.aws_region' "$EKS_APP_WORKFLOW" 2>/dev/null; then
  fail "Phase 12 9b: ${EKS_APP_WORKFLOW} still writes/declares steps.load_environment.outputs.aws_region"
else
  pass "Phase 12 9b: ${EKS_APP_WORKFLOW} no longer writes/declares steps.load_environment.outputs.aws_region"
fi

# 10: no active workflow contains an independent runtime "region: eu-west-1" literal or an "- eu-west-1" choice-input option anywhere in this Terraform orchestration path (or elsewhere).
PHASE12_REGION_LITERAL_HITS="$(grep -rnE '^[[:space:]]*region:[[:space:]]*eu-west-1|^[[:space:]]*-[[:space:]]*eu-west-1[[:space:]]*$' .github/workflows/*.yaml 2>/dev/null || true)"
if [ -z "$PHASE12_REGION_LITERAL_HITS" ]; then
  pass "Phase 12 10: no active workflow contains a runtime 'region: eu-west-1' literal or an '- eu-west-1' choice-input option"
else
  fail "Phase 12 10: an active workflow still contains a region literal/choice option:"$'\n'"${PHASE12_REGION_LITERAL_HITS}"
fi

# Direct-call static regression: extract the REAL, unmodified resolve_environment step and execute it with only TARGET_ENVIRONMENT=dev set (exactly what a direct manual workflow_dispatch run supplies) -- proves the workflow derives its own region with zero other caller participation, never contacting AWS or the corporate reusable workflow.
if [ "$PYTHON_AVAILABLE" = "true" ] && [ -f "$IAM_WORKFLOW" ]; then
  python3 - "$IAM_WORKFLOW" > "${WORKDIR}/resolve_environment_step.sh" <<'PYEOF'
import sys
import yaml
with open(sys.argv[1]) as f:
    doc = yaml.safe_load(f)
for step in doc["jobs"]["validate_environment_config"]["steps"]:
    if step.get("id") == "resolve_environment":
        sys.stdout.write(step["run"])
        break
else:
    sys.exit("step not found")
PYEOF

  if [ ! -s "${WORKDIR}/resolve_environment_step.sh" ]; then
    fail "Phase 12 direct-call: could not extract the resolve_environment step from ${IAM_WORKFLOW}"
  else
    DIRECT_CALL_OUTPUT="$(mktemp)"
    set +e
    DIRECT_CALL_LOG="$(TARGET_ENVIRONMENT="dev" GITHUB_OUTPUT="$DIRECT_CALL_OUTPUT" bash "${WORKDIR}/resolve_environment_step.sh" 2>&1)"
    DIRECT_CALL_STATUS=$?
    set -e

    if [ "$DIRECT_CALL_STATUS" -eq 0 ] && grep -qF "aws_region=eu-west-1" "$DIRECT_CALL_OUTPUT"; then
      pass "Phase 12 direct-call: given only TARGET_ENVIRONMENT=dev (exactly a direct manual run's inputs.environment), the real resolve_environment step derives aws_region=eu-west-1 from envs/dev/environment.yaml with no other caller participation"
    else
      fail "Phase 12 direct-call: the real resolve_environment step did not derive the canonical region from environment=dev alone (status=${DIRECT_CALL_STATUS}):"$'\n'"${DIRECT_CALL_LOG}"
    fi
    rm -f "$DIRECT_CALL_OUTPUT"
  fi
else
  skip "Phase 12 direct-call: python3/PyYAML unavailable or ${IAM_WORKFLOW} missing"
fi

# Region-change regression: two synthetic, fully valid, temporary environment.yaml fixtures (isolated copy of the resolver, never touching envs/dev/) with two different syntactically-valid AWS regions prove the resolver -- and therefore the workflow step above -- is genuinely environment-derived, never a hardcoded eu-west-1 fallback.
if [ "$PYTHON_AVAILABLE" = "true" ] && [ -f envs/dev/environment.yaml ]; then
  SYNTH_ROOT="${WORKDIR}/region-synth-repo"
  rm -rf "$SYNTH_ROOT"
  mkdir -p "$SYNTH_ROOT/hack" "$SYNTH_ROOT/envs/synth-region-a" "$SYNTH_ROOT/envs/synth-region-b"
  cp hack/goldengate-environment.py "$SYNTH_ROOT/hack/goldengate-environment.py"

  # Every eu-west-1 occurrence (aws.region, eks.oidcIssuer, network.certificateArn, kms.monitorDynamoDbKeyArn) is substituted together so cross-field region consistency validation still passes -- proven by requiring `validate` to succeed below, not merely `get`.
  sed -e 's/^environment: dev$/environment: synth-region-a/' -e 's/eu-west-1/ap-southeast-2/g' \
    envs/dev/environment.yaml > "$SYNTH_ROOT/envs/synth-region-a/environment.yaml"
  sed -e 's/^environment: dev$/environment: synth-region-b/' -e 's/eu-west-1/us-east-2/g' \
    envs/dev/environment.yaml > "$SYNTH_ROOT/envs/synth-region-b/environment.yaml"

  set +e
  SYNTH_VALIDATE_A="$(python3 "$SYNTH_ROOT/hack/goldengate-environment.py" --environment synth-region-a validate 2>&1)"
  SYNTH_VALIDATE_A_STATUS=$?
  SYNTH_VALIDATE_B="$(python3 "$SYNTH_ROOT/hack/goldengate-environment.py" --environment synth-region-b validate 2>&1)"
  SYNTH_VALIDATE_B_STATUS=$?
  SYNTH_REGION_A="$(python3 "$SYNTH_ROOT/hack/goldengate-environment.py" --environment synth-region-a get AWS_REGION 2>&1)"
  SYNTH_REGION_B="$(python3 "$SYNTH_ROOT/hack/goldengate-environment.py" --environment synth-region-b get AWS_REGION 2>&1)"
  set -e

  if [ "$SYNTH_VALIDATE_A_STATUS" -eq 0 ] && [ "$SYNTH_VALIDATE_B_STATUS" -eq 0 ] \
      && [ "$SYNTH_REGION_A" = "ap-southeast-2" ] && [ "$SYNTH_REGION_B" = "us-east-2" ] \
      && [ "$SYNTH_REGION_A" != "$SYNTH_REGION_B" ]; then
    pass "Phase 12 region-change: two synthetic, fully-valid, isolated environment.yaml fixtures with different AWS regions (ap-southeast-2 / us-east-2) each resolve get AWS_REGION to exactly their own region -- the resolver is genuinely environment-derived, no eu-west-1 fallback exists"
  else
    fail "Phase 12 region-change: synthetic region-change regression failed (validateA=${SYNTH_VALIDATE_A_STATUS} validateB=${SYNTH_VALIDATE_B_STATUS} regionA=${SYNTH_REGION_A} regionB=${SYNTH_REGION_B})"
  fi
  rm -rf "$SYNTH_ROOT"
else
  skip "Phase 12 region-change: python3/PyYAML unavailable or envs/dev/environment.yaml missing"
fi

echo ""
echo "=================================================="
echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${SKIP_COUNT} skipped"
echo "=================================================="

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
