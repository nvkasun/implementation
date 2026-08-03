#!/usr/bin/env bash
set -euo pipefail

# Orchestration/regression script for the GoldenGate EKS repository.
#
# Runs static parsing/Helm/Python checks derived from the ONE canonical
# deployment source (envs/dev/goldengate-deployments.yaml) -- no runtime
# names are hardcoded here. Detailed behavior tests live in the Python
# unit-test suites (monitoring/monitor/tests/).
#
# Does not deploy, does not touch the cluster, does not require AWS
# credentials.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# This script's own python3 invocations must never create __pycache__/*.pyc
# -- that would make the "no committed pycache" check below self-defeating.
export PYTHONDONTWRITEBYTECODE=1

CANONICAL_CONFIG="envs/dev/goldengate-deployments.yaml"
RUNTIME_CHART="helm/goldengate"
PLATFORM_CHART="helm/goldengate-platform"
MONITOR_CHART="helm/goldengate-monitor"
MONITOR_APP_DIR="monitoring/monitor"
MONITOR_WORKFLOW=".github/workflows/goldengate-monitor.yaml"
EKS_APP_WORKFLOW=".github/workflows/goldengate-eks-app.yaml"
PLATFORM_WORKFLOW=".github/workflows/goldengate-platform.yaml"
DETECT_SCRIPT="hack/detect-goldengate-deployments.sh"
INVENTORY_SCRIPT="hack/inventory-goldengate-legacy-resources.sh"
INVENTORY_WORKFLOW=".github/workflows/goldengate-legacy-cleanup-inventory.yaml"
OBSERVABILITY_VALUES_FILE="platform/dev/goldengate-observability/values.yaml"
OBSERVABILITY_WORKFLOW=".github/workflows/goldengate-observability.yaml"
ARGOCD_VALUES_FILE="envs/dev/argocd/values.yaml"
ARGOCD_DEPLOY_WORKFLOW=".github/workflows/argocd-eks-deployment.yaml"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

pass() { echo "PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL: $1"; FAIL_COUNT=$((FAIL_COUNT + 1)); }
skip() { echo "SKIP: $1"; SKIP_COUNT=$((SKIP_COUNT + 1)); }

HELM_AVAILABLE="false"
command -v helm >/dev/null 2>&1 && HELM_AVAILABLE="true"

PYTHON_AVAILABLE="false"
if command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
  PYTHON_AVAILABLE="true"
fi

echo "=================================================="
echo "GoldenGate repository regression"
echo "=================================================="
echo "Repository root: ${REPO_ROOT}"
echo "Helm available:  ${HELM_AVAILABLE}"
echo "Python3+PyYAML available: ${PYTHON_AVAILABLE}"
echo ""

# ---------------------------------------------------------------------
# 1. Strict YAML parsing + duplicate-key rejection for the canonical config.
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 2. Both runtimes remain enabled (source of truth: canonical config AND
#    each runtime's own environment values file).
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 3. Python unit tests (monitoring/monitor).
# ---------------------------------------------------------------------
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
  # py_compile writes __pycache__/*.pyc regardless of
  # PYTHONDONTWRITEBYTECODE (it is an explicit compile request) -- clean up
  # immediately so this script's own run never leaves artifacts behind.
  find "$MONITOR_APP_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
else
  skip "py_compile -- python3 not available"
fi

# ---------------------------------------------------------------------
# 4. Helm lint: runtime chart, platform chart, monitor chart.
# ---------------------------------------------------------------------
echo ""
echo "--- Helm lint ---"
if [ "$HELM_AVAILABLE" = "true" ]; then
  # helm/goldengate's deploymentModel value has no usable default (it is ""
  # in values.yaml) and goldengate.assertSupportedDeploymentModel fires
  # unconditionally at render time -- lint the same way the real workflow
  # always does: against a real canonical deployment values file (which
  # declares deploymentModel: singleRuntime itself), never bare/values-less.
  # (helm lint does not propagate a template "fail" call as a non-zero exit
  # by itself, so a bare, values-less invocation here would not actually
  # exercise or prove anything about the assertion either way -- using a
  # canonical values file keeps this check meaningful and representative of
  # how the chart is actually linted in production.)
  if helm lint "$RUNTIME_CHART" -f "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" --set global.environment=dev >"${WORKDIR}/lint-runtime.log" 2>&1; then
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

  # Phase 6A: centralized container logging (platform Fluent Bit
  # DaemonSet). Essential, focused checks only -- same real dev values file
  # and --set-string role-ARN/region/image injection pattern the actual
  # goldengate-platform.yaml workflow uses, not a fake Kubernetes/AWS
  # environment. The digest below is the real, verified private ECR digest
  # supplied via the FLUENT_BIT_IMAGE repository variable.
  PLATFORM_DEV_VALUES="${REPO_ROOT}/platform/dev/goldengate-platform/values.yaml"
  FAKE_ORACLE_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGateSecretsReadRole-dev"
  FAKE_FLUENT_BIT_ROLE_ARN="arn:aws:iam::668311715351:role/GoldenGatePlatformLoggingRole-dev"
  FAKE_FLUENT_BIT_IMAGE="229410149234.dkr.ecr.eu-west-1.amazonaws.com/aws-cloud-factory-fluent-bit@sha256:366923ffc51dfde4966e743dcbd4ca05211b733d4f69c7591903bc7660fbf243"
  if helm lint "$PLATFORM_CHART" \
      --values "$PLATFORM_DEV_VALUES" \
      --set-string serviceAccounts.oracle.roleArn="$FAKE_ORACLE_ROLE_ARN" \
      --set-string serviceAccounts.postgresql.roleArn="$FAKE_ORACLE_ROLE_ARN" \
      --set-string fluentBit.serviceAccount.roleArn="$FAKE_FLUENT_BIT_ROLE_ARN" \
      --set-string fluentBit.aws.region="eu-west-1" \
      --set-string fluentBit.image.reference="$FAKE_FLUENT_BIT_IMAGE" \
      >"${WORKDIR}/lint-platform-fluentbit.log" 2>&1; then
    pass "helm lint ${PLATFORM_CHART} (dev values, fluentBit.create=true, private digest image)"
  else
    fail "helm lint ${PLATFORM_CHART} (dev values, fluentBit.create=true, private digest image)"
    cat "${WORKDIR}/lint-platform-fluentbit.log"
  fi

  PLATFORM_FLUENTBIT_RENDERED="${WORKDIR}/platform-fluentbit-rendered.yaml"
  if helm template goldengate-dev-platform "$PLATFORM_CHART" \
      --values "$PLATFORM_DEV_VALUES" \
      --set-string serviceAccounts.oracle.roleArn="$FAKE_ORACLE_ROLE_ARN" \
      --set-string serviceAccounts.postgresql.roleArn="$FAKE_ORACLE_ROLE_ARN" \
      --set-string fluentBit.serviceAccount.roleArn="$FAKE_FLUENT_BIT_ROLE_ARN" \
      --set-string fluentBit.aws.region="eu-west-1" \
      --set-string fluentBit.image.reference="$FAKE_FLUENT_BIT_IMAGE" \
      > "$PLATFORM_FLUENTBIT_RENDERED" 2>"${WORKDIR}/template-platform-fluentbit.log"; then
    pass "helm template ${PLATFORM_CHART} (dev values, fluentBit.create=true, private digest image) renders"
  else
    fail "helm template ${PLATFORM_CHART} (dev values, fluentBit.create=true, private digest image) renders"
    cat "${WORKDIR}/template-platform-fluentbit.log"
  fi

  # The chart must fail clearly (not silently fall back to any image) when
  # fluentBit.create=true and no image reference is supplied at all.
  if helm template goldengate-dev-platform "$PLATFORM_CHART" \
      --values "$PLATFORM_DEV_VALUES" \
      --set-string serviceAccounts.oracle.roleArn="$FAKE_ORACLE_ROLE_ARN" \
      --set-string serviceAccounts.postgresql.roleArn="$FAKE_ORACLE_ROLE_ARN" \
      --set-string fluentBit.serviceAccount.roleArn="$FAKE_FLUENT_BIT_ROLE_ARN" \
      --set-string fluentBit.aws.region="eu-west-1" \
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

    # Deployment image reference: exact, private, immutable digest -- never
    # public.ecr.aws, never a mutable tag.
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

    # Deterministic per-namespace tag routing (Phase 6A log-routing
    # correction): live verification found the previous single-Tail-input
    # + grep FILTER + rewrite_tag design silently dropped every record
    # (tail=1032, kubernetes=1032, record_modifier=1032, grep=1032,
    # grep_dropped=1032, rewrite_tag=0, cloudwatch proc_records=0) because
    # the grep filter's $kubernetes['namespace_name'] match depended on
    # Kubernetes-metadata enrichment that had not completed. Replaced with
    # two independent, deterministic Tail inputs (runtime.*, monitor.*),
    # each Path-restricted to its own namespace's container-log filename
    # convention, each enriched by its own kubernetes FILTER (explicit
    # Kube_Tag_Prefix, enrichment only -- never a routing dependency), and
    # each OUTPUT matching directly on its own input's tag.
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

    # Bounded filesystem buffering: both cloudwatch_logs OUTPUTs carry
    # storage.total_limit_size, and the fluent-bit-state emptyDir carries a
    # sizeLimit -- distinct from (and in addition to) the pre-existing
    # Mem_Buf_Limit/storage.max_chunks_up/storage.backlog.mem_limit memory
    # and in-flight backlog controls, which bound something different (RAM
    # and in-flight chunks, not the total on-disk buffer directory size).
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

  # Private-image-reference format validation: exercise the exact same
  # regex the workflow's "Validate FLUENT_BIT_IMAGE format" step uses,
  # confirming the real digest passes and representative malformed values
  # (tag-based, public.ecr.aws, wrong repository/account, malformed digest)
  # are all rejected.
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

  # No GoldenGate runtime sidecar: the runtime chart itself (never touched
  # by Phase 6A) must still define exactly one application container and
  # exactly one init container.
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

  # IAM least privilege: the new logging policy must contain exactly the
  # required log-writing actions and nothing else (no CreateLogGroup/
  # DeleteLogGroup, no alarms, no DynamoDB/Secrets Manager/EFS/Kubernetes
  # control permissions).
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

  # CloudWatch Logs encryption correction: no kms_key_id (guessed or
  # otherwise) may be set on either log group -- both groups must rely on
  # CloudWatch Logs' own default server-side encryption until an approved
  # customer-managed KMS key ARN is actually supplied. The two log groups
  # and their retention/tags remain otherwise unchanged.
  CLOUDWATCH_LOGS_TF="${REPO_ROOT}/envs/dev/cloudwatch_logs.tf"
  if [ -f "$CLOUDWATCH_LOGS_TF" ]; then
    if grep -v '^\s*#' "$CLOUDWATCH_LOGS_TF" | grep -qE 'kms_key_id\s*='; then
      fail "envs/dev/cloudwatch_logs.tf still sets kms_key_id -- must rely on CloudWatch Logs default server-side encryption only"
    else
      pass "envs/dev/cloudwatch_logs.tf sets no kms_key_id -- relies on CloudWatch Logs default server-side encryption"
    fi
    if grep -q '"/adcb/goldengate/dev/runtime"' "$CLOUDWATCH_LOGS_TF" && grep -q '"/adcb/goldengate/dev/monitor"' "$CLOUDWATCH_LOGS_TF" \
        && grep -q 'retention_in_days' "$CLOUDWATCH_LOGS_TF"; then
      pass "envs/dev/cloudwatch_logs.tf still defines both log groups with retention configured"
    else
      fail "envs/dev/cloudwatch_logs.tf no longer defines both expected log groups with retention"
    fi
  else
    fail "${CLOUDWATCH_LOGS_TF} not found"
  fi

  # ---------------------------------------------------------------------
  # Phase 6B2A: GoldenGateCloudWatchMetricsRole-dev IAM/Terraform
  # prerequisites (IAM only -- no Kubernetes/Argo CD resource of any kind
  # is created in this phase, so there is nothing Kubernetes-shaped to
  # assert here).
  # ---------------------------------------------------------------------
  IAM_TF="${REPO_ROOT}/envs/dev/iam.tf"
  if [ -f "$IAM_TF" ]; then
    if grep -q 'module "goldengate_cloudwatch_metrics_role_dev"' "$IAM_TF"; then
      pass "envs/dev/iam.tf contains module goldengate_cloudwatch_metrics_role_dev"
    else
      fail "envs/dev/iam.tf is missing module goldengate_cloudwatch_metrics_role_dev"
    fi

    # Extract just this module's block (from its opening line to the next
    # top-level '}' at column 0) so the name/policy_folder/managed_policy_arns
    # checks below cannot accidentally match a different module.
    CLOUDWATCH_METRICS_MODULE_BLOCK="$(awk '/^module "goldengate_cloudwatch_metrics_role_dev" \{/{f=1} f{print} f && /^}$/{exit}' "$IAM_TF")"
    if echo "$CLOUDWATCH_METRICS_MODULE_BLOCK" | grep -q '"GoldenGateCloudWatchMetricsRole-dev"' \
        && echo "$CLOUDWATCH_METRICS_MODULE_BLOCK" | grep -q 'policy_folder = "goldengate-cloudwatch-metrics-dev"' \
        && echo "$CLOUDWATCH_METRICS_MODULE_BLOCK" | grep -q 'managed_policy_arns = \[\]'; then
      pass "goldengate_cloudwatch_metrics_role_dev uses name=GoldenGateCloudWatchMetricsRole-dev, policy_folder=goldengate-cloudwatch-metrics-dev, managed_policy_arns=[]"
    else
      fail "goldengate_cloudwatch_metrics_role_dev module block does not contain the expected name/policy_folder/managed_policy_arns"
    fi

    # No direct aws_iam_* resource anywhere in this file -- every role in
    # this environment (including the new one) must go through the
    # existing ADCB Terraform module pattern, never a raw resource block.
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
    CW_TRUST_CHECK="$(python3 - "$CW_METRICS_TRUST_FILE" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
stmts = doc.get("Statement")
if not isinstance(stmts, list) or len(stmts) != 1:
    print("MISMATCH:not-exactly-one-statement")
    raise SystemExit
s = stmts[0]
principal = s.get("Principal", {})
federated = principal.get("Federated", "")
if "arn:aws:iam::668311715351:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/407C4385FF87947926730569F1E564FB" != federated:
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
ns_cond = put_metric_stmt.get("Condition", {}).get("StringEquals", {}).get("cloudwatch:namespace")
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

  CLOUDWATCH_OBSERVABILITY_TF="${REPO_ROOT}/envs/dev/cloudwatch_observability.tf"
  if [ -f "$CLOUDWATCH_OBSERVABILITY_TF" ]; then
    if grep -q '"/aws/containerinsights/gg-poc-dev/performance"' "$CLOUDWATCH_OBSERVABILITY_TF" \
        && grep -q 'default\s*=\s*30' "$CLOUDWATCH_OBSERVABILITY_TF" \
        && grep -q 'goldengate_container_insights_retention_days' "$CLOUDWATCH_OBSERVABILITY_TF"; then
      pass "envs/dev/cloudwatch_observability.tf defines /aws/containerinsights/gg-poc-dev/performance with a 30-day default retention variable"
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

# Preservation check: every pre-existing repository ARN this policy already
# granted (goldengate, goldengate-monitor, goldengate-platform, gg-monitor)
# must still be present unchanged.
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

# ecr:GetAuthorizationToken statement (Resource "*") must be preserved
# unchanged.
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

  # Regression proof: the existing Fluent Bit log-group ARNs and policy
  # files are unchanged by this phase -- Phase 6B2A only adds a new role
  # and a new log group, it never touches GoldenGatePlatformLoggingRole-dev
  # or the /adcb/goldengate/dev/* groups.
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

  # ---------------------------------------------------------------------
  # Phase 6B2B: private-image-only CloudWatch Observability GitOps source
  # and deployment workflow. Static/offline only -- no AWS/Terraform/
  # kubectl/Argo CD/Git/network call of any kind.
  # ---------------------------------------------------------------------

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
check(v.get("clusterName"), "gg-poc-dev", "clusterName", results)
check(v.get("region"), "eu-west-1", "region", results)
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

private_domain = "229410149234.dkr.ecr.eu-west-1.amazonaws.com"
expected_repos = {
    "manager": "aws-cloud-factory-cloudwatch-agent-operator",
    "agent": "aws-cloud-factory-cloudwatch-agent",
    "kubeStateMetrics": "aws-cloud-factory-kube-state-metrics",
    "nodeExporter": "aws-cloud-factory-node-exporter",
}
found_repos = set()
for top_key, expected_repo in expected_repos.items():
    image = v.get(top_key, {}).get("image", {})
    check(image.get("repositoryDomainMap", {}).get("public"), private_domain, f"{top_key}.image.repositoryDomainMap.public", results)
    check(image.get("repository"), expected_repo, f"{top_key}.image.repository", results)
    found_repos.add(image.get("repository"))

if found_repos != set(expected_repos.values()):
    results.append(f"image-repository-set={sorted(found_repos)}")

values_text = open(sys.argv[1]).read()
for public_registry in ("public.ecr.aws", "registry.k8s.io", "quay.io", "docker.io", "ghcr.io", "gcr.io", "nvcr.io"):
    # Only flag a *live* (non-comment) reference -- this file's own
    # documentation comments legitimately mention these registries by name
    # to record what is NOT used, matching the established convention
    # elsewhere in this repository for negative-assertion prose.
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

  # 10: the Argo CD values file contains exactly four OCI repositories and
  # the exact new Secret name, with the pre-existing three preserved.
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

    # The four container-image repositories must never be added to Argo CD
    # token sync -- it only ever refreshes Helm OCI chart credentials.
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

    # The IAM-policy static-validation step's expected_repos dict must
    # include the fourth ARN.
    if grep -q 'helm/amazon-cloudwatch-observability.*arn:aws:ecr:eu-west-1:229410149234:repository/helm/amazon-cloudwatch-observability\|"helm/amazon-cloudwatch-observability": "arn:aws:ecr:eu-west-1:229410149234:repository/helm/amazon-cloudwatch-observability"' "${REPO_ROOT}/${ARGOCD_DEPLOY_WORKFLOW}"; then
      pass "11c: ${ARGOCD_DEPLOY_WORKFLOW}'s IAM-policy validation step expects the amazon-cloudwatch-observability repository ARN"
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
if "oci://" not in all_run_text or "helm/amazon-cloudwatch-observability" not in all_run_text:
    results.append("private-oci-chart-ref-not-referenced")
if "aws-observability" in all_run_text.lower() and "helm repo add" in all_run_text.lower():
    results.append("workflow-adds-public-helm-repo")
for repo in ("aws-cloud-factory-cloudwatch-agent-operator", "aws-cloud-factory-cloudwatch-agent",
             "aws-cloud-factory-kube-state-metrics", "aws-cloud-factory-node-exporter"):
    if repo not in all_run_text:
        results.append(f"missing-image-repo-reference:{repo}")
if "imageDigest" not in all_run_text and "imageDetails[0].imageDigest" not in all_run_text:
    results.append("no-digest-resolution")
if "GoldenGateCloudWatchMetricsRole-dev" not in all_run_text:
    results.append("missing-iam-role-reference")
# The Secret name is an env: block value (ARGOCD_OBSERVABILITY_SECRET_NAME),
# referenced in run: blocks only via that variable -- so this check must
# scan the whole document, not just run: block text.
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

    # \\? tolerates the workflow's own shell-regex source (e.g.
    # 'public\.ecr\.aws|...') where dots are backslash-escaped in the
    # literal file content, as well as a plain-text mention.
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

    # ---------------------------------------------------------------------
    # Phase 6B2B pre-deployment safety correction (focused, static/offline
    # only -- no AWS/Kubernetes/Argo CD/Git/network call).
    # ---------------------------------------------------------------------

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
    # Matches the Python dict-literal source the step embeds -- proves
    # "path" is the literal string "." (not merely "path" appearing
    # anywhere, and not a "chart:" field).
    if not re.search(r'"path"\s*:\s*"\."\s*,', run_text):
        results.append("oci-path-not-exactly-dot")
    if re.search(r'"chart"\s*:', run_text):
        results.append("unexpected-chart-field-present")
    if 'repoURL": "oci://229410149234.dkr.ecr.eu-west-1.amazonaws.com/helm/amazon-cloudwatch-observability"' not in run_text:
        results.append("repoURL-changed-or-missing")
    if '"targetRevision": "6.2.0"' not in run_text:
        results.append("targetRevision-changed-or-missing")

    # --- Correction 2: ignoreDifferences + RespectIgnoreDifferences -------
    if not re.search(r'"group"\s*:\s*""\s*,\s*\n\s*"kind"\s*:\s*"ServiceAccount"\s*,\s*\n\s*"name"\s*:\s*"cloudwatch-agent"\s*,\s*\n\s*"namespace"\s*:\s*"amazon-cloudwatch"', run_text):
        results.append("ignoreDifferences-rule-not-exact")
    if '/metadata/annotations/eks.amazonaws.com~1role-arn' not in run_text:
        results.append("missing-role-arn-json-pointer")
    if "RespectIgnoreDifferences=true" not in run_text:
        results.append("missing-RespectIgnoreDifferences")
    if "CreateNamespace=true" not in run_text:
        results.append("missing-CreateNamespace")
    if "ServerSideApply=true" not in run_text:
        results.append("missing-ServerSideApply")
    # No broad group/kind/name wildcard: the ignoreDifferences block must
    # reference exactly one Sid-equivalent rule, not e.g. a bare "kind":
    # "ServiceAccount" without a name, or a missing namespace.
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

    # --- Correction 6: every AmazonCloudWatchAgent CR checked for filelog
    # (scoped to the filelog section only -- section "14." legitimately
    # looks up the specific named cloudwatch-agent/cluster-scraper CRs for
    # the unrelated Phase 6B2B host-network isolation check added later in
    # this same step, so the hardcoded-single-CR-name regression check
    # below must not fire on that different, intentional lookup).
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
    # Must never print the resolved env var VALUE or a full env dump --
    # only the pattern that captures NAMES (jsonpath .name, not .value).
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

    # -------------------------------------------------------------------
    # Phase 6B2B runner/connectivity correction (focused, static/offline
    # only -- no AWS/kubectl/network/Git call).
    # -------------------------------------------------------------------
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

    # 9: error handling mentions private EKS/network reachability and does
    # NOT claim the CRD is missing.
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

    # -------------------------------------------------------------------
    # Phase 6B2B DaemonSet full-readiness and failure-diagnostics
    # correction (focused, static/offline only).
    # -------------------------------------------------------------------
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

# 1-3: a reusable exact DaemonSet readiness function comparing all required
# fields, with a bounded timeout and polling interval.
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

    # 5: dynamically derived selector from spec.selector.matchLabels (no
    # hardcoded chart labels).
    if "spec.selector.matchLabels" not in run_text:
        results.append("missing-dynamic-selector-derivation")
    if "show_daemonset_diagnostics()" not in run_text and "show_daemonset_diagnostics ()" not in run_text:
        results.append("missing-show_daemonset_diagnostics-function")

    # 6: failure diagnostics include bounded pod state, node name, waiting
    # reason, restart count, bounded events, bounded current/previous logs.
    for marker in (
        "nodeName", "restartCount", "state.waiting.reason",
        "kubectl get events", "--tail=80", "--previous",
        "tolerated",
    ):
        if marker not in run_text:
            results.append(f"diagnostics-missing:{marker}")

    # Correction 3: diagnostics called before failing, exit non-zero, and
    # no proceeding past the timeout.
    if run_text.count("show_daemonset_diagnostics \"$TARGET_NAMESPACE\" cloudwatch-agent") < 1:
        results.append("diagnostics-not-called-for-cloudwatch-agent")
    if run_text.count("show_daemonset_diagnostics \"$TARGET_NAMESPACE\" node-exporter") < 1:
        results.append("diagnostics-not-called-for-node-exporter")
    if "FAIL: cloudwatch-agent did not reach full readiness" not in run_text:
        results.append("missing-cloudwatch-agent-timeout-fail-message")
    if "FAIL: node-exporter did not reach full readiness" not in run_text:
        results.append("missing-node-exporter-timeout-fail-message")

# 7-8: IRSA check iterates across every CloudWatch Agent DaemonSet pod, and
# the checked count must equal desiredNumberScheduled.
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

# 9: live validation requires both numberReady and numberAvailable to equal
# desiredNumberScheduled (not READY >= DESIRED).
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

# 10: the bounded log diagnostic step uses always() with deploy=true and
# does not fail the workflow itself.
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

# 11: no maxUnavailable, probe, resource, toleration, IAM, Terraform, or
# Helm value change was introduced anywhere in this workflow file. Comment
# lines are excluded -- explaining *why* the default maxUnavailable=1
# causes the false-positive-rollout-status problem is exactly what this
# correction's own comments legitimately do; only actual code/config use is
# forbidden.
#
# hostNetwork is deliberately NOT in this list: a later, separately
# authorized Phase 6B2B host-network isolation correction legitimately
# reads/validates spec.hostNetwork and spec.template.spec.hostNetwork
# (read-only kubectl get/jsonpath checks, never a probe/resource/
# toleration/updateStrategy/maxUnavailable change) -- see check 19/20
# below, which validate that correction's actual scope precisely.
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

    # -------------------------------------------------------------------
    # Phase 6B2B host-network isolation correction (focused, static/
    # offline only) -- workflow-side checks: semantic validation, rendered
    # CR validation, live hostNetwork validation, and the exact crash-
    # symptom log check.
    # -------------------------------------------------------------------
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

    # every node-agent pod and every active cluster-scraper pod checked,
    # via a dynamically derived selector (no hardcoded chart labels).
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

  # -------------------------------------------------------------------
  # Phase 6B2B host-network isolation correction (focused, static/offline
  # only) -- values.yaml-side checks.
  # -------------------------------------------------------------------
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

# 5: existing top-level agent image/ServiceAccount/target-allocator/
# private-ECR configuration remains present and unweakened.
if isinstance(agent_block, dict):
    if agent_block.get("serviceAccount", {}).get("name") != "cloudwatch-agent":
        results.append("agent.serviceAccount.name-missing-or-changed")
    img = agent_block.get("image", {})
    if img.get("repository") != "aws-cloud-factory-cloudwatch-agent":
        results.append("agent.image.repository-missing-or-changed")
    if img.get("repositoryDomainMap", {}).get("public") != "229410149234.dkr.ecr.eu-west-1.amazonaws.com":
        results.append("agent.image.repositoryDomainMap.public-missing-or-changed")
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
# port 8888 itself must never be manually assigned a value in code (only
# ever discussed in comments, which are excluded above).
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

  # 13: no wrapper chart was created for this phase.
  if [ -d "${REPO_ROOT}/helm/goldengate-observability" ]; then
    fail "13: helm/goldengate-observability/ wrapper chart unexpectedly exists -- Argo CD must consume the private upstream OCI chart directly"
  else
    pass "13: no helm/goldengate-observability wrapper chart was created"
  fi

  # 14: no EKS Terraform enable_cloudwatch variable was introduced anywhere
  # in this repository's Terraform.
  if grep -rl 'enable_cloudwatch' "${REPO_ROOT}/envs" 2>/dev/null | grep -q .; then
    fail "14: an enable_cloudwatch Terraform variable/reference was unexpectedly introduced under envs/"
  else
    pass "14: no enable_cloudwatch Terraform variable/reference exists under envs/"
  fi

  # 15: Phase 6A and Phase 6B1 resources remain untouched by this phase.
  PHASE_6A_6B1_DIFF="$(git -C "$REPO_ROOT" diff --stat --ignore-all-space -- \
    .github/workflows/cloudwatch-observability-artifact-sync.yaml \
    helm/goldengate-platform \
    platform/dev/goldengate-platform \
    envs/dev/cloudwatch_observability.tf \
    envs/dev/cloudwatch_logs.tf \
    envs/dev/policies/goldengate-cloudwatch-metrics-dev \
    envs/dev/policies/goldengate-platform-logging-dev \
    2>/dev/null || true)"
  if [ -z "$PHASE_6A_6B1_DIFF" ]; then
    pass "15: Phase 6A (gg-fluent-bit) and Phase 6B1/6B2A (CloudWatch Observability supply chain, IAM) files are unchanged"
  else
    fail "15: an unexpected change was found in Phase 6A/6B1/6B2A files:"$'\n'"${PHASE_6A_6B1_DIFF}"
  fi

  # Strict YAML parse of the platform workflow (must still parse cleanly
  # after the Phase 6A Fluent Bit role-ARN/region plumbing was added), plus
  # a forbidden-mutation-command scan limited to the new/changed lines --
  # this workflow legitimately runs many AWS/kubectl mutating calls
  # elsewhere (namespace/Application apply, ECR repository creation, etc.),
  # so the scan here only proves the Phase 6A additions themselves
  # introduced no new destructive AWS Logs action (no CreateLogGroup/
  # DeleteLogGroup/PutRetentionPolicy anywhere in the whole file).
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

  # The monitor chart's ConfigMap reads a staged copy of the canonical
  # config from its own files/ directory -- never committed there (see
  # goldengate-monitor.yaml) -- so lint/render stage a throwaway copy here.
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

# ---------------------------------------------------------------------
# 5. Render every enabled deployment discovered from the canonical config;
#    validate exactly one StatefulSet per release and no runtime sidecar.
# ---------------------------------------------------------------------
echo ""
echo "--- Render enabled runtimes; one StatefulSet each, no sidecar ---"
if [ "$HELM_AVAILABLE" = "true" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    VALUES_FILE="envs/dev/${name}/values.yaml"
    RENDERED="${WORKDIR}/${name}.yaml"
    if ! helm template "$name" "$RUNTIME_CHART" --namespace goldengate-dev \
        -f "$VALUES_FILE" > "$RENDERED" 2>"${WORKDIR}/${name}.err"; then
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

# ---------------------------------------------------------------------
# 6. Existing shared monitor resources; no duplicate monitor deployment.
# ---------------------------------------------------------------------
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
         --set ingress.enabled=true --set ingress.host=monitor.goldengate-dev.adcbmis.local \
         --set ingress.alb.certificateArn=arn:aws:acm:eu-west-1:668311715351:certificate/test \
         2>/dev/null | grep -q "host: monitor.goldengate-dev.adcbmis.local"; then
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

# ---------------------------------------------------------------------
# 7. No hardcoded runtime/pipeline names in application or workflow code.
# ---------------------------------------------------------------------
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
for f in "${MONITOR_APP_DIR}"/monitor.py "${MONITOR_APP_DIR}"/collector.py "${MONITOR_APP_DIR}"/config.py "${MONITOR_APP_DIR}"/health_rules.py; do
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

# ---------------------------------------------------------------------
# 8. No committed generated copies of the canonical config inside charts.
# ---------------------------------------------------------------------
echo ""
echo "--- No generated canonical-config copies committed in charts ---"
if [ -e "helm/goldengate-monitor/files/goldengate-deployments.yaml" ] \
    || [ -d "helm/goldengate-monitor/files/pipelines" ] \
    || [ -d "helm/goldengate-monitor/files/topologies" ]; then
  fail "helm/goldengate-monitor/files/ contains a committed generated copy of the canonical config"
else
  pass "helm/goldengate-monitor/files/ contains no committed generated copy"
fi

# ---------------------------------------------------------------------
# 9. No committed pycache/pyc.
# ---------------------------------------------------------------------
echo ""
echo "--- No committed __pycache__/*.pyc ---"
STRAY_PYCACHE="$(find . -type d -name "__pycache__" -not -path "*/node_modules/*" 2>/dev/null)"
STRAY_PYC="$(find . -type f -name "*.pyc" -not -path "*/node_modules/*" 2>/dev/null)"
if [ -z "$STRAY_PYCACHE" ] && [ -z "$STRAY_PYC" ]; then
  pass "no __pycache__ directories or *.pyc files present"
else
  fail "found stray __pycache__/*.pyc: ${STRAY_PYCACHE} ${STRAY_PYC}"
fi

# ---------------------------------------------------------------------
# 10. Phase 4B1: contract-probe tool packaged but never auto-run; CloudWatch
#     stays physically disabled by default.
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 11. Phase 4B2A: confirmed secure PMS route documented; 9015 stays
#     unauthenticated-only; /services/v2/metrics not recommended as
#     production PMS.
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 12. Phase 4B2B: --follow-processes fixed detail allowlist exists and is
#     never wired into automatic startup.
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 13. Phase 4C1: production PMS collection bounded, no new DynamoDB
#     record type, forbidden endpoints never referenced by name.
# ---------------------------------------------------------------------
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
# Skip the module docstring (lines 1..first closing triple-quote), which
# legitimately documents these endpoints as NOT used -- only code after it
# must never reference them.
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

# ---------------------------------------------------------------------
# 14. Phase 4C1 correction: process-name/numeric bounds and stale-PMS
#     overwrite semantics remain in place.
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 15. Phase 4C1 pre-deployment correction: total PMS collection time
#     budget stays fixed and comfortably under the deployed stale
#     threshold; serviceHealth validation stays tightened.
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 16. Phase 4C2: manager-compatible portal -- GET /api/processes exists,
#     canonical STATE#-only (no Scan, no legacy fallback in that path).
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 17. Phase 4D1: CloudWatch metric-path source hardening -- exact manager-
#     compatible metric contract, sanitized PutMetricData failure logging,
#     hard switch still gates client construction, no alarm/SNS/gg-alerter/
#     Fluent Bit or read/alarm CloudWatch IAM permission introduced.
# ---------------------------------------------------------------------
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

# Phase 6A introduced the platform-level Fluent Bit DaemonSet -- no longer
# blanket-forbidden, but still confined to its expected locations (the
# goldengate-platform chart templates, its dedicated IRSA policy folder,
# and the CloudWatch Logs Terraform) and never inside the GoldenGate
# runtime/monitor charts or Python code.
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

# ---------------------------------------------------------------------
# 18. Phase 4D1 final correction: strict identity-based two-factor gate,
#     and CloudWatch client construction moved behind a sanitized,
#     non-raising protected publication boundary.
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 19. Phase 4D2: controlled DEV CloudWatch activation -- workflow_dispatch
#     Boolean control, Argo CD ownership of the value, fail-closed CONFIG
#     preflight (GetItem-only, no Scan, no new IAM), and post-deployment
#     verification/rollback. Base Helm default stays disabled.
# ---------------------------------------------------------------------
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

if grep -q "name: CloudWatch publication preflight (CONFIG.metricsEnabled)" "$MONITOR_WORKFLOW" 2>/dev/null; then
  pass "goldengate-monitor.yaml defines the CloudWatch publication preflight step"
else
  fail "goldengate-monitor.yaml is missing the CloudWatch publication preflight step"
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

# ---------------------------------------------------------------------
# 20. Phase 4D2 pre-deployment correction: runtime-image hash scoped to
#     Dockerfile inputs only, unit tests unconditional, POSIX-safe
#     discovery, unique per-attempt Helm OCI revision, Ready-pod selection.
# ---------------------------------------------------------------------
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

# Functional execution of the exact extracted awk script (proving it
# returns precisely the two enabled canonical deployments, never
# hardcoded) is covered by the Python suite -- see
# WorkflowStaticAnalysisTests.test_deployment_discovery_awk_returns_exactly_both_enabled_deployments,
# already run above in section 3 ("Python unit tests").

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

# ---------------------------------------------------------------------
# 21. Phase 4D2 supply-chain/pod-selection correction: .dockerignore
#     participates in the runtime-image hash, the Dockerfile requires an
#     explicitly supplied digest-pinned private base image (no public
#     default), and Ready-pod selection excludes terminating pods.
# ---------------------------------------------------------------------
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
# The value is only ever interpolated on ONE line -- the GITHUB_ENV write
# that hands it to later steps. The success confirmation is a generic
# message (no value at all), and none of the three failure branches above
# interpolate it either. Proven functionally (with a marker value) by
# MonitorBaseImageValidationTests.test_failure_never_prints_the_raw_malformed_value
# and .test_success_path_never_prints_the_full_raw_value_either.
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

TERMINATING_EXCLUSION_COUNT="$(grep -c 'deletionTimestamp == null' "$MONITOR_WORKFLOW" 2>/dev/null || true)"
if [ "${TERMINATING_EXCLUSION_COUNT:-0}" -eq 2 ]; then
  pass "both Ready-pod jq filters (preflight and post-deployment verification) exclude terminating pods"
else
  fail "expected exactly 2 Ready-pod jq filters excluding terminating pods, found ${TERMINATING_EXCLUSION_COUNT:-0}"
fi

# ---------------------------------------------------------------------
# 22. Phase 4D2 workflow-security and manager critical-service correction:
#     no direct GitHub-expression interpolation inside a run script, a
#     fully-anchored ECR repository+digest grammar (not prefix+suffix
#     only), and manager-compatible adminsrvr/distsrvr/recvsrvr coverage
#     for every deployment.
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# 23. Phase 5A: observer source/build/chart retirement, legacy-values
#     folder disablement without deletion, and gg-monitor legacy-fallback
#     removal.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 5A: legacy values folder disabled (retained, not deleted) ---"

if [ -f "$DETECT_SCRIPT" ] && command -v python3 >/dev/null 2>&1; then
  # Use the real, tracked, executable detection script directly (never a
  # reimplementation, never re-extracted from the workflow YAML -- since
  # Phase 5B2A's workflow-compilation-size fix, the workflow step itself is
  # only a thin wrapper that calls this script; the actual implementation
  # lives here) and exercise its is_active_deployment_values_file()
  # function and its deletion-candidate case statement directly, against
  # the real repository files.
  cp "$DETECT_SCRIPT" "${WORKDIR}/detect_script.sh"

  awk '/^is_active_deployment_values_file\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh" > "${WORKDIR}/is_active_fn.sh"

  # is_goldengate_deployment_values_file and its git-revision sibling both
  # depend on _classify_deployment_model_yaml -- all three must be extracted
  # and sourced together, in dependency order, or the classifier fails with
  # "command not found" while still being source-able (a broken harness that
  # silently produces no useful assertion, exactly the defect being fixed
  # here).
  {
    awk '/^_classify_deployment_model_yaml\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
    echo ""
    awk '/^is_goldengate_deployment_values_file\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
    echo ""
    awk '/^is_goldengate_deployment_values_file_at_ref\(\) \{/,/^\}$/' "${WORKDIR}/detect_script.sh"
  } > "${WORKDIR}/is_gg_fn.sh"

  # Fail loudly (not silently) if any expected function body failed to
  # extract -- an empty/missing body here would make every downstream
  # source-and-call test below meaningless.
  for required_fn in _classify_deployment_model_yaml is_goldengate_deployment_values_file is_goldengate_deployment_values_file_at_ref; do
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

check_one "envs/dev/gg-oracle-payments-01/values.yaml" 0 "oracle-active"
check_one "envs/dev/gg-postgresql-payments-01/values.yaml" 0 "postgresql-active"
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

check_one "envs/dev/gg-oracle-payments-01/values.yaml" 0 "oracle-is-gg"
check_one "envs/dev/gg-postgresql-payments-01/values.yaml" 0 "postgresql-is-gg"
check_one "envs/dev/goldengate-monitor/values.yaml" 1 "monitor-is-not-gg"
check_one "envs/dev/argocd/values.yaml" 1 "argocd-is-not-gg"
HARNESS

  GG_CHECK_OUTPUT="$(bash "${WORKDIR}/run_is_gg_checks.sh" 2>&1 || true)"
  echo "$GG_CHECK_OUTPUT"

  if echo "$GG_CHECK_OUTPUT" | grep -q "^PASS oracle-is-gg" \
      && echo "$GG_CHECK_OUTPUT" | grep -q "^PASS postgresql-is-gg"; then
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

  if echo "$ACTIVE_CHECK_OUTPUT" | grep -q "^PASS oracle-active" && echo "$ACTIVE_CHECK_OUTPUT" | grep -q "^PASS postgresql-active"; then
    pass "the real workflow's is_active_deployment_values_file() reports both canonical folders active"
  else
    fail "one or both canonical folders are not reported active by the real workflow function"
  fi

  # A shared-chart-change selection scans every envs/dev/<id>/values.yaml
  # (excluding argocd/) exactly as the workflow does, then filters through
  # the same two real functions, in the same order the workflow applies them
  # (is_goldengate_deployment_values_file first, then
  # is_active_deployment_values_file) -- proving the exact resulting active
  # set, using the actual discovery command.
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

  EXPECTED_ACTIVE_IDS="gg-oracle-payments-01 gg-postgresql-payments-01"
  if [ "$ACTIVE_IDS_SORTED" = "$EXPECTED_ACTIVE_IDS" ]; then
    pass "a shared chart change produces exactly the canonical active set (${EXPECTED_ACTIVE_IDS}) -- no additional ID present"
  else
    fail "a shared chart change produced an unexpected active set: got [${ACTIVE_IDS_SORTED}], expected [${EXPECTED_ACTIVE_IDS}]"
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

  # Deletion-candidate safeguard: extract the real case-statement logic and
  # exercise it, together with the REAL classifier functions (never stubs
  # for is_goldengate_deployment_values_file/_at_ref -- only jq is stubbed,
  # since its own JSON behavior is not what this test verifies), against a
  # throwaway, self-contained Git repository built specifically to exercise
  # all 7 required scenarios: existing/removed files, GoldenGate/non-
  # GoldenGate deploymentModel, and malformed/unknown content.
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
             "${DELETION_REPO}/envs/dev/case3-historical-legacypair-removed"

    printf 'deploymentModel: singleRuntime\nrunning: at-base-revision\n' > "${DELETION_REPO}/envs/dev/case2-removed-canonical/values.yaml"
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

    # Now mutate the working tree to the "after" state the loop actually
    # evaluates: case2/3/4/5/6/7 are removed (git rm, matching a real
    # removed/renamed deletion candidate -- case3 specifically proves the
    # HISTORICAL DELETION CONTRACT still classifies a legacyPair deployment
    # that existed at the base revision, exactly how the real, now-removed
    # envs/dev/payments-ora-to-pg-001/ deletion actually worked); case1 is
    # added fresh in the working tree only (never committed -- it
    # represents a "still exists, but now inactive" candidate, which is
    # what the loop's is_goldengate_deployment_values_file working-tree/
    # ACTIVE CONTRACT path reads -- and, being legacyPair, is correctly
    # invisible to that active-only path regardless of its
    # deployment.enabled value); the case-empty-* files are overwritten IN
    # PLACE (never git rm'd) with each of the four "deliberately empty"
    # shapes the classification fix must fall back through to their still-
    # valid content at BEFORE_SHA.
    git -C "$DELETION_REPO" rm -rq envs/dev/case2-removed-canonical envs/dev/goldengate-monitor envs/dev/argocd envs/dev/case6-malformed envs/dev/case7-unknown-model envs/dev/case3-historical-legacypair-removed

    mkdir -p "${DELETION_REPO}/envs/dev/case1-retired-legacypair-retained"
    printf 'deploymentModel: legacyPair\ndeployment:\n  enabled: false\n' > "${DELETION_REPO}/envs/dev/case1-retired-legacypair-retained/values.yaml"

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
        local args=("$@") model="" id=""
        for i in "${!args[@]}"; do
          [ "${args[$i]}" = "deployment_id" ] && id="${args[$((i+1))]}"
          [ "${args[$i]}" = "deployment_model" ] && model="${args[$((i+1))]}"
        done
        echo "[ADDED id=${id} model=${model}]"
      }
      BEFORE_SHA="'"$DELETION_BEFORE_SHA"'"

      for id in case1-retired-legacypair-retained case2-removed-canonical case3-historical-legacypair-removed goldengate-monitor argocd case6-malformed case7-unknown-model case-empty-zerobyte case-empty-comment case-empty-whitespace case-empty-null; do
        DELETION_MATRIX_ITEMS="[]"
        INACTIVE_LOG=""
        DELETION_CANDIDATE_IDS="$id"
        source "'"${WORKDIR}"'/deletion_loop.sh"
        echo "RESULT ${id} => ${DELETION_MATRIX_ITEMS}"
      done
    ' 2>&1)"
    DELETION_HARNESS_STATUS=$?
    echo "$DELETION_TEST_OUTPUT"

    # The harness itself must never silently swallow a broken classifier:
    # any command-not-found or Python traceback anywhere in the captured
    # output fails this test outright, regardless of what the individual
    # case assertions below would otherwise report.
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
    check_deletion_case "2: removed canonical GoldenGate values (deploymentModel: singleRuntime) produces a deletion entry with deployment_model=singleRuntime" \
      '^RESULT case2-removed-canonical => \[ADDED id=case2-removed-canonical model=singleRuntime\]$'
    check_deletion_case "4-req: the historical deletion contract still classifies a removed legacyPair deployment (deployment_model=legacyPair) even though legacyPair is no longer active/deployable" \
      '^RESULT case3-historical-legacypair-removed => \[ADDED id=case3-historical-legacypair-removed model=legacyPair\]$'
    check_deletion_case "11: removed goldengate-monitor values does not enter the GoldenGate deletion matrix" \
      '^RESULT goldengate-monitor => \[\]$'
    check_deletion_case "12: removed argocd values does not enter the GoldenGate deletion matrix" \
      '^RESULT argocd => \[\]$'
    check_deletion_case "13: removed malformed previous YAML does not enter deletion" \
      '^RESULT case6-malformed => \[\]$'
    check_deletion_case "14: removed unknown deploymentModel does not enter deletion" \
      '^RESULT case7-unknown-model => \[\]$'
    check_deletion_case "8: a zero-byte values file (previously valid) creates its deletion entry" \
      '^RESULT case-empty-zerobyte => \[ADDED id=case-empty-zerobyte model=singleRuntime\]$'
    check_deletion_case "6: a comment-only canonical values file creates its deletion entry" \
      '^RESULT case-empty-comment => \[ADDED id=case-empty-comment model=singleRuntime\]$'
    check_deletion_case "7: a whitespace-only values file creates its deletion entry" \
      '^RESULT case-empty-whitespace => \[ADDED id=case-empty-whitespace model=singleRuntime\]$'
    check_deletion_case "9: YAML null creates its deletion entry when the previous file was valid" \
      '^RESULT case-empty-null => \[ADDED id=case-empty-null model=singleRuntime\]$'

    rm -rf "$DELETION_REPO"
  else
    fail "could not extract the deletion-candidate loop and/or classifier functions from ${DETECT_SCRIPT}"
  fi
else
  skip "Phase 5A legacy-folder behavioral checks -- ${DETECT_SCRIPT} or python3 not available"
fi

# ---------------------------------------------------------------------
# Phase 5B2A pre-deployment correction: active/historical classifier split
# regression tests. Covers the required proofs: manual legacyPair
# deployment is rejected; active legacyPair cannot enter the build matrix;
# missing/unknown current deploymentModel never defaults to legacyPair;
# unknown deletion-matrix model fails closed; no active build/Application
# path contains legacyPair; no source/target StatefulSet/PVC validation
# remains; the workflow summary accurately documents all deletion
# triggers. (Historical-legacyPair deletion classification is covered
# above by case3-historical-legacypair-removed.)
# ---------------------------------------------------------------------
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

  # 1: manual (workflow_dispatch-equivalent) legacyPair deployment request
  # is rejected by the active contract -- workflow_dispatch validates the
  # requested deployment_id's values file with exactly this same function.
  if echo "$CLASSIFIER_OUT" | grep -qE "^CLASSIFY case-manual-legacypair status=1 reason=not a GoldenGate deployment values file: deploymentModel='legacyPair'$"; then
    pass "1: a manual (workflow_dispatch) request for a legacyPair deployment is rejected by the active contract"
  else
    fail "1: a manual legacyPair deployment request was not rejected as expected"
  fi

  # 2: active legacyPair cannot enter the push-triggered build/update
  # matrix -- that loop classifies every candidate with exactly this same
  # function before ever considering active/inactive status.
  if echo "$CLASSIFIER_OUT" | grep -qE "^CLASSIFY case-push-active-legacypair status=1 reason=not a GoldenGate deployment values file: deploymentModel='legacyPair'$"; then
    pass "2: a legacyPair deployment values file cannot enter the active push build/update matrix"
  else
    fail "2: a legacyPair deployment values file was not excluded from the active build matrix as expected"
  fi

  # 3: missing/unknown current deploymentModel never defaults to
  # legacyPair -- both a file with no deploymentModel key at all, and a
  # file with an unrecognized value, must be rejected (reason mentions the
  # actual value/None), never silently treated as legacyPair or accepted.
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
  # 5: the deletion job's "Prepare deletion variables" step must fail
  # closed (non-zero exit, no defaulting) when matrix.deployment_model is
  # neither singleRuntime nor legacyPair.
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

    # Sanity: singleRuntime and legacyPair both still resolve without error.
    set +e
    SINGLE_OK_OUT="$(TEST_ENVIRONMENT="dev" TEST_DEPLOYMENT_ID="gg-oracle-payments-01" TEST_DEPLOYMENT_MODEL="singleRuntime" \
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

# 6/7: static checks that no active build/Application-path code contains
# legacyPair conditional logic, and that no source/target StatefulSet/PVC
# validation remains anywhere in the workflow.
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
        # Allowed: blank, a comment line (bash '#' or Python '#' -- this
        # job's run: blocks are bash with embedded python3 heredocs, and
        # both comment styles use '#'), or a bare echo/print statement --
        # those are informational log/error-message text (e.g. explaining
        # *why* legacyPair is rejected), never branching/decision logic.
        # What must be absent is legacyPair appearing in an actual
        # conditional, comparison, assignment, or case pattern.
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

  # Only non-comment lines count as "validation remaining" -- a comment
  # that merely explains what was removed (e.g. "no longer renders
  # source-statefulset.yaml") is expected and must not be flagged.
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

# 9: the build job's workflow summary accurately documents every deletion
# trigger (physical removal, zero-byte, whitespace-only, comment-only,
# YAML null, lifecycle.state=absent) and correctly describes enabled=false/
# deployment.enabled=false as retained (non-deleting).
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

# ---------------------------------------------------------------------
# Phase 5B2A: malformed CURRENT YAML must fail the workflow closed (never
# silently skipped, never silently deleted); whole-folder, whole-envs-
# directory, and rename scenarios exercised through the REAL discovery
# logic (git diff --name-status), not just the isolated per-ID loop above.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 5B2A: malformed-current-YAML hard failure; folder/envs-directory/rename discovery ---"

if [ -f "${WORKDIR}/detect_script.sh" ] && [ -s "${WORKDIR}/detect_script.sh" ] && command -v python3 >/dev/null 2>&1; then
  # 15: malformed CURRENT YAML (file still exists, still has bytes, but is
  # not valid YAML) must abort the whole detection script with a clear
  # error -- never be treated as an intentional deletion signal, and never
  # silently ignored either.
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

  # 4/5/10: exercise the REAL discovery logic (REMOVED_PATH_IDS/
  # CHANGED_VALUES_IDS via git diff --name-status), not just a manually
  # supplied DELETION_CANDIDATE_IDS, for: whole-folder deletion, whole-envs-
  # directory deletion, and folder rename.
  # Extract only the discovery half (NAME_STATUS/REMOVED_PATH_IDS/
  # CHANGED_VALUES_IDS/DELETION_CANDIDATE_IDS construction), stopping
  # before the "for CANDIDATE_ID in $DELETION_CANDIDATE_IDS" loop --
  # that loop is reused as-is from the already-extracted, already-proven
  # deletion_loop.sh above, so this test never needs to stub the
  # unrelated DEPLOYMENT_MATRIX_ITEMS jq recomputation that follows it
  # in the real script (which requires real jq and $GITHUB_OUTPUT).
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

    # Test 10: renaming a deployment folder deletes the old ID and the new
    # ID is discovered as an independent candidate (build-matrix discovery
    # is a separate code path from the deletion loop under test here, so
    # this proves the deletion half of the contract: the OLD id must be
    # queued for deletion; the NEW id must never itself appear as a
    # deletion entry).
    setup_rename() { :; }
    setup_rename_mutate() { git -C "$1" mv envs/dev/gg-oracle-payments-01 envs/dev/gg-oracle-payments-01-renamed; }
    run_discovery_case "10: renaming a deployment folder deletes the old ID (and never queues the new ID for deletion)" \
      setup_rename \
      'ADDED id=gg-oracle-payments-01 model=singleRuntime' \
      'ADDED id=gg-oracle-payments-01-renamed'

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
  # The only place this workflow deletes an Argo CD Application or
  # namespace is delete_removed_argocd_applications, gated on
  # has_deletions=true from the deletion matrix -- already proven above to
  # exclude deployment.enabled=false. No separate, disable-triggered
  # deletion path may exist anywhere else in the file.
  DIRECT_DELETE_HITS="$(grep -n 'kubectl delete\|delete-repository\|efs delete-access-point\|delete_access_point' "$EKS_APP_WORKFLOW" | grep -v 'kubectl delete application\|kubectl delete namespace' || true)"
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
  # The only acceptable occurrences are inside a step-level `env:` mapping
  # (INPUT_X: ${{ inputs.x }}), never inside a run-script body. Re-check
  # precisely against the full line text (grep -v above already filtered
  # the common env-mapping shape; anything left over is a real hit).
  if [ -n "$INPUTS_INTERP_HITS" ]; then
    fail "\${{ inputs.* }} appears outside a step-level env: mapping in ${EKS_APP_WORKFLOW}:"$'\n'"${INPUTS_INTERP_HITS}"
  else
    pass "every \${{ inputs.* }} occurrence in ${EKS_APP_WORKFLOW} is confined to a step-level env: mapping, never a run-script body"
  fi
else
  skip "inputs.* interpolation sweep -- ${EKS_APP_WORKFLOW} not found"
fi

if [ -f "$DETECT_SCRIPT" ] && command -v python3 >/dev/null 2>&1; then
  # Marker-file proof: feed the real, tracked hack/detect-goldengate-
  # deployments.sh a workflow_dispatch deployment_id containing shell
  # metacharacters via INPUT_DEPLOYMENT_ID (exactly how the real step-level
  # env: mapping delivers it), and confirm the payload is never evaluated
  # as shell code. EVENT_NAME/BEFORE_SHA/AFTER_SHA are plain environment
  # variables in this script (never ${{ }} GitHub expression syntax --
  # that substitution happens once, outside this script, in the workflow's
  # own env: mapping), so no sed-based expression resolution is needed
  # here: set EVENT_NAME=workflow_dispatch directly, the same opaque-string
  # way the real workflow step would.

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

  # Payloads reference $MARKER_FILE_FOR_TEST (exported above) rather than an
  # embedded absolute path, so the exact same payload strings work
  # regardless of $WORKDIR's location.
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
  for pair in "gg-oracle-payments-01:goldengate-dev" "gg-postgresql-payments-01:goldengate-dev"; do
    id="${pair%%:*}"; ns="${pair##*:}"
    VALUES_FILE="envs/dev/${id}/values.yaml"
    RENDERED="${WORKDIR}/${id}-observer-check.yaml"
    if helm template "$id" "$RUNTIME_CHART" --namespace "$ns" -f "$VALUES_FILE" \
        --set global.environment=dev --set global.deploymentId="$id" > "$RENDERED" 2>"${WORKDIR}/${id}-observer-check.err"; then
      if grep -qi "goldengate-observer\|observer-enabled" "$RENDERED"; then
        fail "${id}: rendered manifest still contains an observer container/annotation reference"
      else
        pass "${id}: rendered manifest contains no observer container/annotation reference"
      fi
    else
      fail "${id}: helm template failed during observer-absence render check"
      cat "${WORKDIR}/${id}-observer-check.err"
    fi
  done
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

# Structural signals only -- never a bare substring grep, which would
# false-positive on this repository's own negative-assertion code (e.g. a
# test's forbidden-string tuple, or FORBIDDEN_CONTAINER_SUBSTRINGS in the
# workflow's singleRuntime contract check -- both deliberately mention these
# names to prove their absence, not to implement them). Phase 6A legitimately
# added helm/goldengate-platform/templates/fluent-bit-*.yaml (checked
# separately above); this block only proves Fluent Bit was never added as
# its own sibling chart (helm/<name>, maxdepth 2) or into the GoldenGate
# runtime/monitor charts specifically.
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

# Phase 5B1 legitimately changes envs/dev/policies/goldengate-secrets-read-dev
# (observer DynamoDB/CloudWatch statements removed) and envs/dev/iam.tf's
# comments/description text -- the monitor role's policy folder must remain
# completely untouched, and iam.tf's structural identifiers (role names,
# policy_folder attachments) must not change even though description text
# may. Whitespace/line-ending-only diffs are pre-existing baseline noise in
# this repository -- compare with --ignore-all-space so only substantive
# content changes count.
MONITOR_IAM_DIFF="$(git diff --ignore-all-space -- envs/dev/policies/goldengate-monitor-read-dev 2>/dev/null || true)"
if [ -z "$MONITOR_IAM_DIFF" ]; then
  pass "envs/dev/policies/goldengate-monitor-read-dev has no substantive changes (monitor IAM untouched)"
else
  fail "unexpected change detected in envs/dev/policies/goldengate-monitor-read-dev -- the monitor role must remain untouched"
fi

# ---------------------------------------------------------------------
# 27. Phase 5B1: runtime IAM least-privilege reduction (observer DynamoDB/
#     CloudWatch permissions removed; monitor IAM and Secrets Manager/KMS
#     access for canonical and legacy runtime pods unchanged).
# ---------------------------------------------------------------------
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


# 1. Runtime policy grants no DynamoDB action anywhere (the entire
# monitoring-state statement, not just its Sid, must be gone).
runtime_dynamodb_actions = set()
for s in runtime_statements:
    runtime_dynamodb_actions |= {a for a in actions_of(s) if a.startswith("dynamodb:")}
check("1_no_dynamodb_actions", not runtime_dynamodb_actions)

# 2. Runtime policy grants no cloudwatch:PutMetricData (or any cloudwatch:*).
runtime_cloudwatch_actions = set()
for s in runtime_statements:
    runtime_cloudwatch_actions |= {a for a in actions_of(s) if a.startswith("cloudwatch:")}
check("2_no_cloudwatch_actions", not runtime_cloudwatch_actions)

# 3. Runtime role retains its Secrets Manager statement, byte-identical to
# the original (never broadened to compensate for the removed statements).
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

# 5. Monitor role retains DynamoDB read/write (CONFIG reads + LEASE/STATE#
# writes travel over the same table-level actions) and PutMetricData scoped
# to GoldenGate/Pipelines.
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

# 6. Runtime and monitor policies remain distinct documents (never merged/
# aliased into each other).
check("6_roles_remain_separate", runtime_path != monitor_path and runtime_statements != monitor_statements)

# 9. No wildcard (Resource: "*") DynamoDB, CloudWatch, or Secrets Manager
# action exists in the runtime policy (the pre-existing KMS Decrypt
# Resource: "*" is a known, unchanged, intentionally broad grant -- not
# newly introduced by this phase -- so it is exempted here and covered by
# checks 3/4's byte-identical comparison instead).
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

  # 10. No statement was broadened to compensate: the runtime policy has
  # exactly the 2 retained statements, nothing more.
  RUNTIME_STMT_COUNT="$(python3 -c "import json; print(len((json.load(open('${RUNTIME_POLICY_FILE}')) or {}).get('Statement') or []))")"
  if [ "$RUNTIME_STMT_COUNT" = "2" ]; then
    pass "10: runtime policy has exactly 2 statements (no broadening or replacement compensation)"
  else
    fail "10: runtime policy has ${RUNTIME_STMT_COUNT} statements, expected exactly 2"
  fi
else
  skip "Phase 5B1 IAM least-privilege checks -- policy files or python3 not available"
fi

# 7. Canonical runtime ServiceAccounts still reference GoldenGateSecretsReadRole-dev.
RUNTIME_ROLE_REF_MISSING=""
for f in envs/dev/gg-oracle-payments-01/values.yaml envs/dev/gg-postgresql-payments-01/values.yaml; do
  grep -q "role/GoldenGateSecretsReadRole-dev" "$f" 2>/dev/null || RUNTIME_ROLE_REF_MISSING="${RUNTIME_ROLE_REF_MISSING} ${f}"
done
if [ -z "$RUNTIME_ROLE_REF_MISSING" ]; then
  pass "7: canonical runtime ServiceAccounts (Oracle, PostgreSQL) still reference GoldenGateSecretsReadRole-dev"
else
  fail "7: canonical runtime values file(s) no longer reference GoldenGateSecretsReadRole-dev:${RUNTIME_ROLE_REF_MISSING}"
fi

# 8. gg-monitor still references GoldenGateMonitorReadRole-dev.
if grep -q "role/GoldenGateMonitorReadRole-dev" "envs/dev/goldengate-monitor/values.yaml" 2>/dev/null; then
  pass "8: gg-monitor still references GoldenGateMonitorReadRole-dev"
else
  fail "8: envs/dev/goldengate-monitor/values.yaml no longer references GoldenGateMonitorReadRole-dev"
fi

# 11. Terraform references remain valid: iam.tf's module block still exists,
# still names the same role, and still attaches the same policy_folder.
if grep -q 'module "goldengate_secrets_read_role_dev"' envs/dev/iam.tf \
    && grep -q 'name          = "GoldenGateSecretsReadRole-dev"' envs/dev/iam.tf \
    && grep -q 'policy_folder = "goldengate-secrets-read-dev"' envs/dev/iam.tf \
    && grep -q 'module "goldengate_monitor_read_role_dev"' envs/dev/iam.tf \
    && grep -q 'name          = "GoldenGateMonitorReadRole-dev"' envs/dev/iam.tf \
    && grep -q 'policy_folder = "goldengate-monitor-read-dev"' envs/dev/iam.tf; then
  pass "11: envs/dev/iam.tf's module blocks still name the same roles and attach the same policy_folder values"
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

# 12. No manager metric/DynamoDB/lease behavior changed: collector.py and
# monitor.py are untouched by this IAM-only phase.
IAM_PHASE_COLLECTOR_DIFF="$(git diff --stat -- monitoring/monitor/collector.py 2>/dev/null || true)"
IAM_PHASE_MONITOR_DIFF="$(git diff --stat -- monitoring/monitor/monitor.py 2>/dev/null || true)"
if [ -z "$IAM_PHASE_COLLECTOR_DIFF" ] && [ -z "$IAM_PHASE_MONITOR_DIFF" ]; then
  pass "12: collector.py and monitor.py are unchanged -- no manager metric/DynamoDB/lease behavior was altered"
else
  fail "12: collector.py and/or monitor.py were unexpectedly modified during an IAM-only phase"
fi

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

# ---------------------------------------------------------------------
# 24. No accidental pasted command-note files under hack/.
# ---------------------------------------------------------------------
echo ""
echo "--- No accidental command-note files under hack/ ---"

if [ -f "hack/test.yaml" ]; then
  fail "hack/test.yaml exists -- this was an accidental pasted VDR command note and is not a legitimate repository file"
else
  pass "hack/test.yaml does not exist"
fi

# Generic guard: any *.yaml/*.yml file anywhere under hack/ must actually
# parse as YAML -- a plain-prose/shell command note accidentally saved with
# a .yaml/.yml extension (exactly how hack/test.yaml happened) is caught
# here even if it is renamed or a new one is added later.
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

# ---------------------------------------------------------------------
# 25. Repository hygiene: proven-dead file cleanup regression checks.
# ---------------------------------------------------------------------
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

if ! grep -rn "payments-ora-to-pg-001" envs/dev/goldengate-deployments.yaml 2>/dev/null | grep -qv "pipeline:"; then
  pass "21: no active deployment-registry configuration references the retired deployment folder (only the shared logical pipeline: grouping id remains, which is unrelated and intentionally preserved)"
else
  fail "21: envs/dev/goldengate-deployments.yaml appears to reference the retired deployment beyond the shared pipeline: grouping id"
fi

CANONICAL_PRESENCE_MISSING=""
for f in \
  envs/dev/gg-oracle-payments-01/values.yaml \
  envs/dev/gg-postgresql-payments-01/values.yaml \
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

# ---------------------------------------------------------------------
# 26. EFS rendered-resource validation: strict basePath derivation
#     (matching goldengate.efsBasePath), fail-closed YAML parsing, no
#     fragile grep on an optional key under set -euo pipefail.
# ---------------------------------------------------------------------
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

    run_efs_step() {
      ( cd "$EFS_WORKDIR" && \
        RELEASE_NAME="$1" VALUES_FILE="$2" DEPLOYMENT_ID="$3" DEPLOYMENT_MODEL="$4" ENVIRONMENT="$5" \
        bash "${WORKDIR}/efs_validate.sh" 2>&1 )
      return $?
    }

    helm template gg-oracle-payments-01 "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" \
      --set global.environment=dev --set global.deploymentId=gg-oracle-payments-01 \
      > "${EFS_WORKDIR}/rendered/gg-oracle-payments-01.yaml" 2>"${EFS_WORKDIR}/oracle-render.err" || true
    helm template gg-postgresql-payments-01 "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "${REPO_ROOT}/envs/dev/gg-postgresql-payments-01/values.yaml" \
      --set global.environment=dev --set global.deploymentId=gg-postgresql-payments-01 \
      > "${EFS_WORKDIR}/rendered/gg-postgresql-payments-01.yaml" 2>"${EFS_WORKDIR}/postgres-render.err" || true
    set +e
    ORACLE_OUT="$(run_efs_step "gg-oracle-payments-01" "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    ORACLE_STATUS=$?
    set -e
    echo "$ORACLE_OUT"
    if [ "$ORACLE_STATUS" -eq 0 ] && echo "$ORACLE_OUT" | grep -qF "Expected EFS basePath: /gg-oracle-payments-01"; then
      pass "1: gg-oracle-payments-01 (no explicit basePath) resolves to /gg-oracle-payments-01"
    else
      fail "1: gg-oracle-payments-01 basePath derivation failed or produced an unexpected value"
    fi

    set +e
    POSTGRES_OUT="$(run_efs_step "gg-postgresql-payments-01" "${REPO_ROOT}/envs/dev/gg-postgresql-payments-01/values.yaml" "gg-postgresql-payments-01" "singleRuntime" "dev")"
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
with open('${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml') as f:
    data = yaml.safe_load(f)
data['persistence']['efs']['storageClass']['basePath'] = '/custom-override-path'
with open('${EFS_WORKDIR}/values/oracle-override.yaml', 'w') as f:
    yaml.dump(data, f)
"
    helm template gg-oracle-payments-01 "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "${EFS_WORKDIR}/values/oracle-override.yaml" \
      --set global.environment=dev --set global.deploymentId=gg-oracle-payments-01 \
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

    # 5: a missing fileSystemId fails with a clear controlled error (never
    # an unexplained shell abort).
    cat > "${EFS_WORKDIR}/values/missing-fsid.yaml" <<'EOF'
deploymentModel: singleRuntime
persistence:
  enabled: true
  provider: efs
  efs:
    storageClass:
      basePath: /x
EOF
    set +e
    MISSING_FSID_OUT="$(run_efs_step "x" "${EFS_WORKDIR}/values/missing-fsid.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    MISSING_FSID_STATUS=$?
    set -e
    if [ "$MISSING_FSID_STATUS" -ne 0 ] && echo "$MISSING_FSID_OUT" | grep -qF "persistence.efs.fileSystemId must be a non-empty string"; then
      pass "5: a missing fileSystemId fails with a clear controlled error"
    else
      fail "5: a missing fileSystemId did not fail with the expected controlled error"
      echo "$MISSING_FSID_OUT"
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

    # 7: an unknown deploymentModel fails closed (this EFS validation step
    # now only ever expects deployment_model=singleRuntime, passed through
    # from the job's own upstream assertion -- never re-inferred here).
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

    # 8/9/11: mutate a real rendered manifest's StorageClass to prove the
    # rendered-resource checks have teeth (wrong basePath, wrong
    # fileSystemId, and a duplicate matching-name StorageClass).
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
    WRONG_BASEPATH_OUT="$(run_efs_step "wrong-basepath" "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
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
    WRONG_FSID_OUT="$(run_efs_step "wrong-fsid" "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    WRONG_FSID_STATUS=$?
    set -e
    if [ "$WRONG_FSID_STATUS" -ne 0 ] && echo "$WRONG_FSID_OUT" | grep -qF "parameters.fileSystemId"; then
      pass "9: a rendered StorageClass with the wrong filesystem ID fails"
    else
      fail "9: a rendered StorageClass with the wrong filesystem ID did not fail as expected"
      echo "$WRONG_FSID_OUT"
    fi

    # 10: absence of the optional basePath key never causes an unexplained
    # shell exit -- structural proof (the fragile grep pattern is gone) plus
    # behavioral proof (tests 1/2 above already completed with a clean
    # PASS/FAIL verdict, not a raw "unbound variable"/pipefail abort).
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
    DUP_SC_OUT="$(run_efs_step "duplicate-storageclass" "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" "gg-oracle-payments-01" "singleRuntime" "dev")"
    DUP_SC_STATUS=$?
    set -e
    if [ "$DUP_SC_STATUS" -ne 0 ] && echo "$DUP_SC_OUT" | grep -qF "expected exactly one StorageClass"; then
      pass "11: exactly one expected StorageClass is required (a duplicate is rejected)"
    else
      fail "11: a duplicate matching-name StorageClass was not rejected as expected"
      echo "$DUP_SC_OUT"
    fi

    # 22: legacyPair Helm rendering is rejected with a clear controlled
    # error (the chart no longer implements legacyPair source/target
    # rendering -- it was removed in Phase 5B2A). Also confirm an unknown
    # deploymentModel fails closed the same way.
    set +e
    LEGACY_REJECT_ERR="$(helm template ogg-legacy-reject "$RUNTIME_CHART" --namespace goldengate-dev \
      --values "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" \
      --set global.environment=dev --set global.deploymentId=ogg-legacy-reject \
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
      --values "${REPO_ROOT}/envs/dev/gg-oracle-payments-01/values.yaml" \
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

    # 13: this EFS-only correction did not touch observer removal or the
    # workflow-matrix classifier logic elsewhere in the same file.
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

# ---------------------------------------------------------------------
# Phase 5B2A workflow-compilation-size correction: the "Detect changed
# deployments" step's inline run: scalar previously reached ~23,971 UTF-8
# characters, above GitHub Actions' ~21,000-character limit for a single
# run: command -- GitHub rejected the whole workflow file at compile time
# (falling back to displaying it by file path, not its configured name/
# run-name). The fix moves the real implementation into the tracked,
# executable hack/detect-goldengate-deployments.sh; the workflow step is
# now only a small env:-mapping-plus-invocation wrapper. These tests prove
# the fix and guard against regressing back over the limit.
# ---------------------------------------------------------------------
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

  # 1: the "Detect changed deployments" run: body is below GitHub's
  # 21,000-character limit (the exact defect this phase fixes).
  DETECT_STEP_LENGTH="$(echo "$RUN_LENGTHS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['detect_step_length'])")"
  if [ -n "$DETECT_STEP_LENGTH" ] && [ "$DETECT_STEP_LENGTH" -lt 21000 ]; then
    pass "1: the 'Detect changed deployments' run: body (${DETECT_STEP_LENGTH} chars) is below GitHub's 21,000-character run: limit"
  else
    fail "1: the 'Detect changed deployments' run: body is missing or still at/above the 21,000-character limit (length=${DETECT_STEP_LENGTH:-<missing>})"
  fi

  # 2: safety margin -- every run: scalar in the whole workflow is below
  # 18,000 characters, not just below the hard 21,000 limit.
  MAX_RUN_LENGTH="$(echo "$RUN_LENGTHS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['max_length'])")"
  if [ -n "$MAX_RUN_LENGTH" ] && [ "$MAX_RUN_LENGTH" -lt 18000 ]; then
    pass "2: every run: scalar in ${EKS_APP_WORKFLOW} is below the 18,000-character safety margin (max=${MAX_RUN_LENGTH})"
  else
    fail "2: at least one run: scalar in ${EKS_APP_WORKFLOW} is at/above the 18,000-character safety margin (max=${MAX_RUN_LENGTH:-<missing>})"
  fi

  # 3/4: the workflow header has a non-empty name and run-name. PyYAML
  # (YAML 1.1) parses an unquoted top-level "on" key as the boolean True,
  # not the string "on" -- that is expected and must not be treated as a
  # missing/malformed key here or anywhere else this script inspects the
  # parsed workflow document.
  HEADER_CHECK="$(python3 - "$EKS_APP_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as f:
    doc = yaml.safe_load(f)

name_ok = isinstance(doc.get("name"), str) and doc.get("name").strip() != ""
run_name_ok = isinstance(doc.get("run-name"), str) and doc.get("run-name").strip() != ""

# YAML 1.1 boolean-key quirk: PyYAML resolves the unquoted key "on" to the
# Python boolean True. Both True and the literal string "on" are accepted
# here as "the trigger key is present" -- this must never be scored as a
# missing key/false failure.
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

  # 6: no second, embedded copy of _classify_deployment_model_yaml remains
  # inside the workflow YAML -- the one and only implementation lives in
  # ${DETECT_SCRIPT}.
  CLASSIFIER_IN_WORKFLOW_COUNT="$(grep -c "_classify_deployment_model_yaml() {" "$EKS_APP_WORKFLOW" || true)"
  if [ "${CLASSIFIER_IN_WORKFLOW_COUNT:-0}" -eq 0 ]; then
    pass "6: no embedded copy of _classify_deployment_model_yaml exists inside ${EKS_APP_WORKFLOW}"
  else
    fail "6: ${EKS_APP_WORKFLOW} still contains an embedded _classify_deployment_model_yaml definition (found ${CLASSIFIER_IN_WORKFLOW_COUNT})"
  fi

  # 9: workflow input/context expressions are mapped through a step-level
  # env: block, never pasted directly into the external shell
  # implementation. Checked two ways: the workflow step's env: mapping
  # carries INPUT_*/EVENT_NAME/BEFORE_SHA/AFTER_SHA, and the external
  # script itself contains no "${{ ... }}" GitHub Actions expression
  # syntax at all (it only ever reads plain shell environment variables).
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
  # 7: the external script is executable, or is explicitly invoked
  # through bash regardless of its own executable bit (the workflow
  # wrapper always does `bash hack/detect-goldengate-deployments.sh`, so
  # either property alone is sufficient -- this test accepts either).
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

# ---------------------------------------------------------------------
# Phase 5B2B1: read-only legacy external-resource cleanup inventory.
# hack/inventory-goldengate-legacy-resources.sh and
# .github/workflows/goldengate-legacy-cleanup-inventory.yaml never create,
# modify, or delete any AWS/Kubernetes resource -- these tests prove that,
# prove the canonical deny-list can never be overridden by any observed
# evidence, and prove permission gaps are reported rather than guessed.
# ---------------------------------------------------------------------
echo ""
echo "--- Phase 5B2B1: read-only legacy cleanup inventory ---"

FORBIDDEN_MUTATION_PATTERN='kubectl (delete|patch|apply|edit|scale|rollout restart)|aws efs delete-access-point|aws dynamodb (delete-item|batch-write-item)|aws ecr (delete-repository|batch-delete-image)|aws route53 change-resource-record-sets|aws secretsmanager delete-secret|terraform apply|helm (install|upgrade)|argocd app delete'

if [ -f "$INVENTORY_WORKFLOW" ] && [ "$PYTHON_AVAILABLE" = "true" ]; then
  INVENTORY_HEADER_CHECK="$(python3 - "$INVENTORY_WORKFLOW" <<'PYEOF'
import sys
import yaml

with open(sys.argv[1], encoding="utf-8") as f:
    doc = yaml.safe_load(f)

# YAML 1.1 boolean-key quirk: PyYAML resolves the unquoted key "on" to the
# Python boolean True -- accept either, matching the same handling used for
# goldengate-eks-app.yaml elsewhere in this test suite.
on_key = True if True in doc else "on"
triggers = doc.get(on_key, {}) or {}

print(f"only_workflow_dispatch={sorted(triggers.keys()) == ['workflow_dispatch']}")
print(f"has_push={'push' in triggers}")

inputs = ((triggers.get("workflow_dispatch") or {}).get("inputs")) or {}
print(f"input_names={sorted(inputs.keys())}")

mutation_keywords = ("apply", "delete", "mutate", "mutation", "confirm", "destructive", "force")
suspicious_inputs = [name for name in inputs if any(k in name.lower() for k in mutation_keywords)]
print(f"suspicious_inputs={suspicious_inputs}")

run_lengths = []
for job in doc.get("jobs", {}).values():
    for step in job.get("steps", []):
        run = step.get("run")
        if run is not None:
            run_lengths.append(len(run.encode("utf-8")))
print(f"max_run_length={max(run_lengths) if run_lengths else 0}")

detect_calls_script = any(
    "bash hack/inventory-goldengate-legacy-resources.sh" in (step.get("run") or "")
    for job in doc.get("jobs", {}).values()
    for step in job.get("steps", [])
)
print(f"invokes_script={detect_calls_script}")
PYEOF
)"
  echo "$INVENTORY_HEADER_CHECK"

  # 1/2: workflow_dispatch-only, no push trigger.
  if echo "$INVENTORY_HEADER_CHECK" | grep -q "^only_workflow_dispatch=True$"; then
    pass "1: ${INVENTORY_WORKFLOW} is workflow_dispatch-only"
  else
    fail "1: ${INVENTORY_WORKFLOW} is not workflow_dispatch-only"
  fi

  if echo "$INVENTORY_HEADER_CHECK" | grep -q "^has_push=False$"; then
    pass "2: ${INVENTORY_WORKFLOW} has no push trigger"
  else
    fail "2: ${INVENTORY_WORKFLOW} unexpectedly has a push trigger"
  fi

  # 3: no mutation input -- exactly the minimal safe "environment" input,
  # and no input name suggests an apply/delete/mutation mode.
  if echo "$INVENTORY_HEADER_CHECK" | grep -q "^input_names=\['environment'\]$" \
      && echo "$INVENTORY_HEADER_CHECK" | grep -q "^suspicious_inputs=\[\]$"; then
    pass "3: ${INVENTORY_WORKFLOW} has no mutation input (only environment)"
  else
    fail "3: ${INVENTORY_WORKFLOW} has an unexpected or suspicious input"
  fi

  # 5 (part): the workflow step invokes the real external script, never a
  # duplicated inline implementation.
  if echo "$INVENTORY_HEADER_CHECK" | grep -q "^invokes_script=True$"; then
    pass "${INVENTORY_WORKFLOW} invokes ${INVENTORY_SCRIPT} rather than embedding its own implementation"
  else
    fail "${INVENTORY_WORKFLOW} does not invoke ${INVENTORY_SCRIPT}"
  fi

  # 20: every run: block in every workflow file (not just this new one)
  # stays below GitHub's 21,000-character limit.
  ALL_WORKFLOW_MAX_LENGTH=0
  for wf in .github/workflows/*.yaml .github/workflows/*.yml; do
    [ -f "$wf" ] || continue
    WF_MAX="$(python3 - "$wf" <<'PYEOF'
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as f:
    doc = yaml.safe_load(f)
lengths = [len((step.get("run") or "").encode("utf-8"))
           for job in (doc.get("jobs") or {}).values()
           for step in job.get("steps", [])]
print(max(lengths) if lengths else 0)
PYEOF
)"
    if [ "${WF_MAX:-0}" -gt "$ALL_WORKFLOW_MAX_LENGTH" ]; then
      ALL_WORKFLOW_MAX_LENGTH="$WF_MAX"
    fi
  done
  if [ "$ALL_WORKFLOW_MAX_LENGTH" -lt 21000 ]; then
    pass "20: every run: block across all workflow files is below 21,000 characters (max=${ALL_WORKFLOW_MAX_LENGTH})"
  else
    fail "20: at least one run: block across the workflow files is at/above 21,000 characters (max=${ALL_WORKFLOW_MAX_LENGTH})"
  fi
else
  skip "inventory workflow header/trigger checks -- ${INVENTORY_WORKFLOW} or python3 not available"
fi

if [ -f "$INVENTORY_WORKFLOW" ]; then
  WORKFLOW_MUTATION_HITS="$(grep -nE "$FORBIDDEN_MUTATION_PATTERN" "$INVENTORY_WORKFLOW" || true)"
  if [ -z "$WORKFLOW_MUTATION_HITS" ]; then
    pass "${INVENTORY_WORKFLOW} contains no forbidden mutation command"
  else
    fail "${INVENTORY_WORKFLOW} contains forbidden mutation command(s):"$'\n'"${WORKFLOW_MUTATION_HITS}"
  fi
fi

if [ -f "$INVENTORY_SCRIPT" ]; then
  # 4: the script contains no forbidden mutation command.
  SCRIPT_MUTATION_HITS="$(grep -nE "$FORBIDDEN_MUTATION_PATTERN" "$INVENTORY_SCRIPT" || true)"
  if [ -z "$SCRIPT_MUTATION_HITS" ]; then
    pass "4: ${INVENTORY_SCRIPT} contains no forbidden mutation command"
  else
    fail "4: ${INVENTORY_SCRIPT} contains forbidden mutation command(s):"$'\n'"${SCRIPT_MUTATION_HITS}"
  fi

  bash -n "$INVENTORY_SCRIPT" >/dev/null 2>&1 && pass "${INVENTORY_SCRIPT} passes bash -n syntax check" || fail "${INVENTORY_SCRIPT} fails bash -n syntax check"

  [ -x "$INVENTORY_SCRIPT" ] && pass "7: ${INVENTORY_SCRIPT} is executable" || fail "7: ${INVENTORY_SCRIPT} is not executable"

  # 5: the four known old PV IDs are exactly the script's candidate list.
  EXPECTED_PV_CANDIDATES="pvc-3a93c990-a9fa-4cca-99df-7c3375472074 pvc-93251c3f-c408-4713-bd46-ebc5e0eafa8a pvc-5c43940e-1054-43f5-8031-9db4b51a024a pvc-bacb3e9d-d904-467c-959f-dea9548699c9"
  PV_CANDIDATES_MISSING=""
  for pv_id in $EXPECTED_PV_CANDIDATES; do
    grep -qF "\"${pv_id}\"" "$INVENTORY_SCRIPT" || PV_CANDIDATES_MISSING="${PV_CANDIDATES_MISSING} ${pv_id}"
  done
  if [ -z "$PV_CANDIDATES_MISSING" ]; then
    pass "5: all four known old PV IDs are present as inventory candidates in ${INVENTORY_SCRIPT}"
  else
    fail "5: ${INVENTORY_SCRIPT} is missing expected candidate PV ID(s):${PV_CANDIDATES_MISSING}"
  fi

  # 11: DynamoDB inventory uses Query against an exact partition key, never
  # a table-wide Scan.
  if grep -qE "(aws )?dynamodb query" "$INVENTORY_SCRIPT" && ! grep -qE "(aws )?dynamodb scan| --scan " "$INVENTORY_SCRIPT"; then
    pass "11: ${INVENTORY_SCRIPT} uses 'dynamodb query' (exact partition key) and never 'dynamodb scan'"
  else
    fail "11: ${INVENTORY_SCRIPT} does not exclusively use Query for DynamoDB (scan present or query absent)"
  fi

  # 13: missing EFS/ECR permissions produce a permission-gap literal,
  # exactly as specified, rather than the script guessing eligibility.
  if grep -q "EFS_METADATA_PERMISSION_MISSING" "$INVENTORY_SCRIPT" && grep -q "OBSERVER_ECR_PERMISSION_MISSING" "$INVENTORY_SCRIPT"; then
    pass "13: ${INVENTORY_SCRIPT} reports EFS_METADATA_PERMISSION_MISSING and OBSERVER_ECR_PERMISSION_MISSING on missing permissions"
  else
    fail "13: ${INVENTORY_SCRIPT} is missing the required permission-gap literal(s)"
  fi

  # 14: the manifest JSON schema contains every required top-level and
  # candidates sub-key.
  SCHEMA_KEYS_MISSING=""
  for key in environment generatedAt baseline canonicalBaselineVerified inventoryComplete eligibilityReady canonical candidates blocked permissionGaps; do
    grep -qE "^\s*${key}:" "$INVENTORY_SCRIPT" || SCHEMA_KEYS_MISSING="${SCHEMA_KEYS_MISSING} ${key}"
  done
  for key in persistentVolumes efsAccessPoints storageClasses dynamodbPartitions ecrRepositories ecrImages; do
    grep -qE "^\s*${key}:" "$INVENTORY_SCRIPT" || SCHEMA_KEYS_MISSING="${SCHEMA_KEYS_MISSING} ${key}"
  done
  if [ -z "$SCHEMA_KEYS_MISSING" ]; then
    pass "14: the manifest JSON schema in ${INVENTORY_SCRIPT} contains every required key"
  else
    fail "14: the manifest JSON schema in ${INVENTORY_SCRIPT} is missing key(s):${SCHEMA_KEYS_MISSING}"
  fi

  # PVCs have no candidate resourceType at all in the schema -- structurally
  # deny-listed (7): there is no "persistentVolumeClaims" candidate array,
  # so a PVC can never appear as a cleanup candidate regardless of any
  # observed state.
  if ! grep -qE "^\s*persistentVolumeClaims:" "$INVENTORY_SCRIPT"; then
    pass "7: PersistentVolumeClaims have no candidate resourceType in the manifest schema -- structurally deny-listed"
  else
    fail "7: ${INVENTORY_SCRIPT} unexpectedly defines a persistentVolumeClaims candidate list"
  fi

  # 15: no secret-value retrieval anywhere in the script (Secrets Manager
  # GetSecretValue, or any other "get secret value" shaped call).
  if grep -qiE "get-secret-value|getsecretvalue" "$INVENTORY_SCRIPT"; then
    fail "15: ${INVENTORY_SCRIPT} appears to retrieve a secret value"
  else
    pass "15: ${INVENTORY_SCRIPT} never retrieves a secret value"
  fi

  # Confirm the script never logs/echoes a raw AWS Secret string value
  # (only Secrets Manager *paths*, e.g. dev/goldengate/source/admin, are
  # ever referenced -- paths are identifiers, not secret values).
  if grep -qE "SecretString|secretString" "$INVENTORY_SCRIPT"; then
    fail "15b: ${INVENTORY_SCRIPT} appears to reference a raw secret string field"
  else
    pass "15b: ${INVENTORY_SCRIPT} never references a raw secret string field"
  fi
else
  fail "${INVENTORY_SCRIPT} does not exist"
fi

if [ -f "$INVENTORY_SCRIPT" ] && command -v python3 >/dev/null 2>&1; then
  # Extract just the constants + pure classification functions (never the
  # live AWS/kubectl collection code) so eligibility logic can be unit-
  # tested deterministically, the same established pattern already used
  # elsewhere in this file for the detection script's classifier.
  python3 - "$INVENTORY_SCRIPT" > "${WORKDIR}/inventory_classify_funcs.sh" <<'PYEOF'
import re
import sys

with open(sys.argv[1]) as f:
    lines = f.readlines()

start = next(i for i, l in enumerate(lines) if l.startswith("ENVIRONMENT="))
end = None
for i, l in enumerate(lines):
    if l.startswith("classify_observer_image() {"):
        for j in range(i, len(lines)):
            if lines[j].strip() == "}":
                end = j
                break
        break

if end is None:
    sys.exit("could not locate classify_observer_image() function body")

# Drop the "prerequisites" tool-check block (MISSING_TOOLS.. through
# in_array()) -- not needed and not relevant for pure-function testing;
# keeping it would make this fixture depend on jq/python3 being on PATH
# purely to reach the functions under test.
body = "".join(lines[start:end + 1])
prereq_start = body.find("MISSING_TOOLS=()")
prereq_end = body.find("in_array()")
if prereq_start != -1 and prereq_end != -1:
    body = body[:prereq_start] + body[prereq_end:]

sys.stdout.write("set -uo pipefail\n")
sys.stdout.write(body)
PYEOF

  if [ -s "${WORKDIR}/inventory_classify_funcs.sh" ] && bash -n "${WORKDIR}/inventory_classify_funcs.sh" >/dev/null 2>&1; then
    # Focused pure-function assertions. classify_pv/classify_observer_image/
    # classify_ecr_repository signatures below match the PV active-PVC-
    # reference and ECR image-inventory-gating corrections -- baseline=false
    # blocks eligibility; a false *ReferenceCheckVerified flag blocks
    # eligibility; canonical resources never enter candidates; an absent
    # legacy StorageClass is already_absent, not eligible; a non-matching
    # observer tag is blocked; a PVC-list read failure blocks PV
    # eligibility; an active PVC reference blocks PV eligibility; an ECR
    # image-inventory read failure blocks repository eligibility; a
    # repository URI mismatch blocks image eligibility.
    INVENTORY_CLASSIFY_OUTPUT="$(bash -c '
      source "'"${WORKDIR}"'/inventory_classify_funcs.sh"
      set +e

      echo "--- 1: baseline=false blocks an otherwise-fully-eligible PV ---"
      classify_pv "pvc-3a93c990-a9fa-4cca-99df-7c3375472074" "Released" "Retain" "fs-05cadf3570f23cd39::fsap-007cfc2ff801c24b8" "true" "true" "false" "true" "false" "false"
      echo "exit=$?"

      echo "--- baseline=true, fully verified old PV is eligible (control case) ---"
      classify_pv "pvc-3a93c990-a9fa-4cca-99df-7c3375472074" "Released" "Retain" "fs-05cadf3570f23cd39::fsap-007cfc2ff801c24b8" "true" "true" "false" "true" "false" "true"
      echo "exit=$?"

      echo "--- 2: podReferenceCheckVerified=false blocks eligibility even though referenced=false ---"
      classify_pv "pvc-93251c3f-c408-4713-bd46-ebc5e0eafa8a" "Released" "Retain" "fs-05cadf3570f23cd39::fsap-035f46f17955f57cb" "true" "true" "false" "false" "false" "true"
      echo "exit=$?"

      echo "--- 3a: a current canonical PV is never eligible, even with every other fact true ---"
      classify_pv "pvc-dd1bc7bc-b736-4fee-abfe-abf622e70550" "Released" "Retain" "fs-05cadf3570f23cd39::fsap-canonical1" "true" "true" "false" "true" "false" "true"
      echo "exit=$?"

      echo "--- 3b: a current canonical EFS access point is never eligible ---"
      CANONICAL_EFS_ACCESS_POINT_IDS=("fsap-canonical1" "fsap-canonical2")
      classify_efs_access_point "fsap-canonical1" "true" "fs-05cadf3570f23cd39" "available" "true" "false" "true"
      echo "exit=$?"

      echo "--- 4: an absent legacy StorageClass is already_absent, not eligible ---"
      classify_legacy_storage_class "false" "true" "false" "true"
      echo "exit=$?"

      echo "--- legacy StorageClass present, proven unused, baseline verified -> eligible (control case) ---"
      classify_legacy_storage_class "true" "true" "false" "true"
      echo "exit=$?"

      echo "--- 5: an observer image tag that does not match the pattern is blocked, not eligible ---"
      classify_observer_image "[\"latest\"]" "true" "true" "true" 0 "true"
      echo "exit=$?"

      echo "--- observer image with a matching tag and zero verified references is eligible (control case) ---"
      classify_observer_image "[\"obs-abc123def456\"]" "true" "true" "true" 0 "true"
      echo "exit=$?"

      echo "--- 6: PVC reference verification failure blocks PV eligibility ---"
      classify_pv "pvc-3a93c990-a9fa-4cca-99df-7c3375472074" "Released" "Retain" "fs-05cadf3570f23cd39::fsap-007cfc2ff801c24b8" "true" "false" "false" "true" "false" "true"
      echo "exit=$?"

      echo "--- 7: an active PVC reference blocks PV eligibility ---"
      classify_pv "pvc-3a93c990-a9fa-4cca-99df-7c3375472074" "Released" "Retain" "fs-05cadf3570f23cd39::fsap-007cfc2ff801c24b8" "true" "true" "true" "true" "false" "true"
      echo "exit=$?"

      echo "--- 8: ECR image-inventory verification failure blocks repository eligibility ---"
      classify_ecr_repository "uri" "true" "true" "true" 0 "false" "false" "true"
      echo "exit=$?"

      echo "--- ECR repository fully verified with an empty, verified image inventory is eligible (control case) ---"
      classify_ecr_repository "uri" "true" "true" "true" 0 "true" "false" "true"
      echo "exit=$?"

      echo "--- 9: repository URI mismatch blocks image eligibility ---"
      classify_observer_image "[\"obs-abc123def456\"]" "false" "true" "true" 0 "true"
      echo "exit=$?"
    ' 2>&1)"
    echo "$INVENTORY_CLASSIFY_OUTPUT"

    if echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "1: baseline=false blocks an otherwise-fully-eligible PV" | grep -q "^exit=1$" \
        && echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "baseline=true, fully verified old PV is eligible" | grep -q "^exit=0$"; then
      pass "1: canonical_baseline_verified=false blocks an otherwise-eligible candidate (control case confirms baseline=true allows it)"
    else
      fail "1: canonical_baseline_verified=false did not block eligibility as expected"
    fi

    if echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "2: podReferenceCheckVerified=false blocks eligibility" | grep -q "^exit=1$"; then
      pass "2: podReferenceCheckVerified=false blocks eligibility even when referencedByRunningPod=false"
    else
      fail "2: podReferenceCheckVerified=false did not block eligibility as expected"
    fi

    if echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "3a: a current canonical PV is never eligible" | grep -q "^exit=1$" \
        && echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "3b: a current canonical EFS access point is never eligible" | grep -q "^exit=1$"; then
      pass "3: canonical PV/EFS-access-point identifiers are never eligible, regardless of other evidence"
    else
      fail "3: a canonical resource was not blocked as expected"
    fi

    if echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "4: an absent legacy StorageClass is already_absent" | grep -q "^exit=2$" \
        && echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "legacy StorageClass present, proven unused, baseline verified -> eligible" | grep -q "^exit=0$"; then
      pass "4: an absent legacy StorageClass reports already_absent (exit 2), never eligible; a present+unused+baseline-verified one is eligible"
    else
      fail "4: legacy StorageClass existence-first classification did not behave as expected"
    fi

    if echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "5: an observer image tag that does not match the pattern is blocked" | grep -q "^exit=1$" \
        && echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "observer image with a matching tag and zero verified references is eligible" | grep -q "^exit=0$"; then
      pass "5: an observer image tag not matching ^obs-[0-9a-f]{12}\$ is blocked, never eligible"
    else
      fail "5: non-matching observer image tag was not blocked as expected"
    fi

    if echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "6: PVC reference verification failure blocks PV eligibility" | grep -q "^exit=1$"; then
      pass "6: a PVC-list (pvcReferenceCheckVerified=false) read failure blocks PV eligibility"
    else
      fail "6: pvcReferenceCheckVerified=false did not block PV eligibility as expected"
    fi

    if echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "7: an active PVC reference blocks PV eligibility" | grep -q "^exit=1$"; then
      pass "7: a PV referenced by an active PVC (referencedByActivePvc=true) is blocked, never eligible"
    else
      fail "7: an active PVC reference did not block PV eligibility as expected"
    fi

    if echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "8: ECR image-inventory verification failure blocks repository eligibility" | grep -q "^exit=1$" \
        && echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "ECR repository fully verified with an empty, verified image inventory is eligible" | grep -q "^exit=0$"; then
      pass "8: imageInventoryVerified=false blocks repository eligibility; a verified empty image inventory does not"
    else
      fail "8: ECR image-inventory gating did not behave as expected"
    fi

    if echo "$INVENTORY_CLASSIFY_OUTPUT" | grep -A2 "9: repository URI mismatch blocks image eligibility" | grep -q "^exit=1$"; then
      pass "9: repositoryUriMatch=false blocks observer image eligibility, even with a matching tag and zero references"
    else
      fail "9: repository URI mismatch did not block image eligibility as expected"
    fi
  else
    fail "could not extract or syntax-validate the pure classification functions from ${INVENTORY_SCRIPT}"
  fi
else
  skip "inventory classification unit tests -- ${INVENTORY_SCRIPT} or python3 not available"
fi

if [ -f "$INVENTORY_SCRIPT" ] && command -v python3 >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
  # Dual-account correction (Phase 5B2B1 account-context correction) focused
  # checks. These extract the exact production account-baseline block and
  # the exact production canonical-monitor-validation block (never the rest
  # of Section 4, which makes real kubectl/aws calls at source time) and run
  # them under small in-test stub run_aws_json/run_workload_aws_json/
  # run_kubectl_json/add_permission_gap functions -- never a fake aws/
  # kubectl binary or an end-to-end integration-test framework.
  python3 - "$INVENTORY_SCRIPT" > "${WORKDIR}/inventory_account_block.sh" <<'PYEOF'
import sys

with open(sys.argv[1]) as f:
    lines = f.readlines()

const_start = next(i for i, l in enumerate(lines) if l.startswith("ENVIRONMENT="))
const_end = next(i for i, l in enumerate(lines) if l.startswith("GENERATED_AT="))

block_start = next(i for i, l in enumerate(lines) if l.startswith('BASELINE_BUILD_ACCOUNT_OK="false"'))
marker = next(i for i, l in enumerate(lines) if "aws CLI not available on this runner" in l)
block_end = None
for j in range(marker, len(lines)):
    if lines[j].strip() == "fi":
        block_end = j
        break
if block_end is None:
    sys.exit("could not locate end of account baseline block")

sys.stdout.write("".join(lines[const_start:const_end]))
sys.stdout.write("".join(lines[block_start:block_end + 1]))
PYEOF

  python3 - "$INVENTORY_SCRIPT" > "${WORKDIR}/inventory_monitor_block.sh" <<'PYEOF'
import sys

with open(sys.argv[1]) as f:
    lines = f.readlines()

const_start = next(i for i, l in enumerate(lines) if l.startswith("ENVIRONMENT="))
const_end = next(i for i, l in enumerate(lines) if l.startswith("GENERATED_AT="))

block_start = next(i for i, l in enumerate(lines)
                    if l.startswith('echo "Validating shared monitor via canonical DynamoDB'))
marker = next(i for i, l in enumerate(lines)
              if "Monitor validation blocked: STALE_AFTER_SECONDS could not be obtained" in l)
block_end = marker + 1
if lines[block_end].strip() != "fi":
    sys.exit("could not locate end of monitor validation block")

sys.stdout.write("".join(lines[const_start:const_end]))
sys.stdout.write("".join(lines[block_start:block_end + 1]))
PYEOF

  if [ -s "${WORKDIR}/inventory_account_block.sh" ] && bash -n "${WORKDIR}/inventory_account_block.sh" >/dev/null 2>&1; then
    ACCOUNT_BLOCK_OUTPUT="$(bash -c '
      add_permission_gap() { :; }
      HAVE_AWS="true"
      AWS_REGION="eu-west-1"

      echo "--- separation: build session reports build account, workload session reports workload account ---"
      run_aws_json() { LAST_AWS_OK="true"; LAST_AWS_JSON="{\"Account\":\"229410149234\"}"; }
      run_workload_aws_json() { LAST_WORKLOAD_SESSION_OK="true"; LAST_WORKLOAD_AWS_OK="true"; LAST_WORKLOAD_AWS_JSON="{\"Account\":\"668311715351\"}"; }
      source "'"${WORKDIR}"'/inventory_account_block.sh"
      echo "buildOk=${BASELINE_BUILD_ACCOUNT_OK} workloadOk=${BASELINE_WORKLOAD_ACCOUNT_OK}"

      echo "--- mismatch: workload session actually resolves to the build account (wrong-account evidence) ---"
      run_aws_json() { LAST_AWS_OK="true"; LAST_AWS_JSON="{\"Account\":\"229410149234\"}"; }
      run_workload_aws_json() { LAST_WORKLOAD_SESSION_OK="true"; LAST_WORKLOAD_AWS_OK="true"; LAST_WORKLOAD_AWS_JSON="{\"Account\":\"229410149234\"}"; }
      source "'"${WORKDIR}"'/inventory_account_block.sh"
      echo "buildOk=${BASELINE_BUILD_ACCOUNT_OK} workloadOk=${BASELINE_WORKLOAD_ACCOUNT_OK}"

      echo "--- assume-role failure: workload session unavailable ---"
      run_aws_json() { LAST_AWS_OK="true"; LAST_AWS_JSON="{\"Account\":\"229410149234\"}"; }
      run_workload_aws_json() { LAST_WORKLOAD_SESSION_OK="false"; LAST_WORKLOAD_AWS_OK="false"; LAST_WORKLOAD_AWS_JSON=""; }
      source "'"${WORKDIR}"'/inventory_account_block.sh"
      echo "buildOk=${BASELINE_BUILD_ACCOUNT_OK} workloadOk=${BASELINE_WORKLOAD_ACCOUNT_OK}"
    ' 2>&1)"
    echo "$ACCOUNT_BLOCK_OUTPUT"

    if echo "$ACCOUNT_BLOCK_OUTPUT" | grep -A4 "separation: build session reports build account" | grep -q "^buildOk=true workloadOk=true$"; then
      pass "dual-account 1: build-account and workload-account sessions are independently verified against their own expected account IDs"
    else
      fail "dual-account 1: build/workload account separation did not behave as expected"
    fi

    if echo "$ACCOUNT_BLOCK_OUTPUT" | grep -A4 "mismatch: workload session actually resolves to the build account" | grep -q "^buildOk=true workloadOk=false$"; then
      pass "dual-account 2: a workload session that resolves to the build account is never treated as workloadAccountOk"
    else
      fail "dual-account 2: workload/build account mismatch was not detected as expected"
    fi

    if echo "$ACCOUNT_BLOCK_OUTPUT" | grep -A4 "assume-role failure: workload session unavailable" | grep -q "^buildOk=true workloadOk=false$"; then
      pass "dual-account 2b: a failed AssumeRole of EKS_DEPLOY_ROLE_ARN never becomes workloadAccountOk=true"
    else
      fail "dual-account 2b: an unavailable workload session was not correctly reported as workloadAccountOk=false"
    fi
  else
    fail "could not extract or syntax-validate the account baseline block from ${INVENTORY_SCRIPT}"
  fi

  if grep -qE '\[ "\$BASELINE_WORKLOAD_ACCOUNT_OK" = "true" \]' "$INVENTORY_SCRIPT" && ! grep -q 'BASELINE_ACCOUNT_OK=' "$INVENTORY_SCRIPT"; then
    pass "dual-account 2c: CANONICAL_BASELINE_VERIFIED requires BASELINE_WORKLOAD_ACCOUNT_OK, and the old single-session BASELINE_ACCOUNT_OK no longer exists"
  else
    fail "dual-account 2c: CANONICAL_BASELINE_VERIFIED does not require BASELINE_WORKLOAD_ACCOUNT_OK, or a stale BASELINE_ACCOUNT_OK reference remains"
  fi

  # 3: EFS access-point NotFound evidence must come from the workload-account
  # session (LAST_WORKLOAD_AWS_NOTFOUND / LAST_WORKLOAD_SESSION_OK), never
  # from the build-account session (LAST_AWS_NOTFOUND) -- a build-account
  # "not found" result is exactly the untrustworthy evidence this correction
  # removes.
  EFS_SECTION="$(sed -n '/^echo "--- C\. EFS access-point validation ---"$/,/^echo "--- D\. StorageClass validation ---"$/p' "$INVENTORY_SCRIPT")"
  if echo "$EFS_SECTION" | grep -q 'run_workload_aws_json efs describe-access-points' \
      && echo "$EFS_SECTION" | grep -q 'LAST_WORKLOAD_AWS_NOTFOUND' \
      && echo "$EFS_SECTION" | grep -q 'LAST_WORKLOAD_SESSION_OK' \
      && ! echo "$EFS_SECTION" | grep -qE 'run_aws_json efs|LAST_AWS_NOTFOUND|LAST_AWS_OK|LAST_AWS_JSON'; then
    pass "dual-account 3: EFS access-point NotFound evidence is derived only from the workload-account session, never the build-account session"
  else
    fail "dual-account 3: EFS access-point validation does not cleanly use the workload-account session for NotFound evidence"
  fi

  if [ -s "${WORKDIR}/inventory_monitor_block.sh" ] && bash -n "${WORKDIR}/inventory_monitor_block.sh" >/dev/null 2>&1; then
    MONITOR_BLOCK_OUTPUT="$(bash -c '
      add_permission_gap() { :; }
      HAVE_KUBECTL="true"
      run_kubectl_json() {
        LAST_KUBECTL_OK="true"
        LAST_KUBECTL_JSON="{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"env\":[{\"name\":\"STALE_AFTER_SECONDS\",\"value\":\"120\"}]}]}}}}"
      }

      echo "--- canonical CONFIG/LEASE/STATE success validates the monitor ---"
      run_workload_aws_json() {
        local args="$*" dep_type="" now recorded expires
        case "$args" in
          *gg-oracle-payments-01*) dep_type="oracle" ;;
          *gg-postgresql-payments-01*) dep_type="postgresql" ;;
        esac
        now="$(date -u +%s)"; recorded=$((now - 10)); expires=$((now + 500))
        LAST_WORKLOAD_SESSION_OK="true"; LAST_WORKLOAD_AWS_OK="true"
        LAST_WORKLOAD_AWS_JSON="$(jq -nc --arg t "$dep_type" --argjson recorded "$recorded" --argjson expires "$expires" \
          "{Items: [{recordType:{S:\"CONFIG\"}, metricsEnabled:{BOOL:true}, alertsEnabled:{BOOL:false}}, {recordType:{S:\"LEASE\"}, holder:{S:\"gg-monitor-0\"}, expiresAt:{N:(\$expires|tostring)}}, {recordType:{S:\"STATE#_deployment\"}, status:{S:\"UP\"}, recordedAt:{N:(\$recorded|tostring)}, deploymentType:{S:\$t}, criticalServices:{M:{adminsrvr:{M:{reachable:{BOOL:true}}}, distsrvr:{M:{reachable:{BOOL:true}}}, recvsrvr:{M:{reachable:{BOOL:true}}}}}}]}")"
      }
      source "'"${WORKDIR}"'/inventory_monitor_block.sh"
      echo "monitorValidated=${BASELINE_MONITOR_VALIDATED}"

      echo "--- a failed workload-account DynamoDB query blocks monitor validation ---"
      run_workload_aws_json() {
        LAST_WORKLOAD_SESSION_OK="true"; LAST_WORKLOAD_AWS_OK="false"; LAST_WORKLOAD_AWS_JSON=""
      }
      source "'"${WORKDIR}"'/inventory_monitor_block.sh"
      echo "monitorValidated=${BASELINE_MONITOR_VALIDATED}"
    ' 2>&1)"
    echo "$MONITOR_BLOCK_OUTPUT"

    if echo "$MONITOR_BLOCK_OUTPUT" | grep -A5 "canonical CONFIG/LEASE/STATE success validates the monitor" | grep -q "^monitorValidated=true$"; then
      pass "dual-account 5: fully-conforming canonical CONFIG/LEASE/STATE#_deployment records (via the workload-account session) validate the monitor"
    else
      fail "dual-account 5: canonical CONFIG/LEASE/STATE records that satisfy the manager contract did not validate the monitor"
    fi

    if echo "$MONITOR_BLOCK_OUTPUT" | grep -A5 "a failed workload-account DynamoDB query blocks monitor validation" | grep -q "^monitorValidated=false$"; then
      pass "dual-account 4: a failed workload-account DynamoDB Query blocks monitor validation, never silently passes it"
    else
      fail "dual-account 4: a failed DynamoDB query did not block monitor validation as expected"
    fi
  else
    fail "could not extract or syntax-validate the monitor validation block from ${INVENTORY_SCRIPT}"
  fi
else
  skip "dual-account inventory unit tests -- ${INVENTORY_SCRIPT}, python3, or jq not available"
fi

if [ -f "$INVENTORY_SCRIPT" ] && command -v python3 >/dev/null 2>&1; then
  # 10: eligibilityReady=false leaves no candidate with eligibility=eligible.
  # enforce_eligibility_readiness lives in Section 5 (after the live-
  # collection code), so it is extracted on its own -- never alongside
  # Section 4, which makes real kubectl/aws calls at source time and must
  # never be sourced in a local test.
  python3 - "$INVENTORY_SCRIPT" > "${WORKDIR}/inventory_enforce_fn.sh" <<'PYEOF'
import sys

with open(sys.argv[1]) as f:
    lines = f.readlines()

start = next(i for i, l in enumerate(lines) if l.startswith("enforce_eligibility_readiness() {"))
end = None
for j in range(start, len(lines)):
    if lines[j].strip() == "}":
        end = j
        break
if end is None:
    sys.exit("could not locate enforce_eligibility_readiness() function body")

sys.stdout.write("set -uo pipefail\n")
sys.stdout.writelines(lines[start:end + 1])
PYEOF

  if [ -s "${WORKDIR}/inventory_enforce_fn.sh" ] && bash -n "${WORKDIR}/inventory_enforce_fn.sh" >/dev/null 2>&1; then
    ENFORCE_OUTPUT="$(bash -c '
      source "'"${WORKDIR}"'/inventory_enforce_fn.sh"
      ELIGIBILITY_READY="false"
      CANDIDATES_PV="$(jq -nc "[{resourceType:\"PersistentVolume\", identifier:\"pv-1\", eligibility:\"eligible\", evidence:{foo:1}, blockingReasons:[]}, {resourceType:\"PersistentVolume\", identifier:\"pv-2\", eligibility:\"blocked\", evidence:{}, blockingReasons:[\"phase_not_released(Bound)\"]}]")"
      enforce_eligibility_readiness CANDIDATES_PV
      echo "$CANDIDATES_PV"
    ' 2>&1)"
    echo "$ENFORCE_OUTPUT"

    REMAINING_ELIGIBLE="$(echo "$ENFORCE_OUTPUT" | tail -1 | jq '[.[] | select(.eligibility=="eligible")] | length' 2>/dev/null || echo "parse_error")"
    EVIDENCE_PRESERVED="$(echo "$ENFORCE_OUTPUT" | tail -1 | jq -r '.[0].evidence.foo // empty' 2>/dev/null || echo "")"
    REASON_ADDED="$(echo "$ENFORCE_OUTPUT" | tail -1 | jq -r 'any(.[]; .blockingReasons | index("inventory_not_eligibility_ready") != null)' 2>/dev/null || echo "false")"

    if [ "$REMAINING_ELIGIBLE" = "0" ] && [ "$EVIDENCE_PRESERVED" = "1" ] && [ "$REASON_ADDED" = "true" ]; then
      pass "10: eligibilityReady=false leaves no candidate with eligibility=eligible (evidence preserved, inventory_not_eligibility_ready added)"
    else
      fail "10: eligibilityReady=false did not deterministically downgrade every eligible candidate as expected"
    fi
  else
    fail "could not extract or syntax-validate enforce_eligibility_readiness() from ${INVENTORY_SCRIPT}"
  fi
else
  skip "eligibilityReady enforcement test -- ${INVENTORY_SCRIPT} or python3 not available"
fi

# 16: no docs directory or runbook was added by this phase.
NEW_DOC_FILES="$(git -C "$REPO_ROOT" status --porcelain=v1 2>/dev/null | grep -E '^\?\? .*\.(md|MD)$' || true)"
if [ -z "$NEW_DOC_FILES" ] && [ ! -d "docs" ]; then
  pass "16: no docs directory or runbook (.md file) was added"
else
  fail "16: unexpected new documentation file(s)/directory found:"$'\n'"${NEW_DOC_FILES}"
fi

# 17/18: collector.py, monitor.py, and IAM remain unchanged.
if command -v git >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  COLLECTOR_MONITOR_DIFF="$(git -C "$REPO_ROOT" diff --stat -- monitoring/monitor/collector.py monitoring/monitor/monitor.py 2>/dev/null || true)"
  if [ -z "$COLLECTOR_MONITOR_DIFF" ]; then
    pass "17: collector.py and monitor.py are unchanged"
  else
    fail "17: collector.py and/or monitor.py were unexpectedly modified"
  fi

  # 18: IAM is unchanged, except for the specific, already-reviewed
  # additions from prior phases, Phase 6A, and Phase 6B2A:
  #   - Phase 5B2B1 dual-account correction: exactly one statement each of
  #     dynamodb:Query (scoped to gg-eks-pipeline) and elasticfilesystem:
  #     Describe* added to the GoldenGateEKSDeployRole-dev policy.
  #   - Phase 6A: envs/dev/iam.tf gains the new, dedicated
  #     goldengate_platform_logging_role_dev module block (a NEW IAM role,
  #     never a change to an existing one).
  #   - Phase 6B2A: envs/dev/iam.tf gains the new, dedicated
  #     goldengate_cloudwatch_metrics_role_dev module block (another NEW IAM
  #     role, never a change to an existing one), and
  #     envs/dev/policies/argocd-ecr-oci-read-dev/policies/policies_1.json
  #     gains exactly one new statement (the amazon-cloudwatch-observability
  #     Helm OCI chart repository ARN) alongside its unchanged pre-existing
  #     statements.
  # GoldenGateSecretsReadRole-dev and GoldenGateMonitorReadRole-dev must
  # never be touched by any of these phases. New files under a brand-new
  # policy folder (e.g. goldengate-platform-logging-dev/,
  # goldengate-cloudwatch-metrics-dev/) are untracked, not a "diff" of an
  # existing file, so they never appear here -- that is exactly the
  # expected shape of adding a new role.
  # --name-only does not fully honor --ignore-all-space in this git version
  # (it still lists files whose only diff is line-ending noise), so the
  # --stat form (which does honor it, confirmed empty for whitespace-only
  # files) is used and parsed for real changed paths instead.
  EXPECTED_MODIFIED_IAM_FILES="envs/dev/policies/goldengate-eks-deploy-dev/policies/policies_1.json
envs/dev/iam.tf
envs/dev/policies/argocd-ecr-oci-read-dev/policies/policies_1.json"
  IAM_DIFF_STAT="$(git -C "$REPO_ROOT" diff --stat=300 --ignore-all-space -- envs/dev/policies envs/dev/iam.tf 2>/dev/null || true)"
  IAM_DIFF_FILES="$(echo "$IAM_DIFF_STAT" | grep -oE '\S+\.(json|tf)' | sort -u || true)"
  UNEXPECTED_IAM_DIFF_FILES="$(comm -23 <(echo "$IAM_DIFF_FILES") <(echo "$EXPECTED_MODIFIED_IAM_FILES" | sort -u) 2>/dev/null || true)"
  SECRETS_MONITOR_DIFF="$(git -C "$REPO_ROOT" diff --stat --ignore-all-space -- envs/dev/policies/goldengate-secrets-read-dev envs/dev/policies/goldengate-monitor-read-dev 2>/dev/null || true)"
  if [ -z "$IAM_DIFF_FILES" ]; then
    pass "18: IAM (envs/dev/policies, envs/dev/iam.tf) is unchanged"
  elif [ -z "$UNEXPECTED_IAM_DIFF_FILES" ] && [ -z "$SECRETS_MONITOR_DIFF" ]; then
    pass "18: only the expected files changed (${IAM_DIFF_FILES//$'\n'/, }); GoldenGateSecretsReadRole-dev and GoldenGateMonitorReadRole-dev are unchanged"
  else
    fail "18: IAM changed outside the expected file set, or a protected role's policy was touched:"$'\n'"unexpected changed files: ${UNEXPECTED_IAM_DIFF_FILES}"$'\n'"${SECRETS_MONITOR_DIFF}"
  fi
else
  skip "collector.py/monitor.py/IAM unchanged checks -- not a git repository"
fi

echo ""
echo "=================================================="
echo "Summary: ${PASS_COUNT} passed, ${FAIL_COUNT} failed, ${SKIP_COUNT} skipped"
echo "=================================================="

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
