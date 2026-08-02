#!/usr/bin/env bash
# Detects which GoldenGate deployment folders under envs/dev/ should be
# built/updated (deployment_matrix) and which should be deleted
# (deletion_matrix) for this push/workflow_dispatch run. This is the real,
# single production implementation -- .github/workflows/goldengate-eks-app.yaml
# only wraps this script (env: mapping + `bash hack/detect-goldengate-deployments.sh`),
# it never re-implements any of this logic inline.
#
# Required environment (all opaque string data -- never treated as shell
# source, only ever read via "$VAR"):
#   INPUT_ENVIRONMENT   -- workflow_dispatch input: target environment
#   INPUT_DEPLOYMENT_ID -- workflow_dispatch input: deployment_id
#   INPUT_DEPLOY        -- workflow_dispatch input: deploy (true/false)
#   EVENT_NAME          -- github.event_name (e.g. "push", "workflow_dispatch")
#   BEFORE_SHA          -- github.event.before (push trigger only; may be empty)
#   AFTER_SHA           -- github.sha
#   GITHUB_OUTPUT       -- path to the GitHub Actions step output file
#
# Writes exactly four step outputs to GITHUB_OUTPUT, format/field names
# unchanged from the original inline implementation:
#   has_changes, deployment_matrix, has_deletions, deletion_matrix
set -euo pipefail

# Returns 0 (active) or 1 (inactive) on stdout as a one-line
# reason, for a given envs/dev/<id>/values.yaml path. A deployment
# is inactive if: the file is missing, empty, comment-only, parses
# to null/empty YAML, or explicitly disables itself via
# enabled: false, deployment.enabled: false, or
# lifecycle.state: absent. Prefers PyYAML (safe, full parse) and
# falls back to conservative text patterns only if PyYAML isn't
# installed on the runner -- never installs anything.
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
    # Conservative fallback without PyYAML: only recognizes the three
    # documented disable-flag shapes, not arbitrary YAML nesting.
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

# Strict, fail-closed YAML classifier shared by every GoldenGate-
# deployment classification call site in this workflow. No regex/
# text fallback: this repository's runner is required (see
# "Verify Python and PyYAML prerequisites" in the workflow) to have
# python3 + PyYAML, so a values file only ever qualifies after actual YAML
# parsing. $1 is a path to a file already known to exist and be
# non-empty (the two callers below each check that themselves,
# since a working-tree file and a git-show'd temp file have
# different "missing"/"empty" semantics). $2 is a comma-separated
# list of the deploymentModel values this call site accepts --
# there is no built-in default set here, and a value outside that
# list is never silently accepted no matter which caller invokes
# this.
#
# Two explicit, distinct contracts share this one parser:
#   ACTIVE CONTRACT ($2="singleRuntime", via
#   is_goldengate_deployment_values_file below): used by
#   workflow_dispatch validation, the active push build/update
#   matrix, and the build/deploy job. legacyPair, missing, and
#   unknown models all fail closed here -- excluded from every
#   active path, never inferred or defaulted to anything.
#   HISTORICAL DELETION CONTRACT ($2="singleRuntime,legacyPair",
#   via is_goldengate_deployment_values_file_at_ref below): used
#   only to classify a deployment's content as it existed at a
#   *previous* Git revision, for deletion purposes -- a removed/
#   renamed file, or the base-revision fallback for a current file
#   that was deliberately emptied. legacyPair still classifies
#   correctly here because a historical legacyPair deployment must
#   remain deletable even though the model is no longer
#   deployable. An unrecognized value still fails closed and is
#   never defaulted under this contract either.
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

# ACTIVE CONTRACT. Returns 0 (is an actively deployable GoldenGate
# runtime deployment values file) or 1 (is not) on stdout as a
# one-line reason, for a given envs/dev/<id>/values.yaml path in
# the current working tree. Classification is based solely on
# content -- never on directory name, and never on
# deployment.enabled/lifecycle.state (that is a separate,
# orthogonal active/inactive concern handled by
# is_active_deployment_values_file above). A file only qualifies
# when it is a non-empty, valid YAML mapping whose top-level
# deploymentModel is a string exactly equal to "singleRuntime" --
# legacyPair is a historical-only model and is rejected here just
# like any other unrecognized value, never accepted or defaulted.
# This is what keeps unrelated envs/dev/<x>/ values files (e.g.
# the separate goldengate-monitor chart's own values, or Argo
# CD's own values) -- and any legacyPair deployment someone might
# try to (re)introduce -- out of every active GoldenGate workflow
# path, without hardcoding folder names.
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

# HISTORICAL DELETION CONTRACT. Same shape of classification
# (0/1 plus a one-line reason on stdout), applied to a values
# file's content as it existed at a specific Git revision (never
# the working tree) -- used only for a removed/renamed deletion
# candidate, or the base-revision fallback for a current file that
# was deliberately emptied, whose current working-tree content no
# longer reflects what needs classifying. Accepts singleRuntime
# *and* legacyPair, since a historical legacyPair deployment must
# still be classifiable for deletion even though legacyPair is no
# longer an active, deployable model. Fails closed (returns 1,
# never defaults to any deploymentModel) when the path did not
# exist at that revision, was empty, or fails the same strict
# YAML/mapping/deploymentModel checks as the working-tree version.
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

# Inert assignment only -- $EVENT_NAME/$INPUT_* arrive as opaque string data
# through the workflow step's env: mapping (never interpolated GitHub
# expression syntax), and are only ever read here via "$VAR".
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

  # Fail closed: a manually requested deployment_id must resolve to
  # an actual GoldenGate runtime deployment values file (never a
  # different chart's values under envs/dev/, e.g. the shared
  # monitor's or Argo CD's own values file).
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

  # GG_REASON is exactly "deploymentModel=singleRuntime" on success
  # under the active contract (the only value it ever accepts) --
  # carried into the matrix below so the build job never has to
  # re-infer or default deploymentModel itself.
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

# BEFORE_SHA/AFTER_SHA arrive as opaque string data via the workflow step's
# env: mapping (EVENT_NAME/BEFORE_SHA/AFTER_SHA), already set in the
# environment -- read here via "$VAR" only, never re-interpolated.
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

# envs/dev/argocd/ holds Argo CD's own values file, deployed by the
# separate argocd-eks-deployment.yaml workflow. It is never a
# GoldenGate deployment and must be excluded here.
#
# Selection reason: a shared Helm chart change affects every active
# deployment, so it selects all active deployments. A deployment-
# values-only change only affects its own folder.
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

  # Applies to both shared-chart-change candidates (every
  # envs/dev/<id>/values.yaml) and per-folder push candidates:
  # only a values file whose own deploymentModel is singleRuntime
  # (the active contract -- legacyPair fails closed here just like
  # any other unrecognized value) is ever eligible for the active
  # build/update matrix -- never inferred from folder name, so a
  # different chart's values file under envs/dev/ (e.g. the shared
  # monitor's or Argo CD's own), or a legacyPair deployment, is
  # excluded here regardless of trigger reason.
  set +e
  GG_REASON="$(is_goldengate_deployment_values_file "$VALUES_FILE")"
  GG_STATUS=$?
  set -e

  if [ "$GG_STATUS" -ne 0 ]; then
    echo "Not an actively deployable GoldenGate deployment: ${DEPLOYMENT_ID} (${GG_REASON}) -- excluded from the build/update matrix."
    continue
  fi

  # GG_REASON is exactly "deploymentModel=singleRuntime" on success
  # under the active contract -- carried into the matrix entry
  # below so the build job never has to re-infer or default
  # deploymentModel itself.
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

# A path is a deletion candidate when it was removed (D) or is the
# old side of a rename (R). Only envs/dev/<id>/ paths matter here;
# helm/goldengate/** changes never produce a deployment deletion.
REMOVED_PATH_IDS="$(echo "$NAME_STATUS" \
  | awk '$1 ~ /^D/ { print $2 } $1 ~ /^R/ { print $2 }' \
  | grep -E '^envs/dev/[^/]+/' \
  | grep -v '^envs/dev/argocd/' \
  | sed -E 's#^envs/dev/([^/]+)/.*#\1#' \
  | sort -u || true)"

# Deployments whose values.yaml itself changed (added/modified) in
# this push must be re-checked too: the file can still exist but
# have just become comment-only/empty/disabled.
CHANGED_VALUES_IDS="$(echo "$CHANGED_FILES" \
  | grep -E '^envs/dev/[^/]+/values\.yaml$' \
  | grep -v '^envs/dev/argocd/' \
  | sed -E 's#^envs/dev/([^/]+)/values\.yaml$#\1#' \
  | sort -u || true)"

DELETION_CANDIDATE_IDS="$(printf '%s\n%s\n' "$REMOVED_PATH_IDS" "$CHANGED_VALUES_IDS" | sed '/^$/d' | sort -u || true)"

for CANDIDATE_ID in $DELETION_CANDIDATE_IDS; do
  VALUES_FILE="envs/dev/${CANDIDATE_ID}/values.yaml"

  # Classify once, from whichever source actually has content: the
  # working tree when the file still exists (was modified, not
  # removed/renamed), otherwise its content at the base revision
  # (BEFORE_SHA) -- the last point a removed/renamed file's
  # content can still be read. Fails closed either way: a
  # candidate that does not resolve to a valid GoldenGate
  # deploymentModel (singleRuntime/legacyPair) from EITHER source
  # is never added to the deletion matrix, and is never assumed to
  # be legacyPair (or any other value) by default. This is what
  # keeps a removed/renamed different-chart values file (e.g. the
  # separate goldengate-monitor chart's own, or Argo CD's own)
  # from ever entering the GoldenGate deletion matrix.
  if [ -f "$VALUES_FILE" ]; then
    GG_SOURCE="working tree"
    set +e
    GG_REASON="$(is_goldengate_deployment_values_file "$VALUES_FILE")"
    GG_STATUS=$?
    set -e

    if [ "$GG_STATUS" -ne 0 ]; then
      case "$GG_REASON" in
        "empty values.yaml"|"empty/comment-only values.yaml"|"empty/null parsed YAML")
          # The file still exists but was deliberately emptied
          # (zero-byte, whitespace/comment-only, or YAML null) --
          # this is a recognized intentional-deletion shape, not a
          # classification failure. Fall back to the previous
          # valid content at the base revision to determine
          # whether it used to be a genuine GoldenGate deployment
          # and, if so, which deploymentModel it was.
          echo "${VALUES_FILE} is now deliberately empty (${GG_REASON}). Checking its previous content at base revision (${BEFORE_SHA})..."
          GG_SOURCE="base revision (${BEFORE_SHA}), current file deliberately emptied"
          set +e
          GG_REASON="$(is_goldengate_deployment_values_file_at_ref "$BEFORE_SHA" "$VALUES_FILE")"
          GG_STATUS=$?
          set -e
          ;;
        "unparsable YAML:"*|"parsed YAML is not a mapping")
          # Malformed or structurally invalid current YAML is
          # never treated as an intentional deletion signal, and
          # never silently ignored either -- fail the workflow
          # closed with a clear error so the mistake is visible.
          echo "FAIL: ${VALUES_FILE} could not be classified: ${GG_REASON}"
          echo "This is not a recognized intentional-deletion shape (missing, zero-byte, whitespace/comment-only, or YAML null) -- it looks like invalid content instead. Fix or intentionally empty the file, or remove it entirely, before this push can be processed."
          exit 1
          ;;
        *)
          # A syntactically valid mapping that simply isn't a
          # GoldenGate deployment (no/unknown deploymentModel) --
          # not a deletion signal, not an error either.
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

  # GG_REASON is exactly "deploymentModel=<singleRuntime|legacyPair>"
  # on success (see _classify_deployment_model_yaml) -- the
  # deployment_model placed in the deletion matrix below comes
  # directly from this same successfully parsed and classified
  # document, never re-derived separately.
  CANDIDATE_DEPLOYMENT_MODEL="${GG_REASON#deploymentModel=}"

  set +e
  REASON="$(is_active_deployment_values_file "$VALUES_FILE")"
  STATUS=$?
  set -e

  if [ "$STATUS" -ne 0 ]; then
    # deployment.enabled=false is a "retired but retained" signal
    # only: the values folder is kept for rollback/reference and
    # must never drive live Argo CD Application or namespace
    # deletion. Only a genuinely removed file (missing/deleted) or
    # an explicit lifecycle.state=absent is treated as a real
    # deletion request. enabled=false (top-level) is treated the
    # same as deployment.enabled=false here for consistency.
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

# Deletion wins: drop any ID from the deployment matrix that also
# ended up in the deletion matrix (e.g. a deployment that changed
# to inactive content in the same push helm/goldengate/** changed).
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
