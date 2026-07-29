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
- Credential identity is DEPLOYMENT-level, matching the manager's own
  `credentialsSecretId` concept (`charts/gg-deployment/values.yaml` /
  `templates/statefulset.yaml`, inspected read-only): the manager gives
  every GoldenGate deployment ONE required Secrets Manager identity string
  (`<prefix>/deployments/<name>/credentials`), injected as `SECRET_ID` into
  a `fetch-secrets.py` init container that writes it to a shared file. This
  module preserves that same "one credential identity per deployment, never
  per engine type" concept while using this repository's own approved CSI
  `SecretProviderClass` delivery mechanism instead of an init container --
  each runtime carries its own `adminSecretObject`
  (`secretReferences.admin`) and derived `credentialUserFile` /
  `credentialPasswordFile` paths (`inventory._credential_alias_paths`,
  `"<pipeline>-admin-user"` / `"<pipeline>-admin-password"`), and
  `helm/gg-monitor/templates/secretproviderclass.yaml` generates the CSI
  object list from the same canonical topology data using the identical
  alias convention, so both sides always agree without cross-referencing
  each other. There is no `ADMIN_USER_FILE`/`ADMIN_PASSWORD_FILE` dict keyed
  by engine type anywhere in this module -- a second Oracle deployment with
  a different secret needs only its own topology entry, never a Python or
  chart change.
- Process routing is process-level, not deployment-level (manager
  alignment): the same deployment can appear in more than one topology
  document with different process mappings under different logical
  `pipelineId`s, mirroring the manager's own `{PROCESS: {pipeline_name,
  deployment}}` contract (`utility-sidecar.py build_process_pipeline_map`)
  exactly -- a deployment does not "belong to" one pipeline, only a process
  does. `inventory.build_process_pipeline_map_json` is built once across all
  enabled runtimes in `main()` and filtered locally per deployment inside
  `polling_loop`, the same read-once/filter-per-deployment split the
  manager itself uses.

## Supply chain / packaging (fix 4, manager-alignment correction)

- **MONITOR BASE IMAGE GOVERNANCE GATE**: `Dockerfile`'s `ARG BASE_IMAGE`
  has no default (previously `python:3.12-slim`, a public Docker Hub
  image) and the packaging workflow refuses to run `docker build` at all
  until an operator supplies an approved, digest-pinned, private-ECR
  reference (`repository@sha256:<digest>`) in `MONITOR_BASE_IMAGE`
  (`.github/workflows/gg-monitor-core.yaml` `env:`, currently deliberately
  empty). This image is **not deployable** until that gate is closed --
  matching the manager reference's own private-ECR/digest-pinned/
  boto3-baked-in supply-chain pattern (`charts/gg-deployment/values.yaml`,
  inspected read-only).
- No `pip install` from public PyPI: `requirements.txt` documents what the
  approved base image (or a future approved internal package repository --
  no URL guessed here) must already provide; the Dockerfile fails the
  build closed if any required module is missing from `BASE_IMAGE`.
- **Partially migrated, not complete**: the workflow's new "Generate
  manager-compatible JSON artifacts" step produces `deployments.json`,
  `runtime-config.json` (an approved shared-monitor extension --
  per-deployment canonical key/type/namespace/admin connect detail/
  credential file paths, no secret values), and `process-pipeline-map.json`
  at packaging time from the same canonical YAML, uploaded as build
  artifacts for audit. The **running container still loads its
  configuration from the ConfigMap-mounted YAML via PyYAML at startup**,
  not from these generated files -- fully migrating the runtime read path
  to stdlib `json` only would also remove the ability to pick up a
  topology change via a ConfigMap update alone, without an image rebuild,
  which is a real tradeoff needing its own explicit decision rather than a
  default made in this pass.
