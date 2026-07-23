# GoldenGate monitoring observer

A small, fail-open sidecar that runs alongside the GoldenGate source and
target containers. It never controls GoldenGate -- it only observes it.

## Purpose

Each observer runs once per GoldenGate pod (source or target) and:

- Passively checks whether the GoldenGate admin port (`8443`) and
  performance-metrics port (`9015`) accept TCP connections.
- Reads filesystem utilisation of the read-only `/u02` mount.
- Writes one component-level state record to DynamoDB.
- Publishes component-level metrics to CloudWatch.
- Exposes its own `/healthz` for its Kubernetes liveness probe.

It does not send HTTP/REST requests to GoldenGate, does not use GoldenGate
credentials, and does not scrape metrics content in this version -- only
TCP-level reachability is checked.

## Fail-open behaviour

The observer must never affect GoldenGate. It never:

- Starts, stops, or restarts GoldenGate, Extract, Replicat, Distribution
  Server, or Receiver Server.
- Calls any GoldenGate process-control API or uses GoldenGate admin
  credentials.
- Writes to `/u02` or `/u03`.
- Acquires a DynamoDB lease, performs leader election, or fences a pod.

If DynamoDB, CloudWatch, IRSA, or the GoldenGate ports are unavailable, the
observer logs the failure and retries on the next cycle. A single failed
external call never crashes the observation loop, and `/healthz` stays
healthy as long as the observation loop itself keeps progressing -- it is
not tied to GoldenGate's or AWS's availability. `/healthz` only turns
unhealthy (HTTP 503, `status: "stale"`) when the loop itself has stopped
making progress (see "Self-health endpoint" below) -- a DOWN/DEGRADED
GoldenGate status, or a failed DynamoDB/CloudWatch call, never causes that
on their own.

## Required environment variables

| Variable | Description |
| --- | --- |
| `AWS_REGION` | AWS region for DynamoDB/CloudWatch clients |
| `DYNAMODB_TABLE` | Shared monitoring table name (`gg-eks-pipeline`) |
| `PIPELINE` | Partition key, e.g. `gg-payments-ora-to-pg-001-source` |
| `DEPLOYMENT_ID` | GoldenGate deployment ID |
| `COMPONENT` | `source` or `target` |
| `ENGINE` | Underlying engine, e.g. `oracle` or `postgresql` |
| `POD_NAME` | Injected from `metadata.name` |
| `POD_NAMESPACE` | Injected from `metadata.namespace` |

## Optional environment variables and defaults

| Variable | Default |
| --- | --- |
| `ADMIN_HOST` | `127.0.0.1` |
| `ADMIN_PORT` | `8443` |
| `METRICS_HOST` | `127.0.0.1` |
| `METRICS_PORT` | `9015` |
| `U02_PATH` | `/u02` |
| `CHECK_INTERVAL_SECONDS` | `30` |
| `CONNECT_TIMEOUT_SECONDS` | `3` |
| `HEALTH_LISTEN_HOST` | `0.0.0.0` |
| `HEALTH_LISTEN_PORT` | `8080` |
| `CLOUDWATCH_NAMESPACE` | `GoldenGate/Pipelines` |
| `OBSERVER_VERSION` | `development` |

Configuration is validated at startup; the process exits with a
`configuration_invalid` log event if anything required is missing.

## DynamoDB record design

Table `gg-eks-pipeline` (partition key `pipeline`, sort key `recordType`).
Exactly one item is updated per cycle, via `UpdateItem` only (no `Scan`,
`DeleteItem`, `BatchWriteItem`, or table administration):

```
pipeline   = "gg-<deploymentId>-<component>"     # e.g. gg-payments-ora-to-pg-001-source
recordType = "STATE#_deployment"
```

Persisted attributes: `deploymentId`, `component`, `engine`, `podName`,
`namespace`, `status` (`HEALTHY`/`DEGRADED`/`DOWN`), `adminEndpointHealthy`,
`metricsEndpointHealthy`, `u02Mounted`, `u02TotalBytes`, `u02FreeBytes`,
`u02UsedPercent` (only when `/u02` stats are available), `recordedAt` (Unix
epoch seconds), `observerVersion`, `errorSummary` (concise, sanitized, no
stack traces or credentials). No `ttl` attribute is ever set on this record.

`/u02` becoming unavailable is not just "not updated" -- the single
`UpdateItem` call carries an explicit `REMOVE` clause for `u02TotalBytes`,
`u02FreeBytes`, and `u02UsedPercent` whenever storage stats can't be read,
so no stale filesystem numbers are ever left behind from a previous cycle.
If total/free are available but a usable percentage can't be computed, only
`u02UsedPercent` is removed; `u02TotalBytes`/`u02FreeBytes` are kept.

## CloudWatch metrics

Namespace: `GoldenGate/Pipelines`. Dimensions: `Pipeline`, `Component`,
`Engine`. Published once per cycle in a single `PutMetricData` call:

- `ObserverHeartbeat` (Count, always `1`)
- `DeploymentHealthy` (Count, `1` only when status is `HEALTHY`)
- `AdminEndpointHealthy` (Count)
- `MetricsEndpointHealthy` (Count)
- `U02Mounted` (Count)
- `U02UsedPercent` (Percent, omitted when filesystem stats are unavailable)

## Self-health endpoint (`/healthz`)

`GET /healthz` reports observation-loop progress, not GoldenGate/AWS health.
Every loop iteration (whether the cycle's internal DynamoDB/CloudWatch calls
succeeded or failed) marks a progress timestamp on a monotonic clock. A
stale threshold is computed from `CHECK_INTERVAL_SECONDS` and
`CONNECT_TIMEOUT_SECONDS` (with a floor of 120 seconds) -- generous enough
to cover a full cycle's bounded TCP/AWS calls without ever flagging a
normal, slow-but-completing cycle as stale.

- `status: "starting"`, HTTP 200 -- before the process has started or during
  the initial startup grace period (before the first completed cycle, still
  inside the stale-threshold window). Compatible with the Helm chart's
  `monitoring.observer.health.initialDelaySeconds`.
- `status: "ok"`, HTTP 200 -- the loop is progressing normally, regardless of
  whether GoldenGate is HEALTHY, DEGRADED, or DOWN, or whether the last
  DynamoDB/CloudWatch call succeeded.
- `status: "stale"`, HTTP 503 -- the loop has stopped progressing (e.g. a
  genuine hang bypassing the bounded TCP/AWS timeouts) for longer than the
  stale threshold. This is the only condition that can affect the
  container's liveness probe.

The JSON body is always exactly `{"status", "observerVersion", "lastCycleAt"}`
-- no configuration or credentials are ever exposed through this endpoint.

## Local unit tests

```
python3 -m unittest discover -s monitoring/observer/tests -p "test_*.py" -v
```

Tests use `unittest.mock` only -- no AWS credentials, network access,
Docker, Kubernetes, or a real GoldenGate/DynamoDB/CloudWatch are required.

## Docker build

```
docker build -t goldengate-observer:local monitoring/observer
```

## Local run example (fake values, no real AWS calls will succeed)

```
docker run --rm \
  -e AWS_REGION=eu-west-1 \
  -e DYNAMODB_TABLE=gg-eks-pipeline \
  -e PIPELINE=gg-payments-ora-to-pg-001-source \
  -e DEPLOYMENT_ID=payments-ora-to-pg-001 \
  -e COMPONENT=source \
  -e ENGINE=oracle \
  -e POD_NAME=local-test \
  -e POD_NAMESPACE=local-test \
  -p 8080:8080 \
  goldengate-observer:local
```

## IRSA credential model

The observer never receives static AWS credentials. It runs under the
existing `ogg-oracle-sa` ServiceAccount, which is already annotated with
`GoldenGateSecretsReadRole-dev`'s IRSA role ARN. That role's inline policy
already grants exactly `dynamodb:GetItem`/`PutItem`/`UpdateItem`/
`DescribeTable` on `gg-eks-pipeline` and `cloudwatch:PutMetricData`
restricted to the `GoldenGate/Pipelines` namespace -- both deployed in an
earlier Terraform change. No new IAM changes are required or made here.

## Read-only `/u02` mount

The observer mounts the same `u02` volume already used by the GoldenGate
container, but as `readOnly: true`. It only calls `os.statvfs` against it --
it never creates, chmods, or writes any file there.

## No GoldenGate credentials required

The observer never reads `OGG_ADMIN`/`OGG_ADMIN_PWD`, never mounts the
GoldenGate admin or certificate CSI volumes, and never uses `envFrom` or
`secretKeyRef`.

## TCP-level checks only

The admin (`8443`) and metrics (`9015`) checks only open and close a TCP
socket. No HTTP/REST request is sent, no credentials are used, and metrics
content is not scraped in this version.

## Process-level monitoring deferred

This version does not observe Extract, Replicat, Distribution Server, or
Receiver Server process state -- only the shared admin/metrics TCP ports
and `/u02` filesystem availability at the pod level.

## Helm/workflow integration

Enabled per deployment via `monitoring.observer.enabled` in
`helm/goldengate/values.yaml` (see `helm/goldengate/templates/_observer.tpl`
for the shared container definition included by both StatefulSets). The
image is built once per observer-content Git tree SHA by
`.github/workflows/goldengate-eks-app.yaml`'s `ensure_observer_image` job,
independent of the per-deployment Helm/Argo CD matrix -- a Helm-only or
deployment-values-only change reuses the existing image and never
triggers a rebuild.

The workflow injects `monitoring.observer.image.repository` as the **full**
ECR registry/repository URI (e.g.
`229410149234.dkr.ecr.eu-west-1.amazonaws.com/goldengate-observer`), not
just the short repository name -- the Helm template only concatenates
`repository:tag`, so the short name alone would render an incomplete,
unpullable image reference. The short name (`goldengate-observer`) is used
only for AWS CLI operations (`describe-repositories`, `create-repository`,
`describe-images`, repository-policy management) inside the workflow.
