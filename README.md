# Implementation

Repository for implementation work.

## GitHub integration test

This README was created through the connected GitHub integration without cloning the repository.

Implementation details and setup instructions will be added as the project develops.

## GoldenGate deployment identity

A GoldenGate runtime has two names:

| Name | Source | Used for |
| --- | --- | --- |
| Runtime ID | Descriptor folder name, which is also the Helm release name | Kubernetes resources, Ingress host, `OGG_DOMAIN`, Argo CD Application, monitor registry key, and `OGG_DEPLOYMENT` as the bootstrap name |
| Registered deployment | The GoldenGate Service Manager on `/u02` | Service Manager links (`/services/<name>/<service>/...`) and the in-pod NGINX routes |

On a fresh `/u02`, GoldenGate registers the runtime ID, so the two names match. A
retained `/u02` keeps the name it was first registered under: the image creates
the deployment only when `/u02/Deployment/etc` is missing and never renames it.
No descriptor declares the registered name.

### Startup resolution

The chart mounts three files from `helm/goldengate/files/goldengate-identity/`
(ConfigMap `<runtime>-goldengate-identity`). Hooks use single-file `subPath`
mounts, so existing image hooks stay visible.

1. `${OGG_DEPLOYMENT_SCRIPTS}/setup/00-goldengate-identity.sh` is sourced by
   `/usr/local/bin/deployment-main.sh` before Service Manager and NGINX start.
   It records whether `/u02` is fresh. It also checks that the entrypoint renders
   NGINX through a bare `replace-variables.sh` call, and defines a
   `replace-variables.sh` shell function that takes precedence over the script.
2. `start_ogg` starts Service Manager and, on a fresh volume only, registers
   `OGG_DEPLOYMENT`.
3. When `start_nginx` calls `replace-variables.sh`, the function runs
   `/opt/goldengate-identity/resolve_deployment.py resolve`. The resolver:
   - queries `http://127.0.0.1:9011/services/v2/deployments` with the
     administrator credentials, read from the environment and never printed;
   - excludes `ServiceManager` and requires exactly one valid application
     deployment;
   - retries for up to 90 attempts, 2 s apart, while Service Manager starts.

   It exports `OGG_DEPLOYMENT=<registered name>` and calls the original
   `replace-variables.sh`.
4. `${OGG_DEPLOYMENT_SCRIPTS}/startup/99-goldengate-identity.sh` runs after NGINX
   starts. It checks that every rendered `/services/<name>/<service>` route uses
   the resolved name.

The container stops with a `goldengate-identity: ERROR:` log line, and NGINX
routes are never rendered with a guessed name, in any of these cases:

- no registered deployment, or more than one;
- an invalid name;
- a malformed response;
- rejected credentials;
- Service Manager unreachable after the retries;
- a fresh volume that registered an unexpected name;
- a setup hook that never ran.

Resolution only reads the Service Manager API. It never writes to `/u02` or the
registration.

### Pre-deployment check (read-only)

For each runtime, confirm the hook support and the single registration:

```bash
P="-n goldengate-dev <runtime>-0 -c <container>"
kubectl exec $P -- sh -c 'grep -n "run_user_scripts\|source\|^[[:space:]]*replace-variables.sh" /usr/local/bin/deployment-main.sh; ls -la /u01/ogg/scripts/setup /u01/ogg/scripts/startup'
kubectl exec $P -- sh -c 'curl -s --max-time 5 -u "$OGG_ADMIN:$OGG_ADMIN_PWD" http://127.0.0.1:9011/services/v2/deployments | python3 -c "import json,sys; print([i.get(\"name\") for i in json.load(sys.stdin)[\"response\"][\"items\"]])"'
```

The second command must print `ServiceManager` and exactly one other name.

### Verification

For each runtime after deployment:

- the pod is Ready, and its `goldengate-identity:` log lines name the registered
  deployment;
- `/api/processes` reports `registration.status: OK`;
- `tools/gg_api_contract_probe.py --check-routing` reports every registered
  route as `ROUTED`;
- the Service Manager UI links open the Administration, Distribution, Receiver
  and Performance Metrics services.

The hooks only read Service Manager. Removing them, by reverting the chart change,
never touches `/u02` or the registration. Without them, a runtime whose
registered name differs from its runtime ID would get unroutable Service Manager
links (OGG-12031).

## Canonical runtime naming

The descriptor folder name (`envs/<env>/pipelines/<pipeline>/<runtime>/`) is the only
runtime identity. Everything below derives from it, with no per-runtime exceptions:

| Identity | Value | Derived by |
| --- | --- | --- |
| Helm release / Argo CD Application | `<runtime>` / `goldengate-<env>-<runtime without leading gg->` | `phase5_runtime.py` |
| StatefulSet, Services, Ingress host, `OGG_DOMAIN` | `<runtime>` | `goldengate.runtimeName` |
| PVC (chart-owned, `Prune=false,Delete=false`) | `<runtime>-u02` | `goldengate.runtimeU02PVCName` |
| StorageClass / EFS base path | `gg-efs-<env>-<runtime>` / `/<runtime>` | `_helpers.tpl` |
| Managed EFS creation token | `<env>-<runtime>-efs` | `derive_efs_creation_token()`, `goldengate_inventory.tf` |
| Monitor CONFIG item, registry key | `<runtime>` | `dynamodb.tf`, `build_registry()` |
| GoldenGate registration (fresh `/u02`) | `<runtime>`, verified at startup | identity hooks |

`runtime.storage.u02.existingClaim` remains a generic capability for an
externally provisioned claim; standard descriptors leave it empty.

## DEV pre-replication storage reset (runbook)

The four DEV runtimes still run on volumes and EFS filesystems created under their
pre-hierarchy names. This runbook replaces them with fresh, canonically named
storage. It deletes all GoldenGate data on those volumes. Each step marked
**approved** needs the project owner's written approval of the exact commands.

**Current state:** the canonical-naming change (`fbe560a`) is already on `main`.
While the old filesystems exist, every deploy run, including a push that only
disables runtimes, fails Phase 1. The managed-EFS inventory guard rejects
filesystems whose creation token is not `<env>-<runtime>-efs`, so Terraform and
Argo CD are never reached. The procedure therefore restores the previous code
temporarily, disables the runtimes, retires the storage, and re-applies the
change.

All commits below go to `main` and trigger `00 | MAIN | GoldenGate EKS End-to-End
Deployment` automatically, because they touch `envs/dev/**`. Announce a freeze:
nobody else pushes to those paths or dispatches the workflow until step 7 is done.

### 0. Read-only preflight (no approval needed)

Run `gg-dev-reset-preflight.sh` on the SSM kubectl host, and collect:

- the Terraform owner's `terraform state list | grep goldengate_runtime_efs`, plus
  `terraform state show` for each instance (`creation_token`, `id`);
- the GitHub Actions result of the automatic push run for `fbe560a`. Expected:
  Phase 1 failed at "Verify AWS-side managed-EFS inventory".

Do not continue unless all of the following hold:

- four GoldenGate filesystems carry pre-reset tokens, and none is canonical;
- the state addresses and filesystem IDs match the AWS inventory;
- no PV outside `goldengate-dev` references these filesystems;
- no replication configuration exists;
- the `fbe560a` run changed nothing.

### 1. Restore the previous code (no data change)

```bash
git revert --no-edit fbe560a && git push origin main
```

The run must pass end to end. Terraform applies no EFS change: at most it
re-creates the `terraform_data` migration contract. Phase 5 converges the four
runtimes to the `34cc67a` chart, which leaves them unchanged if that chart was
already live; otherwise each pod restarts once. Re-run the preflight; nothing may
differ except pod start times.

### 2. Disable the runtimes through the pipeline

In each of the four descriptors, change only `deployment.enabled`, the first
`enabled:` key under `deployment:` at the top of the file. A blanket `sed` on
`  enabled: true` would also disable ingress and persistence. Check with
`git diff`: exactly four changed lines. Commit and push. The run:

- Phase 1 records `deployment-disabled` deletions; these are allowed for managed EFS.
- Terraform deletes the four DynamoDB CONFIG items and their dashboard widgets
  (they are recreated with defaults in step 6). EFS filesystems are kept.
- Phase 5 deletes each ApplicationSet, then its Application. Argo CD
  cascade-deletes the StatefulSet, Services, Ingress, SecretProviderClasses and
  StorageClass. Post-delete acceptance requires `ABSENT`.

Re-run the preflight. Expected: no runtime pods, no `gg-efs-dev-*`
StorageClasses, and the old PVCs and PVs still present.

### 3. Remove Kubernetes storage objects (approved)

Confirm no pod mounts the claim:
`kubectl -n goldengate-dev get pods -o jsonpath='{..claimName}'`. Then, for each
PVC from the preflight:

```bash
kubectl -n goldengate-dev delete pvc <pvc>          # PV turns Released (Retain)
kubectl delete pv <pv>                              # no CSI delete; access point remains
aws efs delete-access-point --region eu-west-1 --access-point-id <fsap from the PV volumeHandle>
```

Delete any remaining GoldenGate access point the preflight lists on these
filesystems. Also delete process `STATE#…` rows under the four runtime IDs, if
the preflight reports any. The monitor never deletes rows, so they would surface
as stale processes after the reset.

### 4. Retire the old filesystems (approved)

From this point until step 6 is pushed, **any** deploy run would recreate empty
filesystems under the old tokens: the restored code's guard passes when no
filesystem exists.

- **Terraform owner (preferred):** with `main` at the step 2 commit and the
  pipeline's backend, for each address shown in step 0:
  `terraform plan -destroy -target='module.goldengate_runtime_efs["<address key>"]'`.
  The plan must list only that filesystem and its mount targets. Apply it, then
  confirm `terraform state list` no longer shows it.
- **AWS CLI (if Terraform access is unavailable):** delete each mount target,
  wait until `describe-mount-targets` is empty, then run
  `aws efs delete-file-system --file-system-id <fs>`. Terraform drops the missing
  resources on the next run's refresh.

Confirm with the preflight that no GoldenGate-managed filesystem remains.

### 5. Re-apply the canonical-naming change and re-enable

```bash
git revert --no-edit <step-2 commit> <step-1 revert commit>
git diff --stat fbe560a -- envs automation helm .github README.md   # must be empty
git push origin main
```

### 6. Expected run

- **Phase 1:** the inventory guard passes, because no filesystems exist.
- **Terraform:** creates four `dev-<runtime>-efs` filesystems with mount targets
  and four default CONFIG items, and removes the migration contract.
- **Phase 5:**
  - resolves the new filesystem IDs;
  - creates `gg-efs-dev-<runtime>` (base path `/<runtime>`), the PVC
    `<runtime>-u02` with a new access point, and the StatefulSet;
  - on empty `/u02`, GoldenGate registers `<runtime>`, and the identity hook logs
    `registered deployment '<runtime>' matches the runtime bootstrap name`.

### 7. Validate

Re-run the preflight. Every item below must hold:

- [ ] Only canonical tokens.
- [ ] One `<runtime>-u02` PVC per runtime, Bound.
- [ ] Pods Ready, with no restarts during bootstrap.
- [ ] Service Manager registered list is `[ServiceManager, <runtime>]`.
- [ ] Rendered routes use `<runtime>`.
- [ ] `/api/processes` reports `registration.status: OK`.
- [ ] `gg_api_contract_probe.py --check-routing` reports every route as `ROUTED`.
- [ ] The Service Manager UI links work.
- [ ] Phase 6 acceptance is HEALTHY.

### Recovery

- **Step 1 or 2 run fails:** fix and re-run. Nothing destructive has happened yet.
- **Step 3 or 4 interrupted:** re-run the preflight and continue from the first
  incomplete item.
- **A deploy ran between steps 4 and 5:** it recreated empty filesystems with old
  tokens. Treat them as old filesystems and repeat steps 2–4 for them before step 5.
- **Step 6 Phase 1 fails naming an old token:** a filesystem was missed; finish step 4.
- **A pod stops with `goldengate-identity: ERROR`:** read the message.
  `multiple application deployments` means the volume is not fresh. If the
  container restarted during first bootstrap (liveness), the volume may hold a
  partial deployment. In either case, stop and repeat steps 2–4 for that runtime.
- **Rollback after step 4:** there is none. The old data is gone by design.
