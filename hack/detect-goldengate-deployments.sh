#!/usr/bin/env bash
# Builds deployment_matrix/deletion_matrix step outputs for envs/dev/ GoldenGate deployments; the single implementation wrapped by .github/workflows/goldengate-eks-app.yaml.
set -euo pipefail

# Returns 0/1 (active/inactive) with a one-line reason on stdout; inactive if missing/empty/comment-only/null YAML, or enabled:false/deployment.enabled:false/lifecycle.state:absent; prefers PyYAML, falls back to text patterns.
is_active_deployment_values_file() {
  local values_file="$1"

  if [ ! -f "$values_file" ]; then
    echo "missing values.yaml"
    return 1
  fi

  if [ ! -s "$values_file" ]; then
    echo "empty values.yaml"
    return 1
  fi

  if command -v python3 >/dev/null 2>&1; then
    python3 - "$values_file" <<'PYEOF'
import re
import sys

path = sys.argv[1]

with open(path, "r") as f:
    raw = f.read()

non_comment_lines = [
    line for line in raw.splitlines()
    if line.strip() and not line.strip().startswith("#")
]
if not non_comment_lines:
    print("empty/comment-only values.yaml")
    sys.exit(1)

try:
    import yaml
except ImportError:
    # Fallback without PyYAML: only recognizes the three documented disable-flag shapes, not arbitrary YAML nesting.
    if re.search(r'(?m)^\s*enabled\s*:\s*false\s*$', raw):
        print("enabled=false (text fallback, PyYAML unavailable)")
        sys.exit(1)
    if re.search(r'(?ms)^deployment\s*:\s*\n(?:[ \t]+\S.*\n?)*?[ \t]+enabled\s*:\s*false\s*$', raw):
        print("deployment.enabled=false (text fallback, PyYAML unavailable)")
        sys.exit(1)
    if re.search(r'(?ms)^lifecycle\s*:\s*\n(?:[ \t]+\S.*\n?)*?[ \t]+state\s*:\s*["\']?absent["\']?\s*$', raw):
        print("lifecycle.state=absent (text fallback, PyYAML unavailable)")
        sys.exit(1)
    print("active (text fallback, PyYAML unavailable)")
    sys.exit(0)

try:
    data = yaml.safe_load(raw)
except yaml.YAMLError as exc:
    print(f"unparsable YAML: {exc}")
    sys.exit(1)

if not data:
    print("empty/null parsed YAML")
    sys.exit(1)

if not isinstance(data, dict):
    print("parsed YAML is not a mapping")
    sys.exit(1)

if data.get("enabled") is False:
    print("enabled=false")
    sys.exit(1)

deployment = data.get("deployment")
if isinstance(deployment, dict) and deployment.get("enabled") is False:
    print("deployment.enabled=false")
    sys.exit(1)

lifecycle = data.get("lifecycle")
if isinstance(lifecycle, dict) and lifecycle.get("state") == "absent":
    print("lifecycle.state=absent")
    sys.exit(1)

print("active")
sys.exit(0)
PYEOF
    return $?
  fi

  echo "python3 not available on this runner, using conservative bash fallback"

  local non_comment
  non_comment="$(grep -vE '^[[:space:]]*(#.*)?$' "$values_file" || true)"
  if [ -z "$non_comment" ]; then
    echo "empty/comment-only values.yaml (bash fallback)"
    return 1
  fi

  if grep -Eq '^enabled[[:space:]]*:[[:space:]]*false[[:space:]]*$' "$values_file"; then
    echo "enabled=false (bash fallback)"
    return 1
  fi

  if awk '
    /^deployment:[[:space:]]*$/ { in_block=1; next }
    in_block && /^[[:space:]]+enabled:[[:space:]]*false[[:space:]]*$/ { found=1; exit }
    in_block && /^[^[:space:]]/ { in_block=0 }
    END { exit !found }
  ' "$values_file"; then
    echo "deployment.enabled=false (bash fallback)"
    return 1
  fi

  if awk '
    /^lifecycle:[[:space:]]*$/ { in_block=1; next }
    in_block && /^[[:space:]]+state:[[:space:]]*"?absent"?[[:space:]]*$/ { found=1; exit }
    in_block && /^[^[:space:]]/ { in_block=0 }
    END { exit !found }
  ' "$values_file"; then
    echo "lifecycle.state=absent (bash fallback)"
    return 1
  fi

  echo "active (bash fallback, python3 unavailable)"
  return 0
}

# Strict, fail-closed YAML classifier (no regex/text fallback; requires PyYAML) shared by both contracts below: $1 is a values file already known to exist/be non-empty, $2 is the comma-separated list of deploymentModel values this call site accepts.
_classify_deployment_model_yaml() {
  local content_file="$1"
  local allowed_models_csv="$2"

  python3 - "$content_file" "$allowed_models_csv" <<'PYEOF'
import sys

import yaml

path, allowed_models_csv = sys.argv[1:3]
ALLOWED_MODELS = tuple(m for m in allowed_models_csv.split(",") if m)

with open(path, "r") as f:
    raw = f.read()

non_comment_lines = [
    line for line in raw.splitlines()
    if line.strip() and not line.strip().startswith("#")
]
if not non_comment_lines:
    print("empty/comment-only values.yaml")
    sys.exit(1)

try:
    data = yaml.safe_load(raw)
except yaml.YAMLError as exc:
    print(f"unparsable YAML: {exc}")
    sys.exit(1)

if not data:
    print("empty/null parsed YAML")
    sys.exit(1)

if not isinstance(data, dict):
    print("parsed YAML is not a mapping")
    sys.exit(1)

model = data.get("deploymentModel")

if isinstance(model, str) and model in ALLOWED_MODELS:
    print(f"deploymentModel={model}")
    sys.exit(0)

print(f"not a GoldenGate deployment values file: deploymentModel={model!r}")
sys.exit(1)
PYEOF
  return $?
}

# ACTIVE CONTRACT: qualifies only a non-empty, valid YAML mapping whose deploymentModel is exactly "singleRuntime" (legacyPair and unrecognized values fail closed); content-based only, independent of the enabled/lifecycle check above.
is_goldengate_deployment_values_file() {
  local values_file="$1"

  if [ ! -f "$values_file" ]; then
    echo "missing values.yaml"
    return 1
  fi

  if [ ! -s "$values_file" ]; then
    echo "empty values.yaml"
    return 1
  fi

  _classify_deployment_model_yaml "$values_file" "singleRuntime"
  return $?
}

# HISTORICAL DELETION CONTRACT: classifies content at a specific Git revision (never the working tree), for a removed/renamed candidate or an emptied current file; accepts singleRuntime and legacyPair since a historical legacyPair must remain deletable.
is_goldengate_deployment_values_file_at_ref() {
  local ref="$1"
  local path="$2"
  local tmp_file
  tmp_file="$(mktemp)"

  if ! git show "${ref}:${path}" > "$tmp_file" 2>/dev/null; then
    echo "missing at ${ref}:${path}"
    rm -f "$tmp_file"
    return 1
  fi

  if [ ! -s "$tmp_file" ]; then
    echo "empty at ${ref}:${path}"
    rm -f "$tmp_file"
    return 1
  fi

  local reason status
  reason="$(_classify_deployment_model_yaml "$tmp_file" "singleRuntime,legacyPair")"
  status=$?
  rm -f "$tmp_file"
  echo "$reason"
  return $status
}

# $EVENT_NAME/$INPUT_* arrive as opaque data via the workflow step's env: mapping (never interpolated GitHub expression syntax).
if [ "$EVENT_NAME" = "workflow_dispatch" ]; then
  ENVIRONMENT="$INPUT_ENVIRONMENT"
  DEPLOYMENT_ID="$INPUT_DEPLOYMENT_ID"
  DEPLOY="$INPUT_DEPLOY"

  if [[ ! "$DEPLOYMENT_ID" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
    echo "Invalid deployment_id: $DEPLOYMENT_ID"
    echo "Use lowercase letters, numbers, and hyphens only."
    echo "Example: gg-oracle-payments-01"
    exit 1
  fi

  VALUES_FILE="envs/${ENVIRONMENT}/${DEPLOYMENT_ID}/values.yaml"

  echo "Manual workflow_dispatch trigger for deployment_id=${DEPLOYMENT_ID}, environment=${ENVIRONMENT}."
  echo "Validating deployment values file: ${VALUES_FILE}"

  # Fail closed: deployment_id must resolve to an actual GoldenGate values file, never a different chart's values (e.g. monitor/Argo CD).
  set +e
  GG_REASON="$(is_goldengate_deployment_values_file "$VALUES_FILE")"
  GG_STATUS=$?
  set -e

  if [ "$GG_STATUS" -ne 0 ]; then
    echo "Deployment values file check failed: ${GG_REASON}"
    echo ""
    echo "${VALUES_FILE} is not an actively deployable GoldenGate runtime deployment values file (deploymentModel must be exactly singleRuntime -- legacyPair is retired and is no longer deployable). Refusing to build a matrix for it."
    exit 1
  fi

  # GG_REASON is exactly deploymentModel=singleRuntime on success; carried into the matrix so the build job never re-infers it.
  ACTIVE_DEPLOYMENT_MODEL="${GG_REASON#deploymentModel=}"

  set +e
  REASON="$(is_active_deployment_values_file "$VALUES_FILE")"
  STATUS=$?
  set -e

  if [ "$STATUS" -ne 0 ]; then
    echo "Deployment values file check failed: ${REASON}"
    echo ""
    echo "Deployment values file is missing or inactive. Use Git deletion/disable flow for cleanup, or restore active values before manual deploy."
    exit 1
  fi

  echo "Deployment values file is active (${REASON}). Building single-item matrix."

  MATRIX_JSON="$(jq -nc \
    --arg environment "$ENVIRONMENT" \
    --arg deployment_id "$DEPLOYMENT_ID" \
    --arg deployment_model "$ACTIVE_DEPLOYMENT_MODEL" \
    --argjson deploy "$DEPLOY" \
    '[{environment: $environment, deployment_id: $deployment_id, deployment_model: $deployment_model, deploy: $deploy}]')"

  echo "has_changes=true" >> "$GITHUB_OUTPUT"
  echo "deployment_matrix=${MATRIX_JSON}" >> "$GITHUB_OUTPUT"
  echo "has_deletions=false" >> "$GITHUB_OUTPUT"
  echo "deletion_matrix=[]" >> "$GITHUB_OUTPUT"

  echo "Matrix: ${MATRIX_JSON}"
  exit 0
fi

echo "Push trigger. Detecting changed deployment folders under envs/dev/ and helm/goldengate/..."

# BEFORE_SHA/AFTER_SHA arrive as opaque data via the workflow step's env: mapping, read only via "$VAR".
EMPTY_TREE_SHA="4b825dc642cb6eb9a060e54bf8d69288fbee4904"

if [ -z "$BEFORE_SHA" ] || [ "$BEFORE_SHA" = "0000000000000000000000000000000000000000" ]; then
  echo "No usable previous commit (new branch or first push). Diffing against the empty tree."
  BEFORE_SHA="$EMPTY_TREE_SHA"
fi

CHANGED_FILES="$(git diff --name-only "$BEFORE_SHA" "$AFTER_SHA" -- 'envs/dev/**' 'helm/goldengate/**' || true)"

echo "Changed files under envs/dev/ and helm/goldengate/:"
echo "${CHANGED_FILES:-<none>}"

CHART_CHANGED="false"
if echo "$CHANGED_FILES" | grep -q '^helm/goldengate/'; then
  CHART_CHANGED="true"
fi

echo "Chart changed: ${CHART_CHANGED}"

DEPLOYMENT_MATRIX_ITEMS="[]"
DELETION_MATRIX_ITEMS="[]"
ACTIVE_LOG=""
INACTIVE_LOG=""

# Excludes envs/dev/argocd/ (Argo CD's own values, not a GoldenGate deployment); chart-wide changes select all active deployments, otherwise only changed folders.
if [ "$CHART_CHANGED" = "true" ]; then
  echo "Selection reason: shared GoldenGate Helm chart changed. Selecting all active dev deployments."

  DEPLOYMENT_CANDIDATE_IDS="$(find envs/dev -mindepth 2 -maxdepth 2 -name values.yaml -not -path 'envs/dev/argocd/*' \
    | sed -E 's#^envs/dev/([^/]+)/values\.yaml$#\1#' \
    | sort -u || true)"
else
  echo "Selection reason: only envs/dev/<deployment> changed. Selecting changed deployment folders."

  DEPLOYMENT_CANDIDATE_IDS="$(echo "$CHANGED_FILES" \
    | grep -E '^envs/dev/[^/]+/' \
    | grep -v '^envs/dev/argocd/' \
    | sed -E 's#^envs/dev/([^/]+)/.*#\1#' \
    | sort -u || true)"
fi

for DEPLOYMENT_ID in $DEPLOYMENT_CANDIDATE_IDS; do
  VALUES_FILE="envs/dev/${DEPLOYMENT_ID}/values.yaml"

  # Only a values file whose own deploymentModel is singleRuntime is ever eligible (never inferred from folder name).
  set +e
  GG_REASON="$(is_goldengate_deployment_values_file "$VALUES_FILE")"
  GG_STATUS=$?
  set -e

  if [ "$GG_STATUS" -ne 0 ]; then
    echo "Not an actively deployable GoldenGate deployment: ${DEPLOYMENT_ID} (${GG_REASON}) -- excluded from the build/update matrix."
    continue
  fi

  # GG_REASON is exactly deploymentModel=singleRuntime here too; carried into the matrix entry below.
  ACTIVE_DEPLOYMENT_MODEL="${GG_REASON#deploymentModel=}"

  set +e
  REASON="$(is_active_deployment_values_file "$VALUES_FILE")"
  STATUS=$?
  set -e

  if [ "$STATUS" -eq 0 ]; then
    echo "Active: ${DEPLOYMENT_ID} (${REASON})"
    ACTIVE_LOG="${ACTIVE_LOG}  - ${DEPLOYMENT_ID} (${REASON})\n"
    DEPLOYMENT_MATRIX_ITEMS="$(echo "$DEPLOYMENT_MATRIX_ITEMS" | jq -c \
      --arg deployment_id "$DEPLOYMENT_ID" \
      --arg deployment_model "$ACTIVE_DEPLOYMENT_MODEL" \
      '. + [{environment: "dev", deployment_id: $deployment_id, deployment_model: $deployment_model, deploy: true}]')"
  else
    echo "Not active: ${DEPLOYMENT_ID} (${REASON}) -- skipping build/update for this candidate."
  fi
done

echo ""
echo "Detecting deleted or newly-inactive deployment folders under envs/dev/..."

NAME_STATUS="$(git diff --name-status "$BEFORE_SHA" "$AFTER_SHA" -- 'envs/dev/**' 'helm/goldengate/**' || true)"

echo "Name-status diff under envs/dev/ and helm/goldengate/:"
echo "${NAME_STATUS:-<none>}"

# Deletion candidates are removed (D) or renamed-away (R) envs/dev/<id>/ paths; helm/goldengate/** changes never produce a deletion.
REMOVED_PATH_IDS="$(echo "$NAME_STATUS" \
  | awk '$1 ~ /^D/ { print $2 } $1 ~ /^R/ { print $2 }' \
  | grep -E '^envs/dev/[^/]+/' \
  | grep -v '^envs/dev/argocd/' \
  | sed -E 's#^envs/dev/([^/]+)/.*#\1#' \
  | sort -u || true)"

# Also re-check deployments whose values.yaml changed in this push -- it may have just become comment-only/empty/disabled.
CHANGED_VALUES_IDS="$(echo "$CHANGED_FILES" \
  | grep -E '^envs/dev/[^/]+/values\.yaml$' \
  | grep -v '^envs/dev/argocd/' \
  | sed -E 's#^envs/dev/([^/]+)/values\.yaml$#\1#' \
  | sort -u || true)"

DELETION_CANDIDATE_IDS="$(printf '%s\n%s\n' "$REMOVED_PATH_IDS" "$CHANGED_VALUES_IDS" | sed '/^$/d' | sort -u || true)"

for CANDIDATE_ID in $DELETION_CANDIDATE_IDS; do
  VALUES_FILE="envs/dev/${CANDIDATE_ID}/values.yaml"

  # Classify from the working tree if the file still exists, otherwise from its content at BEFORE_SHA; fails closed either way, never defaults to a model.
  if [ -f "$VALUES_FILE" ]; then
    GG_SOURCE="working tree"
    set +e
    GG_REASON="$(is_goldengate_deployment_values_file "$VALUES_FILE")"
    GG_STATUS=$?
    set -e

    if [ "$GG_STATUS" -ne 0 ]; then
      case "$GG_REASON" in
        "empty values.yaml"|"empty/comment-only values.yaml"|"empty/null parsed YAML")
          # Deliberately emptied file (zero-byte/comment-only/null YAML) is an intentional-deletion shape; fall back to its content at the base revision.
          echo "${VALUES_FILE} is now deliberately empty (${GG_REASON}). Checking its previous content at base revision (${BEFORE_SHA})..."
          GG_SOURCE="base revision (${BEFORE_SHA}), current file deliberately emptied"
          set +e
          GG_REASON="$(is_goldengate_deployment_values_file_at_ref "$BEFORE_SHA" "$VALUES_FILE")"
          GG_STATUS=$?
          set -e
          ;;
        "unparsable YAML:"*|"parsed YAML is not a mapping")
          # Malformed/invalid YAML is not an intentional-deletion signal -- fail the workflow closed instead of ignoring it.
          echo "FAIL: ${VALUES_FILE} could not be classified: ${GG_REASON}"
          echo "This is not a recognized intentional-deletion shape (missing, zero-byte, whitespace/comment-only, or YAML null) -- it looks like invalid content instead. Fix or intentionally empty the file, or remove it entirely, before this push can be processed."
          exit 1
          ;;
        *)
          # Valid mapping but not a GoldenGate deployment -- not a deletion signal, not an error.
          ;;
      esac
    fi
  else
    GG_SOURCE="base revision (${BEFORE_SHA})"
    set +e
    GG_REASON="$(is_goldengate_deployment_values_file_at_ref "$BEFORE_SHA" "$VALUES_FILE")"
    GG_STATUS=$?
    set -e
  fi

  if [ "$GG_STATUS" -ne 0 ]; then
    echo "Not a GoldenGate deployment: ${CANDIDATE_ID} (${GG_REASON}, from ${GG_SOURCE}) -- ignoring for deletion evaluation."
    continue
  fi

  # GG_REASON is deploymentModel=<singleRuntime|legacyPair> on success; the deletion matrix's deployment_model comes directly from it.
  CANDIDATE_DEPLOYMENT_MODEL="${GG_REASON#deploymentModel=}"

  set +e
  REASON="$(is_active_deployment_values_file "$VALUES_FILE")"
  STATUS=$?
  set -e

  if [ "$STATUS" -ne 0 ]; then
    # deployment.enabled=false (or top-level enabled=false) is retired-but-retained -- never drives deletion; only a removed file or lifecycle.state=absent does.
    case "$REASON" in
      deployment.enabled=false*|enabled=false*)
        echo "Inactive (retained, not deleted): ${CANDIDATE_ID} (${REASON})"
        INACTIVE_LOG="${INACTIVE_LOG}  - ${CANDIDATE_ID} (${REASON}) [retained -- no deletion request]\n"
        ;;
      *)
        echo "Inactive/deleted: ${CANDIDATE_ID} (${REASON})"
        INACTIVE_LOG="${INACTIVE_LOG}  - ${CANDIDATE_ID} (${REASON})\n"
        echo "  deploymentModel (${GG_SOURCE}): ${CANDIDATE_DEPLOYMENT_MODEL}"

        DELETION_MATRIX_ITEMS="$(echo "$DELETION_MATRIX_ITEMS" | jq -c \
          --arg deployment_id "$CANDIDATE_ID" \
          --arg deployment_model "$CANDIDATE_DEPLOYMENT_MODEL" \
          '. + [{environment: "dev", deployment_id: $deployment_id, deployment_model: $deployment_model}]')"
        ;;
    esac
  else
    echo "Still active: ${CANDIDATE_ID} (${REASON}) -- not a deletion."
  fi
done

# Deletion wins: drop any ID from the deployment matrix that also ended up in the deletion matrix.
DEPLOYMENT_MATRIX_ITEMS="$(echo "$DEPLOYMENT_MATRIX_ITEMS" | jq -c \
  --argjson deletions "$DELETION_MATRIX_ITEMS" \
  '[ .[] | select(.deployment_id as $id | ($deletions | map(.deployment_id) | index($id)) == null) ]')"

echo ""
echo "Active deployment IDs selected for build/update:"
echo -e "${ACTIVE_LOG:-  <none>}"

echo "Inactive/deleted deployment IDs selected for deletion:"
echo -e "${INACTIVE_LOG:-  <none>}"

MATRIX_COUNT="$(echo "$DEPLOYMENT_MATRIX_ITEMS" | jq 'length')"

if [ "$MATRIX_COUNT" -eq 0 ]; then
  echo "No active deployment folders selected for build/update."
  echo "has_changes=false" >> "$GITHUB_OUTPUT"
  echo "deployment_matrix=[]" >> "$GITHUB_OUTPUT"
else
  echo "has_changes=true" >> "$GITHUB_OUTPUT"
  echo "deployment_matrix=${DEPLOYMENT_MATRIX_ITEMS}" >> "$GITHUB_OUTPUT"
fi

DELETION_COUNT="$(echo "$DELETION_MATRIX_ITEMS" | jq 'length')"

if [ "$DELETION_COUNT" -eq 0 ]; then
  echo "No inactive/deleted deployment folders detected."
  echo "has_deletions=false" >> "$GITHUB_OUTPUT"
  echo "deletion_matrix=[]" >> "$GITHUB_OUTPUT"
else
  echo "has_deletions=true" >> "$GITHUB_OUTPUT"
  echo "deletion_matrix=${DELETION_MATRIX_ITEMS}" >> "$GITHUB_OUTPUT"
fi

echo "Deployment matrix: ${DEPLOYMENT_MATRIX_ITEMS}"
echo "Deletion matrix: ${DELETION_MATRIX_ITEMS}"
