# gg-monitor-core

Shared, passive GoldenGate runtime poller/writer for Phase 4.

Reads the canonical runtime inventory from `pipelines/deployments.yaml` and
`topologies/dev/*.yaml` (mounted read-only from the repository, no second
hardcoded runtime list anywhere in this application), polls each enabled
runtime's GoldenGate Admin REST API (port 8443 only -- the separate
PMS/metrics endpoint on port 9015 is retained in topology for a later,
explicitly implemented phase and is not polled by this module) over its
internal Kubernetes Service DNS, evaluates health using manager-compatible
rules (`gg_health_rules.py`, ported from the manager reference
implementation's `gg_health.py` with every active-healing code path
removed), and writes `LEASE` / `STATE#_deployment` / `STATE#<process>`
records plus CloudWatch metrics under `GoldenGate/Pipelines` -- exactly the
record shapes and metric names/dimensions the manager reference
implementation's per-pod utility-sidecar produces.

This process is passive by construction: it contains no code path that
starts, restarts, stops, or fences a GoldenGate process, no Kubernetes
mutation API call, and no credential-sync-into-GoldenGate path.

## Files

- `gg_monitor_core.py` -- main application: dedicated lease-control loop
  (RENEW_INTERVAL cadence) independent of the polling loop
  (checkIntervalSeconds cadence), GoldenGate Admin REST polling, STATE
  writes, CloudWatch metrics, `/healthz` + `/readyz`.
- `gg_health_rules.py` -- pure health-evaluation logic (no I/O), ported from
  the manager reference implementation, healing paths removed.
- `inventory.py` -- loads the canonical inventory/topology, validates every
  enabled runtime's required configuration at startup, and derives
  manager-compatible `deployments.json` / `process-pipeline-map.json`
  equivalents at runtime.
- `tests/` -- focused unit tests (inventory parsing, canonical key
  derivation, lease timeline/renewal, readiness semantics, TLS
  connect-host/tlsServerName separation, Admin REST response parsing,
  credential redaction, passive behavior).

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
- Lease renewal runs on its own `RENEW_INTERVAL` (default 5s) cadence,
  independent of the poll interval (`CONFIG.checkIntervalSeconds`, default
  60s) -- required because a 30s-TTL lease renewed only once per 60s poll
  tick would always expire mid-sleep.
- TLS: connects to the internal Kubernetes Service DNS host but sends
  SNI/verifies the certificate against a separate `tlsServerName`
  (`_SNIHTTPSConnection`) -- `check_hostname=True` and `CERT_REQUIRED`
  always, never `CERT_NONE`. This differs from the manager's own
  `_pms_ssl_context()`, which is loopback-only and therefore skips hostname
  checking; this module is a real network client and does not.
- Readiness (`/readyz`) reflects the monitor's OWN operational state
  (inventory validated, credentials present, TLS context builds, CONFIG
  read succeeds, lease API path succeeds) -- never GoldenGate Admin REST
  reachability. An unreachable runtime is recorded as `DEPLOYMENT_DOWN`
  while the monitor pod itself stays Ready.
