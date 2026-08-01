# Phase 5B2 Legacy Cleanup Runbook

Status: **PLANNING ONLY — NOT EXECUTED**

This document is a non-executing plan. It contains no destructive automation
and must not be treated as authorization to run any command against live
AWS or Kubernetes resources. It exists to make the eventual Phase 5B2 live
retirement of the retired legacy deployment (`payments-ora-to-pg-001`,
`deploymentModel: legacyPair`) safe, auditable, and reversible within the
approved retention window.

Every identifier below was read directly from this repository (the values
files, Terraform, and workflow logic that actually govern the live
deployment) or from the confirmed-live-state facts supplied for Phase 5B1.
Where the repository does not contain enough information to state a fact
with confidence, it is explicitly marked **Unknown — discover live** rather
than guessed.

No credentials, secret values, or private key material appear in this
document, and none may ever be added to it.

---

## 1. Confirmed legacy resource identifiers

Source: `envs/dev/payments-ora-to-pg-001/values.yaml`, `.github/workflows/goldengate-eks-app.yaml`,
`envs/dev/iam.tf`, `envs/dev/policies/goldengate-secrets-read-dev/`, `envs/dev/dynamodb.tf`,
`envs/dev/goldengate-deployments.yaml`, and the Phase 5A/5B1 confirmed-live-state facts.

| Resource | Identifier | Source |
|---|---|---|
| Legacy Argo CD Application | `goldengate-payments-ora-to-pg-001` | `ARGOCD_APP_NAME="goldengate-${DEPLOYMENT_ID}"` in `goldengate-eks-app.yaml` (legacyPair branch), confirmed live |
| Legacy namespace | `gg-dev-payments-ora-to-pg-001` | same workflow, `TARGET_NAMESPACE="gg-${ENVIRONMENT}-${DEPLOYMENT_ID}"`; confirmed live |
| Legacy source StatefulSet/Service name | `ogg-oracle` (+ `ogg-oracle-headless` Service) | `source.name` in the legacy values file |
| Legacy target StatefulSet/Service name | `ogg-postgresql` (+ `ogg-postgresql-headless` Service) | `target.name` in the legacy values file |
| Legacy Ingress | one shared-mode ALB Ingress, hosts `ogg-oracle-payments-ora-to-pg-001.goldengate-dev.adcbmis.local` and `ogg-postgresql-payments-ora-to-pg-001.goldengate-dev.adcbmis.local`, group `gg-poc-dev-alb`, groupOrder `110` | `ingress:` block, legacy values file |
| Legacy ServiceAccount | `ogg-oracle-sa` (single ServiceAccount, shared by both the source and target StatefulSets — `target.serviceAccount.create: false` reuses the source's SA) | `source.serviceAccount`/`target.serviceAccount`, legacy values file |
| Legacy SecretProviderClass names | `ogg-oracle-admin`, `ogg-oracle-certificate`, `ogg-postgresql-admin`, `ogg-postgresql-certificate` | `csi.admin.providerClassName`/`csi.certificate.providerClassName`, legacy values file |
| Legacy CSI-synced K8s Secret names | `ogg-oracle-admin`, `ogg-postgresql-admin` | `csi.admin.secretName`, legacy values file |
| Legacy PVCs | `src-u02` (source `/u02`), `tgt-u02` (target `/u02`); `/u03` is `emptyDir` on both, not a PVC | `source.storage.u02.claimName` / `target.storage.u02.claimName`, legacy values file |
| Legacy StorageClass | `gg-efs-dev-payments-ora-to-pg-001`, `basePath: /payments-ora-to-pg-001`, `reclaimPolicy: Retain` | `persistence.efs.storageClass`, legacy values file |
| EFS filesystem | `fs-05cadf3570f23cd39` | `persistence.efs.fileSystemId`, legacy values file — **shared with canonical** (see §2) |
| Observer ECR repository (short name) | `goldengate-observer` (full: `229410149234.dkr.ecr.eu-west-1.amazonaws.com/goldengate-observer`) | Phase 5A dependency-graph findings (workflow's removed `ensure_observer_image` job); repository itself was never deleted |
| Observer image tag(s) currently in ECR | **Unknown — discover live** (`aws ecr describe-images --repository-name goldengate-observer`); Phase 5A used a content-addressed `obs-<12-hex>` tag scheme, but the exact tag(s) actually pushed/pulled by the still-running legacy pods must be read from the live registry, not assumed | N/A |
| Legacy DynamoDB partitions | `gg-payments-ora-to-pg-001-source`, `gg-payments-ora-to-pg-001-target` (per the task's confirmed live-state facts, matching the removed legacy-observer's `f"gg-{pipelineId}-{role}"` key pattern) | Phase 5A monitor-legacy-fallback removal work; **not** Terraform-managed (absent from `envs/dev/dynamodb.tf`'s `pipeline_config` `for_each`, which only seeds `gg-oracle-payments-01`/`gg-postgresql-payments-01`) |
| Legacy DynamoDB record types under those partitions | **Unknown — discover live** (`recordType` values actually written by the old observer: at minimum a `STATE#_deployment`-shaped item per the removed fallback code; exact set, including any `STATE#<process>` rows, must be read live with `GetItem`/`Query`, never `Scan`) | N/A |
| Secrets Manager objects the legacy pods read | `dev/goldengate/source/admin`, `dev/goldengate/target/admin`, `dev/goldengate/tls-certificate` | `csi.admin.objectName`/`csi.certificate.objectName`, legacy values file — **shared with canonical** (see §2) |
| IAM role used by legacy pods | `GoldenGateSecretsReadRole-dev` | `serviceAccountRoleArn`, legacy values file — **shared with canonical** (see §2); trust policy already scopes `ogg-oracle-sa` under `system:serviceaccount:gg-dev-*:*` |
| CloudWatch namespace historically published to by the observer | `GoldenGate/Pipelines` | Phase 5A dependency-graph findings; metric/dimension identity of any legacy-only datapoints is **Unknown — discover live** (CloudWatch does not tag datapoints by source pod) |
| ALB | shared group `gg-poc-dev-alb` (**shared with canonical**, see §2) | `ingress.alb.groupName`, legacy values file |
| ACM certificate | `arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7` (**shared with canonical**, see §2) | `ingress.alb.certificateArn`, legacy values file |
| Route 53 records | **Unknown — discover live.** No Route 53/hosted-zone resource exists anywhere in this Terraform root; the `*.goldengate-dev.adcbmis.local` internal DNS domain is not provisioned by this repository, so whether any record resolves specifically to the legacy Ingress/ALB must be confirmed against the actual DNS zone before assuming it is safe to remove | N/A |
| PVs bound to the legacy PVCs | **Unknown — discover live** (dynamically provisioned by the EFS CSI driver at bind time; the exact PV names/access-point IDs are not recorded anywhere in this repository and must be read with `kubectl get pv` / `describe pvc` before any deletion decision) | N/A |

## 2. Resources shared with the canonical deployments — never delete as part of legacy cleanup

These identifiers are identical between `envs/dev/payments-ora-to-pg-001/values.yaml` and the two
canonical values files (`envs/dev/gg-oracle-payments-01/values.yaml`,
`envs/dev/gg-postgresql-payments-01/values.yaml`), confirmed by direct comparison:

- Secrets Manager objects `dev/goldengate/source/admin`, `dev/goldengate/target/admin`, `dev/goldengate/tls-certificate`
- EFS filesystem `fs-05cadf3570f23cd39` (the filesystem itself — only the legacy StorageClass/access-points/directories under it are legacy-exclusive)
- IAM role `GoldenGateSecretsReadRole-dev`
- ALB group `gg-poc-dev-alb`
- ACM certificate `arn:aws:acm:eu-west-1:668311715351:certificate/9e53e28e-3243-47fc-85a1-50f9a94acde7`

Any cleanup step touching these must operate at the legacy-specific sub-resource level only
(e.g., the legacy StorageClass/access-point, the legacy Ingress host rules, the legacy IAM
*trust-policy subject*) and must never delete or reconfigure the shared resource itself.

**IAM sharing consequence (carried over from Phase 5B1):** `GoldenGateSecretsReadRole-dev`'s
DynamoDB (`AllowWriteGoldenGateMonitoringState`) and CloudWatch (`AllowPublishGoldenGateMonitoringMetrics`)
statements were removed in Phase 5B1 because canonical runtime pods never needed them. The legacy
`ogg-oracle-sa` ServiceAccount assumes this *same* role. If the legacy observer sidecar is still
running when the Phase 5B1 IAM change is actually applied (`terraform apply`, not performed in
this phase), it will lose DynamoDB write and CloudWatch publish access immediately — before any
of the steps in §4 below run. This is expected and consistent with the objective (the observer is
being retired), but must be captured as evidence *before* the IAM change is applied live, not
after, if a complete "last known good" legacy DynamoDB/CloudWatch snapshot is wanted.

## 3. Legacy cleanup inventory contract

Each item is tagged with exactly one category:

- **A. Delete after evidence capture** — safe to delete once inventory evidence (§1) has been captured, canonical health is reconfirmed, and owner approval (§5) is obtained.
- **B. Retain temporarily for rollback** — kept for the approved retention window after the Application/namespace are retired, then reassessed.
- **C. Retain permanently** — shared with canonical or otherwise never in scope for deletion.
- **D. Requires owner approval** — a business/data decision beyond engineering scope; must not proceed without explicit sign-off even after evidence capture.
- **E. Unknown — must be discovered during live inventory** — the repository does not contain enough information to classify yet.

| Resource | Category | Notes |
|---|---|---|
| Legacy Argo CD Application `goldengate-payments-ora-to-pg-001` | D | Deletion cascades to Application-managed resources (finalizer-driven); requires explicit approval before the destructive step in §4 |
| Legacy namespace `gg-dev-payments-ora-to-pg-001` | B | Only removed via the workflow's existing guarded namespace-ownership-label check (see `goldengate-eks-app.yaml`'s `delete_removed_argocd_applications` job) — never a bare `kubectl delete namespace` |
| Legacy StatefulSets `ogg-oracle`, `ogg-postgresql` | A | Cascade-deleted by Argo CD Application removal; evidence (full YAML) captured first |
| Legacy Services (`ogg-oracle`, `ogg-oracle-headless`, `ogg-postgresql`, `ogg-postgresql-headless`) | A | Cascade-deleted with the Application |
| Legacy Ingress (shared-mode, ALB group `gg-poc-dev-alb`) | D | Shares an ALB group with canonical traffic — confirm no live routing dependency (§4 step 5) before removing; ALB group/listener itself is Category C |
| Legacy ServiceAccount `ogg-oracle-sa` | A | Cascade-deleted with the namespace; not shared with canonical (canonical uses `gg-oracle-sa`/`gg-postgresql-sa`, created by `helm/goldengate-platform`, independent lifecycle) |
| Legacy SecretProviderClasses / synced K8s Secrets (`ogg-oracle-admin`, `ogg-oracle-certificate`, `ogg-postgresql-admin`, `ogg-postgresql-certificate`) | A | Namespace-scoped, cascade-deleted with the namespace; the underlying Secrets Manager *objects* they reference are Category C (shared) |
| Legacy PVCs `src-u02`, `tgt-u02` | B | `reclaimPolicy: Retain` means the underlying EFS access point/data survives PVC deletion by design — retain per approved rollback window before deciding final data disposition |
| PVs bound to the legacy PVCs | E | Names/IDs unknown until discovered live; disposition follows the PVC decision above (Retain policy applies at the PV level too) |
| Legacy StorageClass `gg-efs-dev-payments-ora-to-pg-001` | B | Only meaningful after its PVCs are gone; retain until the EFS access-point disposition (below) is finalized |
| EFS access points / directories under `basePath: /payments-ora-to-pg-001` | D | Contains historical trail/checkpoint data; a data-retention decision, not a pure infrastructure decision — requires owner approval before deletion regardless of retention window |
| EFS filesystem `fs-05cadf3570f23cd39` | C | Shared with canonical — never delete |
| Observer ECR repository `goldengate-observer` and its images | B | Retain through the approved rollback window in case the legacy observer image is needed for forensic/rollback purposes; do not delete in the same change as the Application retirement |
| Legacy DynamoDB partitions `gg-payments-ora-to-pg-001-source` / `-target` | B | Not Terraform-managed; retain through the rollback window, then delete only the specific items (`GetItem`/targeted `DeleteItem`, never `Scan`/table-level operations) |
| CloudWatch metrics/logs associated with the legacy observer | C | CloudWatch retains its own configured retention independently; no explicit deletion action is planned or in scope — metrics/log groups are not deleted as part of this cleanup |
| Route 53 records (if any) | E | Must be confirmed against the live DNS zone; not provisioned by this repository |
| ALB (`gg-poc-dev-alb`) and ACM certificate | C | Shared with canonical — never delete; only the legacy Ingress's host rules are removed |
| Secrets Manager objects (`dev/goldengate/source/admin`, `dev/goldengate/target/admin`, `dev/goldengate/tls-certificate`) | C | Shared with canonical — never delete |
| IAM role `GoldenGateSecretsReadRole-dev` | C | Shared with canonical — never delete the role; only its trust-policy subject list may eventually be narrowed once the legacy ServiceAccount no longer exists (a distinct, later, owner-approved change — not scoped to this runbook) |
| `envs/dev/payments-ora-to-pg-001/` (repository folder) | B | Retained until the live legacy environment is fully retired and validated (see §11 of the Phase 5B1 instructions); removal is a separate, later repository change |
| `source-statefulset.yaml` / `target-statefulset.yaml` / legacyPair chart rendering support | B | Same as above — retained until live retirement is complete |

## 4. Phase 5B2 pre-check contract

All of the following must be independently confirmed **before** any destructive step in §5 runs.
None of them are encoded as automatic gates in this phase — this is a documented checklist only.

1. Canonical Oracle Argo CD Application (`goldengate-dev-oracle-payments-01`) is `Synced` and `Healthy`.
2. Canonical PostgreSQL Argo CD Application (`goldengate-dev-postgresql-payments-01`) is `Synced` and `Healthy`.
3. Monitor Argo CD Application (`goldengate-monitor`) is `Synced` and `Healthy`.
4. Canonical Oracle StatefulSet is fully ready (desired == ready == current replicas).
5. Canonical PostgreSQL StatefulSet is fully ready.
6. Monitor Deployment is fully ready.
7. Canonical pods remain observer-free (exactly one application container each, no `goldengate-observer`/utility-sidecar/Fluent Bit container).
8. Canonical monitor records for both deployments report effective status `UP` and fresh (not `STALE`/`MISSING`).
9. Canonical leases are valid (held, not expired) for both deployments.
10. `CONFIG.metricsEnabled=true` for both canonical deployments.
11. `CONFIG.alertsEnabled=false` for both canonical deployments (alarms/SNS/gg-alerter remain out of scope).
12. `CLOUDWATCH_PUBLISH_ENABLED=true` on the live monitor Deployment.
13. No `cloudwatch_client_creation_failed`, `cloudwatch_put_metric_data_failed`, or `tick failed` events in recent monitor logs.
14. Canonical EFS PVCs (`gg-oracle-payments-01-u02`, `gg-postgresql-payments-01-u02`) are `Bound`.
15. The stale `ServiceManager.pid` init-container safeguard is present and unmodified in the rendered canonical StatefulSets.
16. The legacy Argo CD Application and namespace are confirmed still present immediately before retirement begins (i.e., nothing else removed them out-of-band since this runbook was written).
17. Evidence has been captured (full YAML, not summaries) for: legacy StatefulSets, pods, PVCs, PVs, Services, Ingress, ServiceAccounts, SecretProviderClasses, and the EFS/StorageClass/access-point relationship.
18. The approved rollback retention period (how long Category B items are kept before final deletion) has been confirmed with the owner and recorded alongside the captured evidence.
19. Explicit owner approval for the destructive retirement has been obtained and recorded (who approved, when, and for which specific resources — matching the Category D list in §3).

If any check fails, retirement must not proceed. No automatic remediation or deletion is triggered
by a failed check — a human decides the next step.

## 5. Recommended Phase 5B2 live retirement order

This is a recommended sequence only. It is not implemented as executable automation in this
phase, and nothing here may run without separately satisfying §4 in full.

1. Capture full legacy inventory and YAML evidence (StatefulSets, pods, Services, Ingress, ServiceAccounts, SecretProviderClasses, Secrets) for every resource listed in §1/§3.
2. Capture PVC, PV, StorageClass, and EFS access-point/directory relationships (including the actual PV names/access-point IDs, currently marked Unknown in §1).
3. Capture the observer image digest(s) currently in `goldengate-observer` and the full legacy DynamoDB record inventory under `gg-payments-ora-to-pg-001-source`/`-target` (`GetItem`/`Query` only, never `Scan`).
4. Confirm canonical runtimes and the shared monitor remain healthy (§4, items 1–15).
5. Confirm no current routing or traffic depends on the legacy Ingress host rules (the ALB group and ACM certificate are shared with canonical, so only the legacy-specific host rules are in scope for removal — never the ALB/listener/certificate itself).
6. Obtain and record explicit owner approval for the destructive retirement (§4, item 19), covering at minimum: the Argo CD Application, the namespace, and the eventual EFS access-point/directory data disposition.
7. Retire the legacy Argo CD Application through the repository's existing lifecycle/deletion safeguard (`goldengate-eks-app.yaml`'s `delete_removed_argocd_applications` job — i.e., make the deployment a genuine deletion candidate, for example via `lifecycle.state: absent`, not merely `deployment.enabled: false`, which this repository's classifier already treats as "retained, not deleted"). Do not invoke a bare `kubectl delete application`.
8. Before executing step 7, confirm what the old namespace-deletion behavior in that same job will actually do for this specific `deployment_model=legacyPair` candidate (it deletes the namespace only if it still exists **and** carries the exact expected ownership labels — verify this against the live namespace's labels first; do not assume).
9. Preserve PV/EFS data using the existing `reclaimPolicy: Retain` safeguard — confirm the PV(s) actually transition to `Released` (not deleted) after the PVCs are removed, and that the underlying EFS access point/directory still exists afterward.
10. Verify the legacy pods (`ogg-oracle-0`, `ogg-postgresql-0`) and their observer sidecars have actually terminated and are not recreated.
11. Revalidate canonical workloads and monitoring (re-run the full §4 checklist) to confirm the legacy retirement had no collateral effect.
12. Apply the runtime IAM reduction from Phase 5B1 (`terraform apply`, restricted to `goldengate_secrets_read_role_dev`'s policy) if it was not already applied earlier — see §2's IAM-sharing consequence for why applying this before vs. after legacy pod termination matters for evidence completeness.
13. Validate canonical pods still retrieve secrets and restart successfully after the IAM change (confirm no `AccessDenied` on Secrets Manager/KMS calls — those permissions are untouched, but this is a genuine live-verification step, not assumed).
14. Only after the approved retention window (§4, item 18) elapses and with owner approval already on record, remove the legacy DynamoDB records (targeted `DeleteItem` by exact key, never a table-level or `Scan`-driven operation) and the observer ECR images/repository.
15. Only after all external cleanup above is complete and verified, remove from this repository: `envs/dev/payments-ora-to-pg-001/`, legacyPair chart compatibility, and any source/target templates no longer required by any remaining deployment model. This is a separate, later repository change — not part of Phase 5B1 or the live retirement steps above.

**Explicit note:** deleting the Argo CD Application does not, by itself, guarantee the desired
namespace, PVC, PV, or EFS outcome. Step 8 and step 9 above exist specifically to verify the
actual observed behavior against the deletion job's real (already-existing) guard logic, not to
assume it.

## 6. Out of scope for this runbook

This runbook does not authorize, schedule, or trigger any of the actions it describes. Phase 5B2
(or a later, separately-scoped phase) is required to actually execute any step in §5, and each
step still requires its own explicit authorization at execution time regardless of what is written
here.
