#!/usr/bin/env bash
# Builds deployment_matrix/deletion_matrix step outputs for envs/dev/ GoldenGate deployments; the single implementation wrapped by .github/workflows/00-main-goldengate-orchestrator.yaml.
set -euo pipefail

# Returns 0/1 (active/inactive) with a one-line reason on stdout; inactive if missing/empty/comment-only/null YAML, or enabled:false/deployment.enabled:false; prefers PyYAML, falls back to text patterns. GoldenGate Runtime Desired-State Simplification: deployment.enabled is the sole runtime-presence control -- lifecycle.state is retired and no longer checked here at all; a descriptor that still carries a stale lifecycle block is not rejected by this cheap git-diff triage heuristic (it never does full descriptor validation for any field), but is fail-closed rejected downstream by hack/goldengate-deployment-model.py's own strict parser the first time this candidate is actually described/built/deployed -- the ONE authoritative source of truth for that rejection, never duplicated here.
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

# EFS-MODE EXTRACTION (deletion-guard support): prints persistence.efs.mode from $1's content (a file already known to exist/be non-empty), or the empty string (never inferred/defaulted) when persistence/efs is not declared, not enabled, not provider=efs, unparsable, or not a mapping; never fails the caller since the deletion-safety gate must fail closed on its own comparison, not on a Python exception here.
_efs_mode_from_yaml() {
  local content_file="$1"

  python3 - "$content_file" <<'PYEOF'
import sys

import yaml

path = sys.argv[1]
with open(path, "r") as f:
    raw = f.read()

try:
    data = yaml.safe_load(raw)
except yaml.YAMLError:
    print("")
    sys.exit(0)

if not isinstance(data, dict):
    print("")
    sys.exit(0)

persistence = data.get("persistence")
if not isinstance(persistence, dict):
    print("")
    sys.exit(0)

if persistence.get("enabled") is not True or persistence.get("provider") != "efs":
    print("")
    sys.exit(0)

efs = persistence.get("efs")
mode = efs.get("mode") if isinstance(efs, dict) else None
print(mode if isinstance(mode, str) else "")
PYEOF
}

# STORAGE-TRANSITION-GUARD support: prints a compact one-line JSON summary of $1's persistence.efs identity (mode/provider/fileSystemId, each string-or-null, plus enabled as a real JSON boolean) so the caller can diff historical vs current state without re-deriving the efs_enabled gate twice; unparsable/malformed content degrades to an all-null/false summary rather than failing the caller, since the transition guard itself decides what is safe, not this extraction step.
_persistence_efs_summary_json() {
  local content_file="$1"

  python3 - "$content_file" <<'PYEOF'
import json
import sys

import yaml

path = sys.argv[1]
with open(path, "r") as f:
    raw = f.read()

summary = {"enabled": False, "provider": None, "mode": None, "fileSystemId": None}

try:
    data = yaml.safe_load(raw)
except yaml.YAMLError:
    print(json.dumps(summary))
    sys.exit(0)

if not isinstance(data, dict):
    print(json.dumps(summary))
    sys.exit(0)

persistence = data.get("persistence")
if not isinstance(persistence, dict):
    print(json.dumps(summary))
    sys.exit(0)

summary["enabled"] = persistence.get("enabled") is True
summary["provider"] = persistence.get("provider") if isinstance(persistence.get("provider"), str) else None

if not summary["enabled"] or summary["provider"] != "efs":
    print(json.dumps(summary))
    sys.exit(0)

efs = persistence.get("efs")
if isinstance(efs, dict):
    summary["mode"] = efs.get("mode") if isinstance(efs.get("mode"), str) else None
    summary["fileSystemId"] = efs.get("fileSystemId") if isinstance(efs.get("fileSystemId"), str) else None

print(json.dumps(summary))
PYEOF
}

# STORAGE-TRANSITION-GUARD rules: given $1=historical and $2=current _persistence_efs_summary_json blobs for the SAME still-present descriptor, prints one non-empty violation reason if the transition is unsafe, otherwise prints nothing. Allowed: new deployment (no historical state, never called for that case -- see the caller), managed->managed, existing->existing with an unchanged fileSystemId, and any change unrelated to persistence.efs identity (e.g. deployment.enabled alone). Blocked: managed->existing, existing->managed, managed->persistence disabled, managed->non-EFS provider, existing fileSystemId mutation.
_check_storage_transition() {
  local historical_json="$1"
  local current_json="$2"

  python3 -c '
import json, sys

historical, current = json.loads(sys.argv[1]), json.loads(sys.argv[2])
h_mode, c_mode = historical.get("mode"), current.get("mode")

if h_mode == "managed":
    if not current.get("enabled"):
        print("managed -> persistence disabled")
    elif current.get("provider") != "efs":
        print("managed -> non-EFS provider")
    elif c_mode == "existing":
        print("managed -> existing")
elif h_mode == "existing":
    if c_mode == "managed":
        print("existing -> managed")
    elif c_mode == "existing" and current.get("fileSystemId") != historical.get("fileSystemId"):
        old_id, new_id = historical.get("fileSystemId"), current.get("fileSystemId")
        print(f"existing fileSystemId changed from {old_id!r} to {new_id!r}")
' "$historical_json" "$current_json"
}

# ACTIVE CONTRACT: qualifies only a non-empty, valid YAML mapping whose deploymentModel is exactly "singleRuntime" (legacyPair and unrecognized values fail closed); content-based only, independent of the enabled check above.
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

  # Live Deploy UX Fix 2: an explicit environment-wide manual run -- deployment_id left blank on purpose (never an invented/default runtime ID, never auto-selecting either repltest descriptor). No descriptor validation is attempted. This does NOT mean "there are no active runtimes globally" -- validate_model's own active_runtime_matrix remains the independent, canonical GLOBAL runtime registry; this only means no individual GoldenGate runtime was selected for build/reconciliation in THIS manual invocation.
  if [ -z "$DEPLOYMENT_ID" ]; then
    if [ "$DEPLOY" = "true" ]; then
      ACTION_LABEL="deploy"
    else
      ACTION_LABEL="validate"
    fi
    echo "Manual environment-wide ${ACTION_LABEL} requested for environment=${ENVIRONMENT}; no individual GoldenGate runtime was selected for build/reconciliation."

    echo "has_changes=false" >> "$GITHUB_OUTPUT"
    echo "deployment_matrix=[]" >> "$GITHUB_OUTPUT"
    echo "has_deletions=false" >> "$GITHUB_OUTPUT"
    echo "deletion_matrix=[]" >> "$GITHUB_OUTPUT"
    echo "has_storage_transition_violations=false" >> "$GITHUB_OUTPUT"
    echo "storage_transition_violations=[]" >> "$GITHUB_OUTPUT"
    exit 0
  fi

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
  # A manual redeploy has no push-diff base to compare against, so the storage-transition guard (which needs BEFORE_SHA content) does not run here; the push path already blocks an unsafe transition before it can be merged.
  echo "has_storage_transition_violations=false" >> "$GITHUB_OUTPUT"
  echo "storage_transition_violations=[]" >> "$GITHUB_OUTPUT"

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
    # GoldenGate Runtime Desired-State Simplification: deployment.enabled=false (or top-level enabled=false) is now a first-class desired-ABSENCE request -- it drives the SAME ownership-safe removal/pruning path (delete_removed_argocd_applications) that a physically-removed descriptor drives, decommissioning the RUNTIME APPLICATION/workload only, while the descriptor (and any managed durable storage it names) is retained. It must never be conflated with physical removal of the descriptor itself, which is the only shape that can make Terraform observe a vanished module instance -- that stays a completely separate reason.
    case "$REASON" in
      deployment.enabled=false*|enabled=false*)
        echo "Deployment disabled (application decommission, descriptor and storage retained): ${CANDIDATE_ID} (${REASON})"
        INACTIVE_LOG="${INACTIVE_LOG}  - ${CANDIDATE_ID} (${REASON}) [deployment-disabled -- application removed, managed storage retained]\n"
        DELETION_REASON="deployment-disabled"
        ;;
      *)
        echo "Inactive/deleted (physical removal): ${CANDIDATE_ID} (${REASON})"
        INACTIVE_LOG="${INACTIVE_LOG}  - ${CANDIDATE_ID} (${REASON})\n"
        DELETION_REASON="physical-removal"
        ;;
    esac

    if [ -n "$DELETION_REASON" ]; then
      echo "  deploymentModel (${GG_SOURCE}): ${CANDIDATE_DEPLOYMENT_MODEL}"

      # Resolve historical persistence.efs.mode from whichever source classified this candidate (working tree if the file still exists there, e.g. deployment.enabled=false, otherwise its content at BEFORE_SHA) for managed_efs_deletion_guard; never inferred, empty string means EFS/managed was never declared there.
      if [ -f "$VALUES_FILE" ] && [ -s "$VALUES_FILE" ]; then
        CANDIDATE_EFS_MODE="$(_efs_mode_from_yaml "$VALUES_FILE")"
      else
        EFS_TMP_FILE="$(mktemp)"
        if git show "${BEFORE_SHA}:${VALUES_FILE}" > "$EFS_TMP_FILE" 2>/dev/null && [ -s "$EFS_TMP_FILE" ]; then
          CANDIDATE_EFS_MODE="$(_efs_mode_from_yaml "$EFS_TMP_FILE")"
        else
          CANDIDATE_EFS_MODE=""
        fi
        rm -f "$EFS_TMP_FILE"
      fi
      echo "  persistence.efs.mode (${GG_SOURCE}): ${CANDIDATE_EFS_MODE:-<not declared>}"
      echo "  reason: ${DELETION_REASON}"

      DELETION_MATRIX_ITEMS="$(echo "$DELETION_MATRIX_ITEMS" | jq -c \
        --arg deployment_id "$CANDIDATE_ID" \
        --arg deployment_model "$CANDIDATE_DEPLOYMENT_MODEL" \
        --arg efs_mode "$CANDIDATE_EFS_MODE" \
        --arg reason "$DELETION_REASON" \
        '. + [{environment: "dev", deployment_id: $deployment_id, deployment_model: $deployment_model, efs_mode: $efs_mode, reason: $reason}]')"
    fi
  else
    echo "Still active: ${CANDIDATE_ID} (${REASON}) -- not a deletion."
  fi
done

echo ""
echo "Checking changed-and-still-present descriptors for unsafe storage-identity transitions..."

TRANSITION_VIOLATIONS="[]"

for CHANGED_ID in $CHANGED_VALUES_IDS; do
  CHANGED_VALUES_FILE="envs/dev/${CHANGED_ID}/values.yaml"

  # Only a still-present descriptor can undergo a "transition" -- a removed/emptied file is a deletion, already handled by the deletion matrix, not a storage-identity mutation of a still-existing runtime.
  if [ ! -f "$CHANGED_VALUES_FILE" ] || [ ! -s "$CHANGED_VALUES_FILE" ]; then
    continue
  fi

  set +e
  CT_GG_REASON="$(is_goldengate_deployment_values_file "$CHANGED_VALUES_FILE")"
  CT_GG_STATUS=$?
  set -e
  if [ "$CT_GG_STATUS" -ne 0 ]; then
    continue
  fi

  # No historical content at BEFORE_SHA means this is a brand-new deployment folder -- any starting persistence.efs.mode is allowed (new managed, new existing).
  CT_HIST_TMP="$(mktemp)"
  if ! git show "${BEFORE_SHA}:${CHANGED_VALUES_FILE}" > "$CT_HIST_TMP" 2>/dev/null || [ ! -s "$CT_HIST_TMP" ]; then
    rm -f "$CT_HIST_TMP"
    continue
  fi

  CT_HISTORICAL_JSON="$(_persistence_efs_summary_json "$CT_HIST_TMP")"
  rm -f "$CT_HIST_TMP"
  CT_CURRENT_JSON="$(_persistence_efs_summary_json "$CHANGED_VALUES_FILE")"

  CT_VIOLATION="$(_check_storage_transition "$CT_HISTORICAL_JSON" "$CT_CURRENT_JSON")"
  if [ -n "$CT_VIOLATION" ]; then
    echo "STORAGE TRANSITION VIOLATION: ${CHANGED_ID}: ${CT_VIOLATION}"
    TRANSITION_VIOLATIONS="$(echo "$TRANSITION_VIOLATIONS" | jq -c \
      --arg deployment_id "$CHANGED_ID" \
      --arg violation "$CT_VIOLATION" \
      '. + [{environment: "dev", deployment_id: $deployment_id, violation: $violation}]')"
  fi
done

TRANSITION_VIOLATION_COUNT="$(echo "$TRANSITION_VIOLATIONS" | jq 'length')"
if [ "$TRANSITION_VIOLATION_COUNT" -eq 0 ]; then
  echo "No unsafe storage-identity transitions detected."
  echo "has_storage_transition_violations=false" >> "$GITHUB_OUTPUT"
  echo "storage_transition_violations=[]" >> "$GITHUB_OUTPUT"
else
  echo "has_storage_transition_violations=true" >> "$GITHUB_OUTPUT"
  echo "storage_transition_violations=${TRANSITION_VIOLATIONS}" >> "$GITHUB_OUTPUT"
fi

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
echo "Storage transition violations: ${TRANSITION_VIOLATIONS}"
