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

## DEV storage reset (runbook)

This runbook replaces the storage of DEV runtimes whose EFS filesystems still carry
a stale creation token (anything other than `<env>-<runtime>-efs`). It deletes all
GoldenGate data on those volumes. The RDS PostgreSQL and SQL Server databases are
not touched. Each step marked **approved** needs the project owner's written
approval of the exact commands.

While a stale filesystem exists, the managed-EFS inventory guard blocks every
deploy run in Phase 1 and again in front of Terraform apply. The reset keeps that
guard. It adds one narrow exception: a manual `00 | MAIN` dispatch may name, in
`efs_replacement_authorized_ids`, the filesystems Terraform may replace in that run.
The exception is refused unless every named filesystem:

- is GoldenGate-managed (`ManagedBy`, `GoldenGateEnvironment`, `GoldenGateStorage`
  tags) and tagged with a runtime that is still in the deployment model;
- carries a stale token (a canonical filesystem cannot be named);
- is `available` and has **zero access points**, so Kubernetes is already detached;
- exists (a stale or mistyped authorization fails the run).

Push runs and standalone `10 | SUB` runs can never carry an authorization. All
other filesystems are checked exactly as before. No Git change and no revert is
needed: descriptors, chart and Terraform stay at their canonical state.

Announce a freeze: nobody pushes to `envs/dev/**` or `helm/goldengate/**`, and
nobody dispatches the workflow, until step 6 is done. A push during the freeze
fails Phase 1 harmlessly.

### 0. Read-only evidence (no approval needed)

Run `bash just.yaml` on the SSM kubectl host. Set `RUNTIMES` to the runtimes being
reset (default: the four DEV runtimes). Set `PRE_RESET_IDS` only to look for
leftovers under older identities. Collect from the Terraform owner, with the
pipeline's backend:

- `terraform state list | grep goldengate_runtime_efs`;
- `terraform state show` for each filesystem (`creation_token`, `id`).

Do not continue unless all of the following hold:

- each runtime has exactly one GoldenGate filesystem, and its token is stale;
- the state keys are the canonical runtime IDs, and the state IDs match AWS;
- no PV outside `goldengate-dev` references these filesystems;
- no replication configuration exists;
- the preflight reports no UNKNOWN check.

Record the filesystem IDs and the PVC, PV and access-point names per runtime.

### 1. Remove the runtime compute (approved)

For each runtime, delete its ApplicationSet. Phase 5 applies these directly, and
no parent Application recreates them:

```bash
kubectl -n argocd delete applicationset goldengate-dev-<runtime without gg->-appset
```

Owner-reference garbage collection deletes the child Application. Its
`resources-finalizer.argocd.argoproj.io` cascade-deletes the StatefulSet, Pod,
Services, Ingress, SecretProviderClasses and StorageClass. The `<runtime>-u02` PVC
is kept (`Delete=false`). Wait until the Application is gone. Never strip
finalizers.

Re-run the preflight. Expected: no runtime pods, no `gg-efs-dev-*` StorageClass,
and the old PVCs and PVs still present.

### 2. Remove the PVCs and PVs (approved)

Confirm that no pod mounts the claim:
`kubectl -n goldengate-dev get pods -o jsonpath='{..claimName}'`. Then, for each
PVC from step 0:

```bash
kubectl -n goldengate-dev delete pvc <pvc>          # PV turns Released (Retain)
kubectl delete pv <pv>                              # no CSI delete; access point remains
```

Record each access point ID from the PV `volumeHandle` (`fs-…::fsap-…`) before
deleting the PV. Delete the process `STATE#…` rows under the runtime IDs, if the
preflight reports any. The monitor never deletes rows, so they would surface as
stale processes. Keep `CONFIG` and `LEASE` items: CONFIG is Terraform-owned.

### 2a. Grant the access-point delete permission (corporate IAM, once)

The generated `GoldenGateEKSDeployRole-<env>` policy
(`envs/<env>/policies/goldengate-eks-deploy-<env>/policies/policies_1.json`)
contains the statement `AllowDeleteGoldenGateCsiEfsAccessPoints`:
`elasticfilesystem:DeleteAccessPoint` on `access-point/*` in the workload account
and region, only when the access point carries `efs.csi.aws.com/cluster=true`.
This repository's Terraform applies that policy, but Terraform cannot run while
the stale filesystems exist. The IAM owner therefore applies the committed policy
document to the role through the corporate IAM change process. It must be
identical to the committed file, so the later Terraform apply shows no IAM change.

### 2b. Authorized access-point cleanup dispatch (approved)

Dispatch `00 | MAIN | GoldenGate EKS End-to-End Deployment` with:

- `environment=dev`, `action=efs-access-point-cleanup`;
- `efs_replacement_authorized_ids=<the filesystem IDs from step 0>`;
- `efs_access_point_cleanup_ids=<the access point IDs from step 2>`.

Phase 1 then runs, with the normal role chain (build role, then the EKS deploy role):

1. Re-validate the scope. The filesystems must pass the replacement authorization
   checks, and the inventory may contain no orphan.
2. Read Kubernetes cluster-wide. It refuses if any Argo CD Application or
   ApplicationSet, pod, StatefulSet, StorageClass, PVC or PV still references
   these filesystems, their access points or their runtimes.
3. List the access points of each filesystem. It refuses if any access point is
   not named, belongs to another filesystem or account, lacks the CSI tag, exposes
   the filesystem root, or is in an unexpected state.
4. Delete only the named access points. Filesystems are never deleted.
5. Re-query until every named filesystem has zero access points (at most 30
   checks, 10 seconds apart), then run the normal inventory verification.

The run stays in Validate mode: Terraform and runtime deployment never run.
Push, `validate`, `deploy` and standalone `10 | SUB` runs reject the cleanup input.
A re-run after a partial failure skips access points that are already gone.

### 3. Single authorized dispatch (approved)

**Prerequisite:** the Terraform owner confirms in writing that the corporate
`aws-terraform-apply.yaml` workflow shows the plan and waits for approval before
apply, for this repository and environment. This repository cannot verify that
external workflow. Without that confirmation, do not dispatch; stop here.

Dispatch `00 | MAIN | GoldenGate EKS End-to-End Deployment` with:

- `environment=dev`, `action=deploy`, `deployment_id` left blank;
- `efs_replacement_authorized_ids=<the filesystem IDs from step 0>`, comma-separated;
- `efs_access_point_cleanup_ids` left empty.

The run:

- **Phase 1:** prints `AUTHORIZED REPLACEMENT: …` for each named filesystem and
  passes the IDs to the Terraform job only.
- **Terraform:** the apply gate re-checks the same authorization. The plan must
  show only `-/+` replacement of each named `aws_efs_file_system` and its mount
  targets, now with token `dev-<runtime>-efs`. The Terraform owner rejects any
  other destroy. CONFIG items are unchanged.
- **Phase 5:**
  - recreates each ApplicationSet with the new filesystem ID;
  - creates `gg-efs-dev-<runtime>` (base path `/<runtime>`), the PVC
    `<runtime>-u02` with a new access point, and the StatefulSet;
  - on empty `/u02`, GoldenGate registers `<runtime>`, and the identity hook logs
    `registered deployment '<runtime>' matches the runtime bootstrap name`.

### 4. Close the authorization

No action is needed. The input defaults to empty and applies to that run only.
Every later run must leave it empty, and the guard then accepts only canonical
filesystems.

### 5. Validate

Re-run the preflight. Every item below must hold:

- [ ] Only canonical tokens; `terraform state show` matches AWS.
- [ ] One `<runtime>-u02` PVC per runtime, Bound.
- [ ] Pods Ready, with no restarts during bootstrap.
- [ ] Service Manager registered list is `[ServiceManager, <runtime>]`.
- [ ] Rendered routes use `<runtime>`.
- [ ] `/api/processes` reports `registration.status: OK`.
- [ ] `gg_api_contract_probe.py --check-routing` reports every route as `ROUTED`.
- [ ] The Service Manager UI links work.
- [ ] Phase 6 acceptance is HEALTHY.

### 6. Lift the freeze

Normal push runs resume.

### Recovery

- **Step 2b fails:** read the Phase 1 message. A Kubernetes reference or an
  unknown access point stops the run before any deletion. `AccessDeniedException`
  means step 2a is missing. After a partial deletion, dispatch step 2b again with
  the same inputs.
- **Step 3 Phase 1 fails:** read the guard message. Typical causes are an access
  point that still exists (repeat step 2b), a mistyped ID, or a filesystem that was
  not named. Nothing has changed yet; fix and dispatch again.
- **Terraform fails part-way:** the replaced filesystems now carry canonical
  tokens. Dispatch again naming only the remaining stale IDs. Naming an ID that no
  longer exists fails the run.
- **Phase 5 fails after Terraform succeeded:** dispatch again with the
  authorization empty.
- **A pod stops with `goldengate-identity: ERROR`:** read the message.
  `multiple application deployments` means the volume is not fresh. If the
  container restarted during first bootstrap (liveness), the volume may hold a
  partial deployment. In either case, stop and repeat steps 1–3 for that runtime.
- **Rollback after step 2:** there is none. The old data is gone by design.
