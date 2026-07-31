# GoldenGate shared monitor

The one shared GoldenGate monitoring application: a passive collector
(GoldenGate Admin REST polling, LEASE ownership, STATE#_deployment /
STATE#<process> writes) plus a read-only operator portal, in a single
process. Never writes CONFIG, never Scans, never restarts/fences a
GoldenGate process, never calls the Kubernetes API.

## Modules

- `monitor.py` -- HTTP server, portal status assembly/rendering, entrypoint
- `collector.py` -- LEASE, GoldenGate Admin REST polling, STATE# writes,
  manager-compatible CloudWatch metric contract (`build_metric_batch`)
- `config.py` -- canonical deployment loading (`envs/dev/goldengate-deployments.yaml`)
- `health_rules.py` -- CONFIG resolution, abend/lag/stall evaluation rules
- `tools/gg_api_contract_probe.py` -- standalone, operator-invoked, read-only
  Admin/Metrics REST contract probe (see "Contract-probe tool" below);
  never runs automatically, never called by monitor.py/collector.py

## Process discovery

`collector.fetch_gg_processes` polls the confirmed Admin REST endpoints only
(`/services/v2/extracts`, `/services/v2/replicats`, `/services/v2/sources`,
each with a `<name>` detail fetch for extracts/replicats) and normalizes
each item to `{process, type, status, lagSeconds, abended, bytes, metrics,
error}`. An item with no valid `name` is skipped rather than recorded under
a synthetic name -- this application can never produce a `STATE#unknown`
record. Unknown statuses normalize to `UNKNOWN`; a malformed/negative lag
value degrades to `0.0` rather than raising; duplicate `(type, name)` pairs
keep only the first occurrence; an empty process list is a valid, non-error
result. One structured, non-sensitive `process_discovery_summary` log line
(deployment name + per-type counts only, never the raw payload) is emitted
per deployment tick.

## Production PMS collection

Once per successful leader tick, `collector.collect_pms` reuses the same
authenticated, TLS-verified HTTPS adminPort 8443 opener as the rest of
Admin REST polling to GET the confirmed process inventory
(`/services/v2/mpoints/processes`) exactly once, then follows up to 20
unique, deduplicated `processName` values with sequential, bounded
`processPerformance` + `serviceHealth` GETs -- the only two production
detail calls. Heartbeat age is derived from `inventory.lastHeartbeat`
(`heartbeat_age_seconds`, timezone-aware, future-clamped to 0); the
`/heartbeat` endpoint returned HTTP 404 for every process in the validated
live environment and is **never** called in production. `/threadPerformance`
and `/process` are intentionally not polled (redundant with inventory /
high-cardinality, deferred); `/services/v2/monitoring/statusChanges`,
`/services/v2/metrics`, and direct authenticated HTTP port 9015 are never
used by this path either.

`collect_pms` never raises -- an inventory failure or every detail request
failing degrades to a closed status (`OK`/`PARTIAL`/`UNAVAILABLE`/
`AUTH_FAILED`/`TLS_FAILED`/`ENDPOINT_UNAVAILABLE`/`INVALID_RESPONSE`) and
**never** marks an otherwise-healthy Admin REST deployment `DOWN`. The
result is folded into the existing guarded/fenced `STATE#_deployment` write
only, under the new `pms` attribute (bounded: per-process
`performance`/`serviceHealth`/`heartbeatAgeSeconds`, capped at 20 entries,
plus deployment-level counts and a `collectedAt` freshness marker) -- no
new DynamoDB table, recordType, or per-PMS-process `STATE#` row is created,
and a standby or fenced collector never issues a PMS request or write (the
exact same lease/fencing rules as every other write in this module).
`cpuTimeUs`/`kernelTimeUs`/`userTimeUs` are cumulative counters and are
never converted into a rate/percentage in this phase. Production PMS
collection never restarts, stops, or otherwise controls GoldenGate.

**The `pms` attribute described above is this repository's own bounded
`STATE#_deployment` PMS enrichment, derived from the live-confirmed
contracts in Phase 4C1/4C1-correction -- it is not part of, and does not
claim to reproduce, any manager-implementation deployment-level PMS
schema.**

A malformed or structurally-invalid response is never treated as healthy:
an inventory response must be a dict whose `response.processes` is a list
(a genuinely empty list is a valid `OK` result with zero counts; anything
else about the shape being wrong -- missing/null/non-dict `response`, or a
non-list `processes` -- is `INVALID_RESPONSE`). A `processPerformance`
detail response must be a dict containing at least one confirmed numeric
field; a `serviceHealth` detail response must be a dict whose `isHealthy`
is a **literal boolean** -- either failure counts that individual detail
call as failed, never successful, and a structurally invalid `serviceHealth`
response is never silently normalized into a false-but-successful
`{isHealthy: false, ...}` result. `status` reflects whether any individual
detail GET actually succeeded this tick -- so a tick where every followed
process got exactly one of its two details is `PARTIAL`, never
`UNAVAILABLE`.

**Total collection time budget:** each PMS request uses
`min(PMS_REQUEST_TIMEOUT_SECONDS, remaining budget)` as its timeout, and
the whole pass (inventory + every detail request) is bounded by a fixed,
non-operator-tunable `PMS_COLLECTION_BUDGET_SECONDS` (30s) measured via an
absolute `time.monotonic()` deadline -- the theoretical unbounded worst
case (1 inventory + up to 40 detail requests at the old 5s-per-request
default) was up to 205s, comfortably past the deployed 120s stale
threshold, which could have made an otherwise-healthy deployment appear
stale before `STATE#_deployment` was even written. Once the deadline
passes, no further PMS request is issued (no sleep, no retry); whatever
was already normalized is preserved, and `status` reflects it honestly
(`PARTIAL` if some detail data was collected first, `UNAVAILABLE` if
none was).

Process names are bounded and validated before ever being followed: a
`processName` must be a non-empty (after trimming), ≤128-character string
with no ASCII control character and never literally `.`/`..`; an invalid
name is skipped before any request, never appears as a `pms.processes` map
key, and is never logged. Accepted names are preserved exactly. Every
numeric PMS field is normalized through a hardened, non-raising helper that
rejects booleans, `NaN`/infinite/negative values, and anything above a
fixed, documented DynamoDB-safe bound (10¹⁵) -- including guarding against
`OverflowError` from an oversized raw integer.

Every `STATE#_deployment` write makes the PMS state unambiguous for the
current tick only: when Admin REST itself is unreachable (PMS is not even
attempted), and when PMS collection fails in a way this module did not
anticipate, the write still carries a **current**, sanitized, empty PMS
snapshot (`collectedAt` stamped to that tick) -- a prior successful tick's
`pms` map is never left attached looking current. Any such unexpected PMS
failure logs only a generic warning naming the canonical deployment --
never the exception text, a traceback, a response body, a process name, a
URL, a hostname, or a credential/CA path.

## CloudWatch metric contract

`collector.build_metric_batch` is a pure function (no boto3 calls) that
builds the full manager-compatible metric set for namespace
`GoldenGate/Pipelines`:

- Deployment (`Deployment`, `DeploymentType`): `LagBreached`, `AbendFailure`,
  `DeploymentDown`, `HeartbeatAgeSeconds`
- Critical service (`Deployment`, `DeploymentType`, `Service`):
  `CriticalServiceDown`
- Process (`Deployment`, `DeploymentType`, `Process`): `ExtractLagSeconds`,
  `ReplicatLagSeconds`, `AbendState`, `AbendEvent`

`HeartbeatAgeSeconds=0` is emitted only immediately after this deployment's
own tick has completed a successful, fenced `STATE#_deployment` write (UP,
STARTING, or DEPLOYMENT_DOWN all count -- the monitor is alive even when
GoldenGate itself is not). A standby replica or a fenced/failed state write
never emits it. If the shared monitor stops publishing entirely, a future
CloudWatch alarm with `treat_missing_data=breaching` is the dead-man signal
-- there is no local heartbeat file.

`collector.publish_metrics_if_enabled` is the single protected publication
boundary both polling_loop metric call sites (Admin-REST-down and normal-UP)
go through -- the only code that constructs a CloudWatch client
(`_cloudwatch_client`) or calls `publish_metric_batch` (the only half that
calls `put_metric_data`). It gates on `cloudwatch_enabled_for(cfg)`, which
requires literal Boolean `True` on both sides by identity, not truthiness:
`CLOUDWATCH_PUBLISH_ENABLED is True` (parsed from the env var by
`_parse_strict_bool_env`, which itself accepts only a trimmed,
case-insensitive `"true"` -- `"1"`, `"yes"`, `"on"`, `"false"`, and any other
string all parse to `False`) **and** `cfg.get("metricsEnabled") is True`. A
CloudWatch client-construction failure (e.g. an IAM/credential problem) is
caught inside this boundary, logged as a sanitized `cloudwatch_client_creation_failed`
event (`event`/`deployment`/`errorCategory` only -- never a raw exception,
traceback, ARN, or hostname), and returns without raising or touching the
DynamoDB deployment status already written that tick. The deployed Helm
default keeps `CLOUDWATCH_PUBLISH_ENABLED=false`, so no CloudWatch client is
ever constructed in the current deployment.

### Controlled DEV activation (Phase 4D2)

`cloudwatch.publishEnabled` (the chart value that renders
`CLOUDWATCH_PUBLISH_ENABLED`) always defaults to `false` in
`helm/goldengate-monitor/values.yaml`, and `envs/dev/goldengate-monitor/values.yaml`
does not override it -- the base default is what deploys unless a specific
run explicitly requests otherwise.

Activation is controlled by a single workflow_dispatch Boolean input on
`.github/workflows/goldengate-monitor.yaml`:

- `enable_cloudwatch_publication` (`type: boolean`, `required: true`,
  `default: false`).

The requested value is passed as an `image.tag`-style Argo CD Application
Helm parameter (`cloudwatch.publishEnabled`), so it is owned by the same
GitHub Actions -> Helm OCI artifact -> Argo CD chain as every other
deployment-specific value -- never a `kubectl set env`/`kubectl patch`, an
unmanaged ConfigMap, or a manual Argo CD Application edit. Every workflow
*attempt* packages its own chart version (`0.<run_number>.<run_attempt>`, a
valid SemVer, unique even across a rerun of the same run) and pushes it to
the (mutable) Helm OCI repository, so Argo CD's `targetRevision` always
points at a distinct, freshly reconciled revision -- no same-tag chart
overwrite is ever required, including on rollback. The monitor image itself
is rebuilt only when a Docker runtime input actually changes -- a
deterministic Git-based hash (mode/blob id/path via `git ls-tree`) over
exactly `Dockerfile`, `.dockerignore`, `requirements.txt`, `monitor.py`,
`collector.py`, `config.py`, `health_rules.py`, and `tools/**` (precisely
what shapes the Docker build context and what the Dockerfile `COPY`s),
combined with the resolved, digest-pinned `MONITOR_BASE_IMAGE` reference
(see below) in the same hash; `README.md`, `requirements-test.txt`, and
`tests/**` are deliberately excluded, so a README-only or tests-only change
can never change the image tag or trigger a rebuild, while a
`.dockerignore` change or a base-image digest change always does. Python
setup, dependency install, syntax validation, and the full unit-test suite
still run on *every* workflow execution regardless of whether the image
already exists -- only the Docker daemon check, ECR login, build, and push
are conditional on that.

**Base image (no public default):** `monitoring/monitor/Dockerfile` declares
`ARG BASE_IMAGE` with no value -- there is no Docker Hub fallback. The
workflow's "Validate approved base image reference" step (which runs before
the image-existence check and before any build) resolves `MONITOR_BASE_IMAGE`
from the repository/environment variable `vars.MONITOR_BASE_IMAGE` (the same
`vars.*` convention as `AWS_REGION`/`GOLDENGATE_AWS_ROLE_ARN`) and fails
closed unless it is: non-empty, a private image inside the approved ECR
registry (`${ECR_REGISTRY}`, never Docker Hub/ghcr.io/quay.io/any other
registry), and digest-pinned (`@sha256:` followed by exactly 64 lowercase
hex characters -- a tag-only reference is rejected). Example accepted shape
(not a real value): `229410149234.dkr.ecr.eu-west-1.amazonaws.com/<approved-base-repository>@sha256:<64 lowercase hex characters>`.
On any failure, only a fixed explanatory message is printed -- the supplied
value itself is never echoed. `--build-arg "BASE_IMAGE=${MONITOR_BASE_IMAGE}"`
is the only way the value reaches `docker build`. If `vars.MONITOR_BASE_IMAGE`
is not yet configured, the workflow fails before any image check/build --
this is intentional; the approved reference is a prerequisite, not something
this workflow invents.

Before enabling (`enable_cloudwatch_publication=true`), the workflow runs a
fail-closed preflight: it finds the currently running `gg-monitor` pod and,
using that pod's own existing IRSA (`GoldenGateMonitorReadRole-dev` -- no new
credential, no IAM change), performs one bounded `GetItem` per enabled
canonical deployment (discovered from `envs/dev/goldengate-deployments.yaml`,
never hardcoded; never a `Scan`) against that deployment's `CONFIG` record.
Output is sanitized to exactly `deployment=<name> metricsEnabled=true` or
`deployment=<name> metricsEnabled-not-literal-true` -- never a raw item,
credential, ARN, hostname, or exception. If any enabled deployment is missing
its `CONFIG` record or does not have `metricsEnabled` set to the literal
Boolean `true`, the workflow fails before the Argo CD Application is ever
touched. If no monitor pod exists yet (first deployment), the workflow fails
with an explicit prerequisite message rather than bypassing the check --
deploy the monitor first with publication disabled, confirm it is healthy,
then re-run with activation requested. When `enable_cloudwatch_publication`
is `false`, none of this runs -- no CONFIG check, no CloudWatch client.

Both the CONFIG preflight and the post-deployment verification select the
monitor pod with a jq filter requiring `status.phase == "Running"`, every
container `ready == true`, **and** `metadata.deletionTimestamp == null` --
never a blind `.items[0]`, and never a pod that is Ready but already
terminating. Only the pod name is ever captured; the full pod object is
never printed. If no such pod exists, the workflow fails closed with a
sanitized prerequisite message.

After Argo CD sync, the workflow's existing runtime-verification step is
extended to confirm the deployed pod's `CLOUDWATCH_PUBLISH_ENABLED` env value
matches exactly what was requested (`cloudwatchPublishEnabled=true` or
`=false`), and -- only when enabled -- re-verifies `CONFIG.metricsEnabled`
post-rollout and scans recent monitor logs for
`cloudwatch_client_creation_failed`, `cloudwatch_put_metric_data_failed`, or
`tick failed`. It never adds a CloudWatch read call: process-level metrics
(`ExtractLagSeconds`/`ReplicatLagSeconds`/`AbendState`/`AbendEvent`) are not
checked and their absence never fails this step, since the current GoldenGate
runtimes do not yet have replication-process STATE rows.

**Rollback** is the same workflow: re-run with
`enable_cloudwatch_publication=false`. No CONFIG mutation, no direct
`kubectl patch`, and no observer/portal/STATE# change is required or
performed -- Argo CD simply reconciles `CLOUDWATCH_PUBLISH_ENABLED` back to
`false`, and the same post-deployment verification confirms it.

### `CONFIG.metricsEnabled` ownership -- this workflow never mutates it

`envs/dev/dynamodb.tf` seeds each canonical deployment's `CONFIG` item
**once**, with `metricsEnabled = false`, and carries
`lifecycle { ignore_changes = [item] }` -- so Terraform's own `apply` never
updates an already-existing `CONFIG` item afterwards, including
`metricsEnabled`. There is therefore no "just re-apply Terraform with the
value flipped" path: tuning an existing item is, by this table's own
design, outside Terraform's reach once it has been seeded.

Phase 4D2's preflight and post-rollout steps only ever **read**
`CONFIG.metricsEnabled` (`GetItem`, never a write, never a `Scan`) to decide
whether to proceed -- they never set it. Live CloudWatch activation
therefore stays blocked in practice until a **separate, independently
approved** controlled CONFIG-update mechanism sets `metricsEnabled` to the
literal Boolean `true` for every enabled deployment; that mechanism does not
exist yet and is explicitly out of scope for this phase. Deploying (or
rolling back) `CLOUDWATCH_PUBLISH_ENABLED=false` never requires any
`CONFIG` mutation at all -- the hard kill switch and the CONFIG gate are
independent, and disabling only ever touches the Helm/Argo CD side.

### Operator-side CloudWatch validation (manual, out of band)

This workflow deliberately never reads CloudWatch itself -- the monitor's
IRSA role (`GoldenGateMonitorReadRole-dev`) is not granted
`cloudwatch:ListMetrics`/`GetMetricData`/any read permission, and none is
added for this validation. After a controlled activation, an operator with
their own already-authorized CloudWatch read access should confirm in the AWS
Console (or via their own credentials), namespace `GoldenGate/Pipelines`:

- Deployment dimensions present: `Deployment=gg-oracle-payments-01` /
  `DeploymentType=oracle`, and `Deployment=gg-postgresql-payments-01` /
  `DeploymentType=postgresql`.
- Deployment-level metrics present: `LagBreached`, `AbendFailure`,
  `DeploymentDown`, `HeartbeatAgeSeconds`.
- Service metric present: `CriticalServiceDown`.
- Expected current limitation: `ExtractLagSeconds`, `ReplicatLagSeconds`,
  `AbendState`, and `AbendEvent` may be absent until real Extract/Replicat/
  Distribution Path processes exist on these runtimes -- this is not a defect.

## Contract-probe tool

`tools/gg_api_contract_probe.py` is a manual, `kubectl exec`-only utility
for inspecting REST JSON *shape* before trusting it in production code --
it performs exactly one read-only GET and prints sanitized STRUCTURAL
metadata only (top-level keys, per-collection item count, field names,
field JSON types) -- never a raw field value, process name, status value,
ID, credential, or secret path. It never writes DynamoDB, never calls
CloudWatch, and only accepts paths beginning with `/services/`.

### Confirmed secure PMS routes (live-environment verified)

Always probed with `--port admin` (HTTPS through adminPort 8443,
authenticated with the same CSI-mounted credentials/CA chain/TLS-SNI the
collector itself uses). Both Oracle and PostgreSQL return HTTP 200:

| Path | Response key |
| --- | --- |
| `/services/v2/mpoints/processes` | `response.processes` |
| `/services/v2/monitoring/statusChanges` | `response.statusChange` |

```
python3 tools/gg_api_contract_probe.py \
  --deployment gg-oracle-payments-01 --port admin --path /services/v2/mpoints/processes

python3 tools/gg_api_contract_probe.py \
  --deployment gg-oracle-payments-01 --port admin --path /services/v2/monitoring/statusChanges
```

**Direct metricsPort 9015 is confirmed plain HTTP** in the current
deployment and is **not** an approved authenticated collection path.
`--port metrics` issues a plain, unauthenticated HTTP request only -- the
mounted admin credentials are never read or attached for a metrics-port
request, and there is no automatic HTTPS-to-HTTP fallback; the scheme is a
fixed function of `--port`, chosen explicitly on each invocation.

**`/services/v2/metrics` is confirmed invalid** in the live environment
(HTTP 404) -- it is not the production PMS endpoint and must not be used as
a recommended example. It remains generically accepted, like any other
`/services/...` path, purely for ad hoc diagnostic compatibility (e.g.
confirming it still 404s). No production code path uses its response, and
no speculative PMS parser exists for it.

### Sanitized collection summary

Every list-valued field directly under `response.*` -- not just
`response.items` -- becomes its own entry under `collections` in the
output, e.g.:

```json
{
  "collections": {
    "processes": {"itemCount": 12, "itemFieldNames": ["...", "..."], "fieldTypes": {"...": ["string"]}, "truncated": false},
    "statusChange": {"itemCount": 3, "itemFieldNames": ["...", "..."], "fieldTypes": {"...": ["string"]}, "truncated": false}
  },
  "collectionsTruncated": false
}
```

Only field **names** and broad JSON **types** (`string`/`number`/`boolean`/
`object`/`array`/`null`) are ever reported -- never a raw value, and nested
objects/arrays are never recursed into (reported only as `"object"`/
`"array"`). Inspection is bounded (max list-valued response fields, max
items per collection, max field names per collection); `truncated` /
`collectionsTruncated` say so without exposing anything. `response.items`
is additionally mirrored into the legacy top-level `itemCount`/
`itemFieldNames`/`fieldTypes` fields for backward compatibility only.

**Bounded input, not operator-tunable:** the response body is read up to
`MAX_RESPONSE_BYTES` (2 MiB) -- a larger body is never parsed, sized, or
echoed, just a fixed `UNEXPECTED_RESPONSE`. `topLevelKeys`/`responseKeys`
and every collection's field names are sorted, capped in count
(`MAX_TOP_LEVEL_KEYS`/`MAX_RESPONSE_KEYS`/`MAX_FIELD_NAMES_PER_COLLECTION`),
and any single key longer than `MAX_KEY_LENGTH` (128) is omitted entirely
(never emitted partial); `topLevelKeysTruncated`/`responseKeysTruncated`/
each collection's own `truncated` flag say so. No CLI option raises or
disables any of these limits.

### Per-process detail capture (`--follow-processes`)

A second, still manual and structural-only mode: GET the confirmed process
inventory (`/services/v2/mpoints/processes`) once, then issue up to 20
sequential, bounded detail GETs -- one per process -- and merge their
structural schemas. Always over authenticated HTTPS adminPort 8443
(`--port admin` is required; direct metricsPort 9015 is never used for this
mode). `--detail` selects one FIXED endpoint suffix only:

```
python3 tools/gg_api_contract_probe.py \
  --deployment gg-oracle-payments-01 --port admin \
  --follow-processes --detail process

python3 tools/gg_api_contract_probe.py \
  --deployment gg-oracle-payments-01 --port admin \
  --follow-processes --detail processPerformance

python3 tools/gg_api_contract_probe.py \
  --deployment gg-oracle-payments-01 --port admin \
  --follow-processes --detail threadPerformance

python3 tools/gg_api_contract_probe.py \
  --deployment gg-oracle-payments-01 --port admin \
  --follow-processes --detail serviceHealth

python3 tools/gg_api_contract_probe.py \
  --deployment gg-oracle-payments-01 --port admin \
  --follow-processes --detail heartbeat
```

The process name used to build each detail endpoint is URL-encoded as
exactly one path segment and is **never** printed -- neither is the process
ID, nor the constructed detail URL. Output is limited to counts
(`inventoryItemCount`/`attemptedCount`/`successCount`/`failureCount`),
`httpStatusCounts`, closed `errorCategoryCounts`
(`AUTH_FAILED`/`TLS_FAILED`/`NOT_FOUND`/`ENDPOINT_UNAVAILABLE`/
`INVALID_JSON`/`UNEXPECTED_RESPONSE`/`UNKNOWN`), and a merged `schema`
(`topLevelKeys`/`collections`/`fieldNames`/`fieldTypes`/`truncated`) --
never a raw response value. One failed detail request never stops the
remaining ones. This mode does not write any monitoring state (no DynamoDB
write) and does not publish CloudWatch; production PMS polling/parsing
remains unimplemented.

## Canonical configuration

Single source: `envs/dev/goldengate-deployments.yaml`, mounted into the pod
(via a ConfigMap staged by `.github/workflows/goldengate-monitor.yaml`) at
`REPO_CONFIG_ROOT` (default `/etc/gg-canonical`). Internal service host, TLS
server name, and default ports are derived from each deployment's `name`,
never stored redundantly.

## DynamoDB access

- CONFIG: `GetItem` only (Terraform-owned; never written here).
- LEASE: `GetItem`/`UpdateItem` (one lease per deployment).
- STATE#_deployment / STATE#<process>: `UpdateItem` (collector), `GetItem`/
  `Query` (portal).
- No `Scan`, no `BatchWriteItem`, no legacy singleton `recordType=STATE`.

## Legacy fallback

For a role whose canonical STATE#_deployment record does not yet exist, the
portal may fall back to the old observer's record under the legacy key
`gg-<pipelineId>-<role>` (derived, never hardcoded). Controlled by
`legacyFallback.enabled` (Helm value); canonical data always takes
precedence once present.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /` | HTML operator portal, grouped by logical pipeline |
| `GET /api/status` | JSON status (see monitor.py's recommended schema); canonical-first, legacy-observer-fallback-second, like the portal |
| `GET /api/processes` | JSON deployment + process detail, **canonical `STATE#` records only** -- no legacy-observer fallback |
| `GET /healthz` | Process liveness only -- never touches DynamoDB |
| `GET /readyz` | Collector readiness + a bounded `DescribeTable` check |

### Manager-compatible portal fields

Both `/` (HTML) and `/api/processes` (JSON) surface the same manager-equivalent
information, built entirely from records the collector already writes --
`GetItem`/`Query` only, never `Scan`, never a write. Per deployment: canonical
name, effective status, an explicit fresh/**STALE** indicator (distinct from
status), `alertsEnabled`, lease holder + valid/expired state, `STATE#_deployment`
record age, and each critical service's reachable/down state. Per process:
name, `processType`, status, a single manager-style combined lag/threshold/mode
cell (e.g. `5s / thr 300s (alert)`, `N/A` when both are absent), record age
(`Ns ago` / `Nm ago` / `Nh ago`), and `consecutiveAbends`. Raw `errorMsg`,
credentials, secret/CA paths, internal hostnames, AWS ARNs, and exception text
are never exposed -- only the existing closed `statusCode`/`statusMessage`
vocabulary. All HTML output is escaped exactly once per value (including the
lease holder). `/api/processes` is deliberately canonical-only (no
legacy-observer fallback, no legacy singleton `STATE` record, no PMS
service-process rows) -- it is a new endpoint, not a replacement for
`/api/status`'s existing migration-compatibility behavior.

The HTML portal (`/`) groups deployments by logical pipeline (`<h2>`) and
renders each deployment as its own card/section beneath that heading --
never a single wide table row with a process table nested inside its final
cell. Each card's own process table lists only that deployment's processes; a
stale process row is marked with both a visible `[STALE]` prefix and a
dedicated CSS row class, so no separate per-process "fresh" column is needed.

Canonical `STATE#<process>` rows are queried independently of whether
`STATE#_deployment` exists: if the deployment-status record is missing (eg a
race during first-tick startup), `effectiveStatus` resolves to `MISSING` (or
falls back to the legacy observer's status when fallback is enabled), but any
`STATE#<process>` rows already present under that deployment's canonical
partition key are still returned and rendered. This holds for both `/`
(`read_runtime_view`) and `/api/processes` (`read_deployment_processes_view`);
neither path ever invents or mixes in legacy process rows.

## IRSA role

`ServiceAccount` `gg-monitor` in namespace `goldengate-monitoring`,
annotated with `GoldenGateMonitorReadRole-dev` -- a separate role from the
GoldenGate runtime ServiceAccounts, which use `GoldenGateSecretsReadRole-dev`.
Scoped to Secrets Manager read, `dynamodb:GetItem`/`Query`/`PutItem`/
`UpdateItem`/`DescribeTable` on `gg-eks-pipeline`, and
`cloudwatch:PutMetricData` restricted to `GoldenGate/Pipelines` (CloudWatch
publishing stays disabled by configuration).

## Local unit tests

```
python3 -m unittest discover -s monitoring/monitor/tests -p "test_*.py" -v
```

## Docker build

`BASE_IMAGE` has no Dockerfile default (no public/Docker Hub fallback) and
must be supplied explicitly -- a digest-pinned, approved private ECR
reference in real use (see "Base image (no public default)" above):

```
docker build \
  --build-arg BASE_IMAGE=<approved-private-ecr-repository>@sha256:<64 lowercase hex characters> \
  -t goldengate-monitor:local monitoring/monitor
```

## Helm chart

`helm/goldengate-monitor` owns namespace `goldengate-monitoring`,
`ServiceAccount gg-monitor`, `Service`, and `Ingress`
(`monitor.goldengate-dev.adcbmis.local`).

## Deployment order

1. IAM/Secrets Terraform workflow (`gg-iam-secrets-deployment.yaml`).
2. Argo CD deployment workflow (`argocd-eks-deployment.yaml`).
3. `goldengate-monitor.yaml`.
4. Verify the portal (`/`, `/api/status`) and the DynamoDB records.
