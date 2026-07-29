# GoldenGate shared monitor

The one shared GoldenGate monitoring application: a passive collector
(GoldenGate Admin REST polling, LEASE ownership, STATE#_deployment /
STATE#<process> writes) plus a read-only operator portal, in a single
process. Never writes CONFIG, never Scans, never restarts/fences a
GoldenGate process, never calls the Kubernetes API.

## Modules

- `monitor.py` -- HTTP server, portal status assembly/rendering, entrypoint
- `collector.py` -- LEASE, GoldenGate Admin REST polling, STATE# writes
- `config.py` -- canonical deployment loading (`envs/dev/goldengate-deployments.yaml`)
- `health_rules.py` -- CONFIG resolution, abend/lag/stall evaluation rules

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
