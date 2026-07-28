# gg-monitor-core

Shared, passive GoldenGate runtime poller/writer for Phase 4.

Reads the canonical runtime inventory from `pipelines/deployments.yaml` and
`topologies/dev/*.yaml` (mounted read-only from the repository, no second
hardcoded runtime list anywhere in this application), polls each enabled
runtime's REST/PMS admin API over its internal Kubernetes Service DNS,
evaluates health using manager-compatible rules (`gg_health_rules.py`, ported
from the manager reference implementation's `gg_health.py` with every
active-healing code path removed), and writes `LEASE` /
`STATE#_deployment` / `STATE#<process>` records plus CloudWatch metrics under
`GoldenGate/Pipelines` -- exactly the record shapes and metric
names/dimensions the manager reference implementation's per-pod
utility-sidecar produces.

This process is passive by construction: it contains no code path that
starts, restarts, stops, or fences a GoldenGate process, no Kubernetes
mutation API call, and no credential-sync-into-GoldenGate path.

## Files

- `gg_monitor_core.py` -- main application: lease management, REST/PMS
  polling, STATE writes, CloudWatch metrics, `/healthz` + `/readyz`.
- `gg_health_rules.py` -- pure health-evaluation logic (no I/O), ported from
  the manager reference implementation, healing paths removed.
- `inventory.py` -- loads the canonical inventory/topology and derives
  manager-compatible `deployments.json` / `process-pipeline-map.json`
  equivalents at runtime.
- `tests/` -- focused unit tests (inventory parsing, canonical key
  derivation, lease conditions, deployment-state item shape, metric
  dimensions, REST response parsing, credential redaction, passive
  behavior).

## Manager reference divergences (see code comments for full detail)

- No utility sidecar, no Fluent Bit sidecar -- this is a standalone shared
  Deployment, not a per-pod container.
- `distpathStallChecks`, never `dispatchStallChecks` (confirmed manager
  Terraform-seed defect, corrected in Phase 3).
- `STATE#_deployment` / `STATE#<process>` only -- never the manager's legacy
  singleton `STATE`.
- No `HeartbeatAgeSeconds` metric -- no local heartbeat file exists for a
  remote poller.
- No `heal_decision` circuit breaker, no critical-service self-heal restart,
  no `FAILOVER` exit path, no credential-sync-into-GoldenGate thread.
