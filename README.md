# Implementation

Repository for implementation work.

## GitHub integration test

This README was created through the connected GitHub integration without cloning the repository.

Implementation details and setup instructions will be added as the project develops.

## GoldenGate deployment identity

A GoldenGate runtime has two names:

| Name | Source | Used for |
| --- | --- | --- |
| Runtime ID | Descriptor folder name, which is also the Helm release name | StatefulSet, Services, Ingress host, SecretProviderClasses, `OGG_DOMAIN`, Argo CD Application, monitor registry key |
| GoldenGate deployment name | `runtime.ogg.deploymentName`, else the runtime ID | `OGG_DEPLOYMENT`: the name registered in the Service Manager on `/u02` and the in-pod reverse-proxy routes |

### Why they can differ

The GoldenGate image creates the Service Manager and deployment only on first
boot, when `/u02/Deployment/etc` does not exist yet. It registers the deployment
under `OGG_DEPLOYMENT` and never renames it afterwards. The deployment directory
is the fixed path `/u02/Deployment`, whatever the name. The image's NGINX config
(`/etc/nginx/ogg.conf`) substitutes `${OGG_DEPLOYMENT}` into the
`/services/<name>/{adminsrvr,distsrvr,pmsrvr,recvsrvr}` locations. Any other
`/services/<x>/...` path falls through to the Service Manager, which answers
`OGG-12031`.

The Service Manager builds its service links from the registered name. If
`OGG_DEPLOYMENT` differs from that name, every link the Service Manager UI
generates misses the proxy routes and fails.

GoldenGate documents adding and removing deployments, but not renaming one.
Removing a deployment deletes its Extracts, Replicats, paths and configuration.
This repository therefore never renames a registration. It sets `OGG_DEPLOYMENT`
to the name already registered on retained `/u02` state.

### Rules (enforced by `automation/goldengate-deployment-model.py` and the chart)

- With no `runtime.ogg.deploymentName`, the registered name is the runtime ID.
  New runtimes need nothing else.
- A name different from the runtime ID is accepted only together with
  `runtime.storage.u02.existingClaim`. Only retained `/u02` state can carry an
  earlier registration.
- The name must start with a letter, contain only letters, digits, `-` or `_`,
  be at most 63 characters, and must not be `ServiceManager`.
- No two runtimes in an environment may share a GoldenGate deployment name
  (compared case-insensitively). No runtime may use another runtime's ID as its
  deployment name.

### Retained DEV runtimes

The four runtimes renamed by the pipeline-aware hierarchy migration still mount
the `/u02` volumes their pre-migration runtimes created. Those runtimes booted
with `OGG_DEPLOYMENT` equal to their old release names:

| Runtime ID | Retained claim | `runtime.ogg.deploymentName` |
| --- | --- | --- |
| `gg-postgresql-repltest-001` | `gg-postgresql-repltest-01-u02` | `gg-postgresql-repltest-01` |
| `gg-mssql-repltest-001` | `gg-mssql-repltest-01-u02` | `gg-mssql-repltest-01` |
| `gg-oracle-repltest-002` | `gg-oracle-repltest-01-u02` | `gg-oracle-repltest-01` |
| `gg-postgresql-repltest-002` | `gg-postgresql-repltest-02-u02` | `gg-postgresql-repltest-02` |

Kubernetes names, hosts, PVCs, EFS and Terraform identities are unchanged. Only
the container's `OGG_DEPLOYMENT` value changes.

### Pre-deployment check (read-only)

Confirm each runtime's registered name before merging. Look at the Service
Manager UI deployment list, or check the generated links and the proxy routes
currently in effect:

```bash
NS=goldengate-dev
POD=gg-postgresql-repltest-001-0
C=ogg-postgresql
kubectl -n "$NS" exec "$POD" -c "$C" -- printenv OGG_DEPLOYMENT
kubectl -n "$NS" exec "$POD" -c "$C" -- grep -o '/services/[^/]*/adminsrvr' /etc/nginx/ogg.conf
kubectl -n "$NS" exec "$POD" -c "$C" -- ls /u02
```

Before this change, the proxy route shows the runtime ID, while Service Manager
links use the old name. If a runtime's registered name differs from the table,
correct its descriptor before deploying.

### Deployment

1. Merge through the normal pull request. The Phase 5 validate step renders the
   chart and fails if `OGG_DEPLOYMENT` differs from the descriptor's resolved
   name or `OGG_DOMAIN` is not canonical.
2. The change edits each StatefulSet's pod template, so Argo CD restarts each
   affected runtime pod once. `/u02` is not modified. To roll out one runtime at
   a time, merge one descriptor per pull request; the chart and model change
   alone restart nothing. Agree a window with the DBA, because any running
   Extract or Replicat is interrupted for the restart.
3. Phase 5 acceptance fails if the live StatefulSet's `OGG_DEPLOYMENT` differs
   from the descriptor.
4. After the monitor is redeployed, `/api/processes` reports `registration`
   per runtime. Phase 6 acceptance requires `MATCH`.

### Post-deployment verification (read-only)

```bash
kubectl -n goldengate-monitoring exec deploy/gg-monitor -- \
  python3 tools/gg_api_contract_probe.py --deployment gg-postgresql-repltest-001 --port admin --check-routing
```

Every `registered` route must be `ROUTED`. The `canonical` routes are reported
only for comparison and are expected to be `WRONG_RESOURCE` for retained
runtimes. Finally, open the Service Manager UI and follow each service link.

### Rollback

Revert the commit. `OGG_DEPLOYMENT` returns to the runtime ID, and each affected
pod restarts once. Neither direction writes to `/u02`, PVCs, EFS or GoldenGate
configuration, and the init container never touches registration state.

### Converging on canonical names (optional, DBA-owned)

Because GoldenGate cannot rename a deployment in place, showing the runtime ID in
Service Manager links means creating a new deployment under that name and
re-creating credentials, connections, Extracts, Replicats and paths in it. The
image keeps a single deployment at `/u02/Deployment`, so the exact procedure
(for example, a fresh volume and a cut-over) must be agreed with Oracle
documentation or support. It is a planned DBA migration with its own approval
and is not automated here. Once the runtime runs on state registered under its
runtime ID, remove `runtime.ogg.deploymentName` from the descriptor.
