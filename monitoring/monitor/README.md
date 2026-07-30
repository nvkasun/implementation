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

`collector.publish_metric_batch` is the only half that calls boto3, and is
only ever invoked behind `cloudwatch_enabled_for(cfg)`
(`CLOUDWATCH_PUBLISH_ENABLED=true` **and** `CONFIG.metricsEnabled=true`).
The deployed Helm default keeps `CLOUDWATCH_PUBLISH_ENABLED=false`, so no
CloudWatch client is ever constructed in the current deployment.

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
| `GET /api/status` | JSON status (see monitor.py's recommended schema) |
| `GET /healthz` | Process liveness only -- never touches DynamoDB |
| `GET /readyz` | Collector readiness + a bounded `DescribeTable` check |

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

```
docker build -t goldengate-monitor:local monitoring/monitor
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
