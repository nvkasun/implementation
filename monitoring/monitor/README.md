# GoldenGate shared monitoring portal (Phase 2)

A single, read-only HTTP portal that gives operators one cross-deployment
view of GoldenGate monitoring state. It never touches GoldenGate, never
calls the Kubernetes API, and never writes to DynamoDB.

## Architecture

One shared `Deployment` (`gg-monitor`, 1 replica) runs in its own namespace
(`goldengate-monitoring`), independent of every per-deployment GoldenGate
namespace. It reads the same DynamoDB table that the
[observer sidecars](../observer/README.md) already write to
(`monitoring/observer/**`, embedded in `helm/goldengate`), using its own
read-only IRSA role. The portal and the observers are deployed and versioned
completely independently: a monitor code change never touches a GoldenGate
StatefulSet, and a GoldenGate/observer change never touches the monitor.

## Read-only behaviour

- Only `dynamodb:GetItem` and `dynamodb:DescribeTable` are ever called.
- No `Scan`, no `BatchGetItem` (the granted IAM role does not allow it), no
  `PutItem`/`UpdateItem`/`DeleteItem`/`BatchWriteItem`.
- No GoldenGate credentials, no Kubernetes API calls, no polling of the ALB.
- The portal reads only an explicitly configured list of pipeline keys --
  never discovers deployments dynamically and never Scans the table.

## DynamoDB schema

Table `gg-eks-pipeline` (partition key `pipeline`, sort key `recordType`).
For each configured pipeline key, exactly one `GetItem` is issued:

```
GetItem(pipeline=<configured pipeline>, recordType="STATE#_deployment")
```

Only these attributes are ever read out of the raw item and exposed --
everything else on the item (including any future attribute) is ignored:
`deploymentId`, `component`, `engine`, `status`, `adminEndpointHealthy`,
`metricsEndpointHealthy`, `u02Mounted`, `podName`, `namespace`, `recordedAt`,
`observerVersion`, `errorSummary`.

## Configured pipeline list

Initial deployment (`envs/dev/goldengate-monitor/values.yaml`):

```
gg-payments-ora-to-pg-001-source
gg-payments-ora-to-pg-001-target
```

Adding a pipeline is a values-only change (`pipelines:` in the Helm chart) --
no code change is required to observe a new deployment.

## Staleness / effective status calculation

`recordedAt` is Unix epoch seconds, updated by the observer roughly every 30
seconds. `STALE_AFTER_SECONDS` (default `120` -- four observer cycles) governs
per-component `effectiveStatus`, in this exact precedence:

1. No item found → `MISSING`
2. Item older than `STALE_AFTER_SECONDS` → `STALE` (regardless of its raw `status`)
3. Raw `status: DOWN` → `DOWN`
4. Raw `status: DEGRADED` → `DEGRADED`
5. Raw `status: HEALTHY` → `HEALTHY`
6. Any other raw `status` value → `UNKNOWN`

Components are grouped by `deploymentId`. A deployment's `overallStatus` is
the most severe status among its components, in order
`DOWN > MISSING > STALE > DEGRADED > UNKNOWN > HEALTHY` -- a deployment is
never reported `HEALTHY` while any configured component is missing or stale.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /` | HTML operator portal, grouped by `deploymentId`, 30s meta-refresh, inline CSS only |
| `GET /api/status` | Stable JSON schema (see below) |
| `GET /healthz` | Process liveness only -- never touches DynamoDB |
| `GET /readyz` | Bounded `DescribeTable` readiness check; 503 on failure/timeout |

`/api/status` returns HTTP 503 with a sanitized JSON error body (no AWS
request IDs, no credentials, no stack traces) if any configured pipeline's
`GetItem` call fails. The HTML root page instead renders a sanitized error
banner and still returns 200, so the portal shell always loads.

## Local unit tests

```
python3 -m unittest discover -s monitoring/monitor/tests -p "test_*.py" -v
```

Uses `unittest.mock` only -- no AWS credentials, network access, Docker,
Kubernetes, Helm, or a real DynamoDB table are required.

## Docker build

```
docker build -t goldengate-monitor:local monitoring/monitor
```

## Helm chart

`helm/goldengate-monitor` (standalone, independent of `helm/goldengate`).
See its own values.yaml for the full contract: `pipelines`, `dynamodb.tableName`,
`serviceAccount.roleArn`, `staleAfterSeconds`, `refreshSeconds`, and the
internal ALB ingress block.

## IRSA role

The portal runs under `ServiceAccount` `gg-monitor` in namespace
`goldengate-monitoring`, annotated with the already-deployed
`GoldenGateMonitorReadRole-dev` role
(`arn:aws:iam::668311715351:role/GoldenGateMonitorReadRole-dev`), scoped to
exactly `dynamodb:GetItem`/`Query`/`DescribeTable` on
`arn:aws:dynamodb:eu-west-1:668311715351:table/gg-eks-pipeline`. No new IAM
role or permission was created for this portal.

## No GoldenGate credentials, no Kubernetes API, no Scan, no writes

The portal never reads `OGG_ADMIN`/`OGG_ADMIN_PWD`, never mounts a GoldenGate
secret or CSI volume, never calls the Kubernetes API, never issues a
`Scan`, and never issues any DynamoDB write operation.

## Internal ALB, HTTP backend

The portal serves plain HTTP on port 8080 inside the cluster. The shared ALB
(group `gg-poc-dev-alb`) terminates HTTPS externally and forwards HTTP to the
pod -- `backend-protocol` and `healthcheck-protocol` are both `HTTP` for this
Ingress (unlike GoldenGate's own HTTPS admin backend). Do not copy
GoldenGate's HTTPS backend/health-check settings onto this Ingress.

## Argo CD repository credential requirement

The Argo CD in-cluster token-sync CronJob must already be refreshing a
`argocd-ecr-goldengate-monitor-oci` repository Secret before Argo CD can pull
the `helm/goldengate-monitor` OCI chart. That Secret is created by the
`.github/workflows/argocd-eks-deployment.yaml` workflow's multi-repository
token sync -- it is not created by the monitor workflow itself.

## Deployment order

1. Run the IAM/Secrets Terraform workflow (`gg-iam-secrets-deployment.yaml`)
   to apply the extended Argo CD ECR read policy
   (`AllowReadGoldengateMonitorHelmOciRepository`).
2. Run the Argo CD deployment workflow (`argocd-eks-deployment.yaml`) to
   deploy the multi-repository token sync and create both repository
   Secrets (`argocd-ecr-goldengate-oci` and `argocd-ecr-goldengate-monitor-oci`).
3. Run the GoldenGate monitor workflow (`goldengate-monitor.yaml`).
4. Verify the portal (`/`, `/api/status`) and the underlying DynamoDB
   records.

Running the monitor workflow before steps 1 and 2 is expected to fail
clearly (missing Helm OCI repository policy / missing repository Secret)
rather than injecting a short-lived credential as a workaround -- the
in-cluster IRSA token sync remains the only long-term credential mechanism
for Argo CD's ECR pulls.
