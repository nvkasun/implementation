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
storage. It deletes all GoldenGate data on those volumes. Run it only with the
project owner's written approval of each destructive step, and only while no
replication is configured.

Merge the canonical-naming change (no legacy creation tokens, empty
`existingClaim`) only at step 6. Until then, `main` must keep the previous code.
If that change reaches the pipeline earlier, the managed-EFS inventory guard fails
Phase 1, because the old filesystems carry the old creation tokens. That stops
Terraform before it can replace them, and it also stops every deploy.

Variables used below:

```bash
ENV=dev; NS=goldengate-dev; ARGO_NS=argocd; REGION=eu-west-1
RUNTIMES="gg-postgresql-repltest-001 gg-mssql-repltest-001 gg-oracle-repltest-002 gg-postgresql-repltest-002"
```

### 1. Inventory (read-only)

```bash
kubectl -n "$ARGO_NS" get applicationsets,applications | grep goldengate-dev
kubectl -n "$NS" get statefulsets,pods,pvc -o wide
kubectl get pv -o custom-columns=PV:.metadata.name,CLAIM:.spec.claimRef.name,SC:.spec.storageClassName,RECLAIM:.spec.persistentVolumeReclaimPolicy,HANDLE:.spec.csi.volumeHandle
kubectl get storageclass | grep gg-efs-
aws efs describe-file-systems --region "$REGION" --query "FileSystems[].{id:FileSystemId,token:CreationToken,state:LifeCycleState,tags:Tags}"
for FS in <each GoldenGate fs-id>; do
  aws efs describe-access-points --region "$REGION" --file-system-id "$FS" --query "AccessPoints[].{ap:AccessPointId,path:RootDirectory.Path,tags:Tags}"
  aws efs describe-mount-targets --region "$REGION" --file-system-id "$FS" --query "MountTargets[].{mt:MountTargetId,subnet:SubnetId}"
done
aws dynamodb query --region "$REGION" --table-name gg-eks-pipeline --key-condition-expression "pipeline = :p" \
  --expression-attribute-values '{":p":{"S":"<old or current runtime id>"}}' --projection-expression "pipeline, recordType"
```

Record for each runtime: the PVC name, PV name, `volumeHandle`
(`fs-…::fsap-…`), StorageClass, filesystem ID and creation token, access points,
and mount targets.

### 2. Ownership checks (stop if any fails)

- Each filesystem is tagged `ManagedBy=goldengate-eks-app`,
  `GoldenGateEnvironment=dev`, and `GoldenGateDeploymentId=<runtime>`. Its token is
  one of `dev-gg-postgresql-repltest-01-efs`, `dev-gg-mssql-repltest-01-efs`,
  `dev-gg-oracle-repltest-01-efs` or `dev-gg-postgresql-repltest-02-efs`.
- No PV outside the four GoldenGate claims references these filesystem IDs:
  `kubectl get pv -o json | jq -r '.items[] | select(.spec.csi.volumeHandle | startswith("<fs-id>")) | .metadata.name'`.
- The access points' root directories sit under the GoldenGate base paths only.
- The shared EFS security group (`efs.sharedSecurityGroupDescription` in
  `envs/dev/environment.yaml`) is infrastructure-owned. Terraform only reads it.
  It is never deleted.
- PVs show `RECLAIM=Retain`. Deleting a PVC or PV therefore never deletes the
  access point or the filesystem; each is removed explicitly below.
- No Extract, Replicat, Distribution Path or credential needs to be kept.

### 3. Stop the runtimes through the pipeline

Commit `deployment.enabled: false` in the four descriptors with the current code
and run the normal Deploy. The removal flow deletes each ApplicationSet,
Application, StatefulSet, Service, Ingress, SecretProviderClass and StorageClass.
It retains PVCs, PVs and EFS, because managed storage is never removed by
disabling a runtime. Verify:

```bash
kubectl -n "$ARGO_NS" get applicationsets,applications | grep goldengate-dev    # none for these runtimes
kubectl -n "$NS" get statefulsets,pods                                          # none for these runtimes
kubectl get storageclass | grep gg-efs-dev-                                     # none for these runtimes
```

### 4. Remove Kubernetes storage objects (owner-approved)

For each PVC recorded in step 1:

```bash
kubectl -n "$NS" delete pvc <pvc-name>            # PV becomes Released (Retain)
kubectl delete pv <pv-name>                       # access point still exists
aws efs delete-access-point --region "$REGION" --access-point-id <fsap-id from volumeHandle>
```

Then list the access points again and delete any other GoldenGate access point
left on these filesystems.

### 5. Retire the old filesystems (owner-approved)

Freeze Deploy runs until step 6 completes. A run with the previous code would
recreate the filesystems under the old tokens.

- **Preferred (Terraform owner, same backend as the pipeline):**
  run `terraform plan -destroy -target='module.goldengate_runtime_efs["<runtime>"]'`
  for each runtime. Confirm the plan destroys only that filesystem and its mount
  targets, then apply it.
- **Alternative (AWS CLI):** delete each mount target, wait until none remain, then
  run `aws efs delete-file-system --file-system-id <fs-id>`. The next pipeline
  apply refreshes state, drops the missing resources, and creates the new ones.

Confirm with `aws efs describe-file-systems` that no filesystem with an old token
remains.

### 6. Merge the canonical-naming change and redeploy

Set `deployment.enabled: true` again in the same change, then run Deploy.
Expected results:

- Phase 1: the inventory guard passes, because no filesystems exist yet.
- Terraform creates `dev-<runtime>-efs` filesystems and mount targets. It also
  removes the retired DynamoDB migration placeholder. CONFIG items are unchanged
  (`ignore_changes`).
- Phase 5 creates `gg-efs-dev-<runtime>` with the new filesystem ID. It also
  creates the chart-owned PVC `<runtime>-u02`, which gets a new access point
  under `/<runtime>`, and the StatefulSet.
- On empty `/u02`, GoldenGate registers `<runtime>`. The identity hook logs
  `registered deployment '<runtime>' matches the runtime bootstrap name`.

### 7. Validate

```bash
kubectl -n "$NS" get pvc        # exactly <runtime>-u02 for each runtime, Bound
kubectl -n "$NS" logs <runtime>-0 -c <container> | grep goldengate-identity
aws efs describe-file-systems --region "$REGION" --query "FileSystems[].CreationToken"   # dev-<runtime>-efs only
```

Check the following:

- `/api/processes` reports `registration.status: OK` with
  `registeredName=<runtime>`.
- `gg_api_contract_probe.py --check-routing` reports every route as `ROUTED`,
  with no runtime alias.
- The Service Manager UI links open all four services.
- Phase 6 acceptance is HEALTHY.

Optionally delete stale DynamoDB `STATE#`/`LEASE` items found in step 1 under
old runtime IDs. CONFIG items under the current runtime IDs are kept.

### Recovery

- **Step 3 fails:** re-run the Deploy. The removal flow is idempotent and never
  deletes storage.
- **Step 4 or 5 is interrupted:** re-run the inventory and continue. Nothing is
  recreated until step 6.
- **Step 6 fails in Phase 1** (guard error naming an old token): an old
  filesystem still exists. Finish step 5.
- **A pod stops with `goldengate-identity: ERROR`:** read the message. For
  `multiple application deployments`, the volume is not fresh. Do not delete data
  ad hoc; repeat steps 3–6 for that runtime.
- **Rolling back to the previous code:** this works only before step 5. After the
  old filesystems are gone, the previous code would create new, empty filesystems
  under the old tokens.
