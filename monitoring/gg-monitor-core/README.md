# gg-monitor-core

Shared, passive GoldenGate runtime poller/writer/portal for Phase 4.

Reads the canonical runtime inventory from `pipelines/deployments.yaml` and
`topologies/dev/*.yaml` (mounted read-only from the repository, no second
hardcoded runtime list anywhere in this application), polls each enabled
runtime's GoldenGate Admin REST API (port 8443 only -- the separate
PMS/metrics endpoint on port 9015 is retained in topology for a later,
explicitly implemented phase and is not polled by this module, since no
verified PMS API contract exists in the manager reference code or supplied
documents) over its internal Kubernetes Service DNS, evaluates health using
manager-compatible rules (`gg_health_rules.py`, ported from the manager
reference implementation's `gg_health.py` with every active-healing code
path removed), and writes `LEASE` / `STATE#_deployment` / `STATE#<process>`
records -- exactly the record shapes the manager reference implementation's
per-pod utility-sidecar produces. It also serves a read-only status portal
(`/`, `/api/status`) over the same records, alongside `/healthz`/`/readyz`.

CloudWatch metric publication under `GoldenGate/Pipelines` is OPTIONAL and
requires **both** of two independent gates (correction pass -- a hard
application-level kill switch, `CLOUDWATCH_PUBLISH_ENABLED`, was added on
top of the existing `CONFIG.metricsEnabled` field): `CLOUDWATCH_PUBLISH_ENABLED`
(env var on the Deployment, default **`false`**, accepts only
`true`/`1`/`yes` case-insensitively -- anything else, including missing or
malformed, is `false`) **and** `CONFIG.metricsEnabled` (defaults **false**
this phase). See `gg_monitor_core.cloudwatch_enabled_for()`. The env var
exists specifically because the CONFIG item is Terraform-owned with
`lifecycle.ignore_changes = [item]` (`envs/dev/dynamodb.tf`) -- an
already-applied CONFIG item can carry `metricsEnabled=true` forever, and
`CONFIG.metricsEnabled=true` alone can never turn CloudWatch on while
`CLOUDWATCH_PUBLISH_ENABLED` stays false. CloudWatch alarms, dashboards,
Logs, SNS, Fluent Bit, CloudWatch Agent, and Container Insights are all out
of scope until a separate, later validated phase; `CLOUDWATCH_PUBLISH_ENABLED`
is not set to `true` in any environment values file. The monitor starts,
becomes Ready, polls, writes LEASE/STATE, and serves the portal with zero
CloudWatch IAM permission required, regardless of either gate's value.

This process is passive by construction: it contains no code path that
starts, restarts, stops, or fences a GoldenGate process, no Kubernetes
mutation API call, and no credential-sync-into-GoldenGate path.

## Files

- `gg_monitor_core.py` -- main application: dedicated lease-control loop
  (RENEW_INTERVAL cadence) independent of the polling loop
  (checkIntervalSeconds cadence), GoldenGate Admin REST polling, STATE
  writes, optional CloudWatch metrics (metricsEnabled-gated), the read-only
  portal (`/`, `/api/status`), and `/healthz` + `/readyz`.
- `gg_health_rules.py` -- pure health-evaluation logic (no I/O), ported from
  the manager reference implementation, healing paths removed.
- `inventory.py` -- loads the canonical inventory/topology, validates every
  enabled runtime's required configuration at startup, and derives
  manager-compatible `deployments.json` / `process-pipeline-map.json` /
  `runtime-config.json` equivalents plus the logical-pipeline
  (source/target) grouping the portal renders.
- `tests/` -- focused unit tests (inventory parsing, canonical key
  derivation, lease timeline/renewal, readiness semantics, TLS
  connect-host/tlsServerName separation, Admin REST response parsing,
  credential redaction, passive behavior, portal rendering/error handling,
  CloudWatch-optional behavior).

## Portal

`/` renders an HTML status page and `/api/status` the equivalent JSON: for
every canonical runtime, its deployment health (status/staleness), LEASE
holder and freshness, and per-process STATE (name/type/status/lag/age/abend
count) -- plus the logical topology relationship (e.g. "Logical pipeline:
payments-ora-to-pg-001 -- source: gg-oracle-payments-01, target:
gg-postgresql-payments-01"), which is presented explicitly as a
relationship between two runtimes, never as if it were a GoldenGate runtime
identity itself. Read-only (GetItem/Query only, never Scan, never writes);
any replica can serve accurate portal data regardless of which replica
currently holds a given runtime's LEASE. This does not replace or modify
the separately deployed `monitoring/monitor/monitor.py` +
`helm/goldengate-monitor` portal, left unchanged. No Kubernetes `Service`
or `Ingress` was added for this portal this pass -- it remains reachable
only via in-cluster access or a future controlled port-forward; external
URL exposure is a later infrastructure decision, out of scope here.

**Thread safety (correction pass):** `ThreadingHTTPServer` hands each
request its own thread. The portal never shares one boto3 Table/Resource
object across those threads -- `_make_handler`/`start_http_server` accept a
`portal_table_factory` **callable**, not a pre-built object; each request
that actually needs DynamoDB access (`/` and `/api/status` only --
`/healthz`/`/readyz` never touch it) calls the factory itself to obtain its
own, independent Table object, used only within that one request and then
discarded.

**Error sanitization (correction pass):** raw `STATE#<process>.errorMsg`
(potentially database hostnames, service URLs, schema names, internal
paths, secret references, driver/TLS detail) is never exposed to `/api/status`
or portal HTML. It is replaced with a sanitized triple: `hasError`
(bool), `statusCode` (a fixed, closed enum -- `NONE`, `POLL_FAILED`,
`AUTH_FAILED`, `TLS_FAILED`, `ENDPOINT_UNAVAILABLE`, `STALE`,
`PROCESS_ABENDED`, `UNKNOWN`), and `statusMessage` (a fixed, generic
message per code). The raw text is still written to DynamoDB by
`write_process_state` (internal, for a future alerter) -- only the
portal's own output is sanitized. Never renders credential file paths or
Secrets Manager object references either; a DynamoDB read failure (at the
factory level or inside a query) shows the same fixed, client-safe message
(the real error is logged server-side only, never returned to the caller).

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

## Supply chain / packaging (correction pass: proven build pattern reused)

`gg-monitor-core` follows the same Docker build and delivery pattern already
proven by the previous successful GoldenGate monitor implementation
(`monitoring/monitor/Dockerfile`, `.github/workflows/goldengate-monitor.yaml`
-- inspected read-only; application code, DynamoDB schema, and observer
architecture were **not** copied, only the container build/delivery
pattern):

- `Dockerfile`'s `ARG BASE_IMAGE` defaults to the public `python:3.12-slim`
  image, used only as the GitHub-hosted runner's own Docker build input.
  Pinned dependencies (exact `==` versions in `requirements.txt`) are
  installed with `pip install --no-cache-dir --no-compile -r
  requirements.txt` during the build, followed by an explicit
  `python3 -c "import boto3, botocore, yaml, jmespath, dateutil,
  s3transfer, six, urllib3"` verification step that fails the build closed
  if any expected module did not actually install. `MONITOR_BASE_IMAGE` is
  **no longer required** -- it has been fully removed from both the
  Dockerfile and `.github/workflows/gg-monitor-core.yaml`; the workflow no
  longer passes a `BASE_IMAGE` build-arg at all, and there is no
  "governance gate" step to satisfy.
- The completed application image is still pushed only to the private ECR
  repository, `229410149234.dkr.ecr.eu-west-1.amazonaws.com/gg-monitor-core`
  -- the public base image is never the final deployed artifact's registry.
  Immutable ECR tags and `scanOnPush=true` are unchanged; the image tag
  remains content-addressed (`mon-core-<git-tree-sha:12>`), so a rebuild of
  unchanged source reuses the existing image rather than pushing a
  duplicate.
- Runs as non-root `10001:10001` (unchanged); `ENTRYPOINT ["python3",
  "gg_monitor_core.py"]`; port `8080` exposed; the same `/healthz`-based
  `HEALTHCHECK` as before.
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

**Current phase and status**: DynamoDB (`gg-eks-pipeline`, `gg-alerts`,
`gg-metrics-history`) and shared-monitor (collector + portal) validation
remain the current phase's focus -- this correction pass only changes how
the image is *built*, not what it does at runtime. CloudWatch stays
disabled by default (see above); Fluent Bit remains untouched and out of
scope. **This application has not been deployed** -- all validation in this
repository is local (unit tests, `helm lint`/`helm template`, static
Dockerfile/workflow review, and a local `docker build` where Docker is
available); no image has been pushed to ECR and no Helm release has been
installed from this pass.
