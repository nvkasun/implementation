# Folder-driven GoldenGate deployment inventory (mirrors automation/goldengate-deployment-model.py); every consumer reads these locals, never the folder tree directly.

variable "environment" {
  description = "GoldenGate environment identifier for this root module; must match the envs/<environment> folder this file lives in."
  type        = string
  default     = "dev"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]*$", var.environment))
    error_message = "environment must be a safe lowercase token."
  }
}

locals {
  # Canonical descriptor shape: pipelines/<pipeline-id>/<deployment-id>/values.yaml, mirroring find_values_files(). Sibling files like argocd/values.yaml are structurally excluded.
  goldengate_runtime_value_files = sort(fileset(path.module, "pipelines/*/*/values.yaml"))

  goldengate_runtime_pipeline_folder_by_id = {
    for f in local.goldengate_runtime_value_files : split("/", f)[2] => split("/", f)[1]
  }

  goldengate_runtime_documents = {
    for f in local.goldengate_runtime_value_files :
    split("/", f)[2] => yamldecode(file("${path.module}/${f}"))
  }

  # jsonencode() roundtrip proves a literal Boolean: can(tobool("true")) is also true for the STRING "true".
  goldengate_enabled_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.deployment.enabled), "")
  }

  # A top-level `replication` key of any shape is a retired-schema tombstone -- key-presence based, mirrors _reject_replication_key_presence().
  goldengate_replication_key_present = {
    for id, doc in local.goldengate_runtime_documents : id => try(contains(keys(doc), "replication"), false)
  }

  # Shared platform invariants, derived and injected by the deploy workflow; declaring any of them at all is a forbidden override.
  goldengate_deployment_admin_secret_declared = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.deployment.adminSecret, null) != null
  }
  goldengate_service_account_declared = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.runtime.serviceAccount, null) != null
  }
  goldengate_csi_admin_object_name_declared = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.runtime.csi.admin.objectName, null) != null
  }
  goldengate_csi_certificate_object_name_declared = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.runtime.csi.certificate.objectName, null) != null
  }
  goldengate_csi_service_account_role_arn_declared = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.runtime.csi.serviceAccountRoleArn, null) != null
  }

  # EFS storage cardinality contract: one runtime deployment = one dedicated EFS filesystem, never one shared between source/target. Mirrors automation/goldengate-deployment-model.py's _parse_efs; never a second inventory implementation, only its Terraform-side precondition mirror.
  goldengate_persistence_declared = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.persistence, null) != null
  }
  goldengate_persistence_enabled_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.persistence.enabled), "")
  }
  goldengate_persistence_enabled_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.persistence.enabled, false) == true
  }
  goldengate_persistence_provider_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.persistence.provider, "")
  }
  # efs_enabled precondition: persistence.enabled=true AND persistence.provider="efs" -- the sole gate for reading persistence.efs.* at all, exactly mirroring the Python tool's efs_enabled check.
  goldengate_persistence_efs_declared = {
    for id, doc in local.goldengate_runtime_documents : id =>
    local.goldengate_persistence_enabled_raw[id] && local.goldengate_persistence_provider_raw[id] == "efs"
  }
  goldengate_persistence_efs_mode_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.persistence.efs.mode, "")
  }
  goldengate_persistence_efs_filesystem_id_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.persistence.efs.fileSystemId, "")
  }
  goldengate_persistence_efs_filesystem_id_declared = {
    for id, doc in local.goldengate_runtime_documents : id =>
    try(doc.persistence.efs.fileSystemId, null) != null && try(doc.persistence.efs.fileSystemId, "") != ""
  }
  goldengate_runtime_storage_u02_type_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.runtime.storage.u02.type, "")
  }

  # Four bounded id renames must not change the EFS creation_token (ForceNew) -- mirrors LEGACY_MANAGED_EFS_CREATION_TOKENS in goldengate-deployment-model.py. Never add an entry for a genuinely new runtime.
  goldengate_legacy_managed_efs_creation_tokens = {
    "gg-postgresql-repltest-001" = "dev-gg-postgresql-repltest-01-efs"
    "gg-mssql-repltest-001"      = "dev-gg-mssql-repltest-01-efs"
    "gg-oracle-repltest-002"     = "dev-gg-oracle-repltest-01-efs"
    "gg-postgresql-repltest-002" = "dev-gg-postgresql-repltest-02-efs"
  }

  # Keyed by every folder-driven document, not just enabled ones -- this must never disappear from this map merely because a deployment is temporarily disabled. Deletion is guarded upstream by managed_efs_deletion_guard.
  goldengate_managed_efs_deployments = {
    for id, doc in local.goldengate_runtime_documents : id => {
      creation_token = lookup(local.goldengate_legacy_managed_efs_creation_tokens, id, "${var.environment}-${id}-efs")
    }
    if local.goldengate_persistence_efs_declared[id] && local.goldengate_persistence_efs_mode_raw[id] == "managed"
  }

  # No filtering here; terraform_data.goldengate_runtime_contract enforces the full contract as a plan-blocking precondition.
  goldengate_runtime_candidates = local.goldengate_runtime_documents

  # deployment.enabled is the sole runtime-presence control; lifecycle.state is retired and rejected by the contract precondition below.
  goldengate_enabled_deployments = {
    for id, doc in local.goldengate_runtime_candidates : id => doc
    if try(doc.deployment.enabled, false) == true
  }

  goldengate_deployment_names = sort(keys(local.goldengate_enabled_deployments))

  goldengate_pipeline_names = sort(distinct([
    for id in local.goldengate_deployment_names : try(local.goldengate_enabled_deployments[id].deployment.pipeline, "")
  ]))

  goldengate_alb_group_order_by_enabled_id = {
    for id in local.goldengate_deployment_names : id => try(local.goldengate_enabled_deployments[id].ingress.alb.groupOrder, null)
  }
  goldengate_duplicate_alb_group_order_ids = toset([
    for id in local.goldengate_deployment_names : id
    if local.goldengate_alb_group_order_by_enabled_id[id] != null
    && length([
      for other_id in local.goldengate_deployment_names : other_id
      if local.goldengate_alb_group_order_by_enabled_id[other_id] == local.goldengate_alb_group_order_by_enabled_id[id]
    ]) > 1
  ])

  goldengate_tls_secret_name = local.gg_env_tls_secret_name

  # One shared IRSA subject for every deploymentType -- onboarding a new engine never needs an IAM trust-policy edit. Mirrors resolve_runtime_service_account().
  goldengate_canonical_runtime_trust_subject = "system:serviceaccount:${local.goldengate_shared_environment.runtimeNamespace}:gg-runtime-sa"

  goldengate_secrets_trust_policy = jsondecode(file("${path.module}/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json"))
  # OIDC condition-map key follows the live-discovered cluster issuer -- never a hardcoded literal, so a recreated cluster is picked up automatically.
  goldengate_secrets_trust_subjects = local.goldengate_secrets_trust_policy.Statement[0].Condition.StringLike[
    "${local.gg_env_oidc_hostpath}:sub"
  ]

  goldengate_platform_values = yamldecode(file("${path.module}/../../platform/${var.environment}/goldengate-platform/values.yaml"))
  goldengate_monitor_values  = yamldecode(file("${path.module}/goldengate-monitor/values.yaml"))
  goldengate_monitor_host    = try(local.goldengate_monitor_values.ingress.host, "")

  goldengate_shared_environment = {
    environment         = var.environment
    runtimeNamespace    = try(local.goldengate_platform_values.namespaces.runtime.name, local.gg_env_namespaces.runtime)
    monitoringNamespace = try(local.goldengate_platform_values.fluentBit.namespaces.monitoring, local.gg_env_namespaces.monitoring)
    dnsDomain           = local.goldengate_monitor_host != "" ? trimprefix(local.goldengate_monitor_host, "monitor.") : local.gg_env_dns_domain
    tlsSecret           = local.goldengate_tls_secret_name
  }
}

# terraform_data + lifecycle.precondition blocks `terraform plan` itself; `check` blocks are diagnostic-only. No external data sources or provisioners are used.
resource "terraform_data" "goldengate_runtime_contract" {
  for_each = local.goldengate_runtime_documents

  input = each.key

  lifecycle {
    precondition {
      condition     = try(each.value.deploymentModel, null) == "singleRuntime"
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: deploymentModel must be exactly \"singleRuntime\"."
    }
    precondition {
      condition     = local.goldengate_enabled_jsonenc[each.key] == "true" || local.goldengate_enabled_jsonenc[each.key] == "false"
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: deployment.enabled must be a literal Boolean, not a Boolean-like string."
    }
    precondition {
      condition     = try(each.value.deployment.pipeline, "") != "" && can(regex("^[a-z][a-z0-9-]*$", each.value.deployment.pipeline))
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: deployment.pipeline must be a safe non-empty identifier."
    }
    precondition {
      # Pipeline-Aware Descriptor Hierarchy: mirrors automation/goldengate-deployment-model.py's own parse_descriptor() pipeline-folder-equality check (contract B) exactly.
      condition     = try(each.value.deployment.pipeline, "") == lookup(local.goldengate_runtime_pipeline_folder_by_id, each.key, "")
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: the parent pipeline folder must exactly equal deployment.pipeline."
    }
    precondition {
      # Pipeline-Aware Descriptor Hierarchy (contract E): mirrors automation/goldengate-deployment-model.py's own _three_digit_suffix() check on deployment.pipeline.
      condition     = can(regex("-[0-9]{3}$", try(each.value.deployment.pipeline, "")))
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: deployment.pipeline must end in a 3-digit pipeline correlation suffix (-NNN)."
    }
    precondition {
      # Pipeline-Aware Descriptor Hierarchy (contract F): mirrors automation/goldengate-deployment-model.py's own _three_digit_suffix() check on the deployment ID.
      condition     = can(regex("-[0-9]{3}$", each.key))
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: the deployment ID (folder name) must end in a 3-digit pipeline correlation suffix (-NNN)."
    }
    precondition {
      # deployment.pipeline's 3-digit suffix must equal the deployment id's suffix; short-circuits when either is malformed (handled above).
      condition = (
        !can(regex("-[0-9]{3}$", each.key))
        || !can(regex("-[0-9]{3}$", try(each.value.deployment.pipeline, "")))
        || substr(each.key, -3, 3) == substr(try(each.value.deployment.pipeline, ""), -3, 3)
      )
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: deployment.pipeline's 3-digit correlation suffix must exactly equal the deployment ID's 3-digit suffix."
    }
    precondition {
      condition     = contains(["source", "target"], try(each.value.deployment.role, ""))
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: deployment.role must be exactly \"source\" or \"target\"."
    }
    precondition {
      # Fresh-EKS Phase A/Phase 9: global.environment is shared environment configuration (envs/dev/environment.yaml), no longer descriptor input -- forbid its reintroduction rather than requiring/validating a descriptor-owned copy.
      condition     = try(each.value.global.environment, null) == null
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: global.environment is a forbidden override -- it is shared environment configuration, injected by the deploy workflow from envs/dev/environment.yaml, and must not be set in a runtime descriptor."
    }
    precondition {
      condition = (
        try(each.value.runtime.deploymentType, "") != ""
        && length(try(each.value.runtime.deploymentType, "")) <= 32
        && can(regex("^[a-z][a-z0-9]*(-[a-z0-9]+)*$", each.value.runtime.deploymentType))
      )
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.deploymentType must be a safe lowercase token (letters/digits only, internal hyphens only, no leading/trailing hyphen, max 32 characters)."
    }
    precondition {
      # Fresh-EKS Phase A/Phase 9: the descriptor owns only the environment-neutral repositoryName; the full private-ECR repository (the canonical environment configuration's ECR registry + <repositoryName>) is derived once by the workflow/deployment model, never descriptor input.
      condition = (
        try(each.value.runtime.image.repositoryName, "") != ""
        && can(regex("^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$", each.value.runtime.image.repositoryName))
      )
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.image.repositoryName must be a non-empty, safe, environment-neutral ECR repository name (no registry host, tag, digest, whitespace, or traversal)."
    }
    precondition {
      condition     = try(each.value.runtime.image.repository, null) == null
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.image.repository is a forbidden override -- it is shared environment identity, derived from the canonical environment configuration's ECR registry + runtime.image.repositoryName, and must not be set in a runtime descriptor."
    }
    precondition {
      condition     = try(each.value.runtime.image.tag, "") != "" && try(each.value.runtime.image.tag, "latest") != "latest"
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.image.tag must be explicit and must not be \"latest\"."
    }
    precondition {
      condition     = !local.goldengate_service_account_declared[each.key]
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.serviceAccount is a forbidden override -- every singleRuntime deployment shares the platform-owned gg-runtime-sa identity regardless of runtime.deploymentType."
    }
    precondition {
      condition     = !local.goldengate_deployment_admin_secret_declared[each.key]
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: deployment.adminSecret is a forbidden override -- the admin secret is derived solely from deployment.role."
    }
    precondition {
      condition     = !local.goldengate_csi_admin_object_name_declared[each.key]
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.csi.admin.objectName is a forbidden override -- it is derived solely from deployment.role."
    }
    precondition {
      condition     = !local.goldengate_csi_certificate_object_name_declared[each.key]
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.csi.certificate.objectName is a forbidden override -- it is a shared platform invariant."
    }
    precondition {
      condition     = !local.goldengate_csi_service_account_role_arn_declared[each.key]
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.csi.serviceAccountRoleArn is a forbidden override -- it is a shared platform invariant."
    }
    precondition {
      # lifecycle.state is retired; deployment.enabled is the sole presence control. Key-presence based -- `lifecycle: null` is still rejected.
      condition     = !try(contains(keys(each.value), "lifecycle"), false)
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: lifecycle.state is no longer supported for runtime presence; use deployment.enabled only."
    }
    precondition {
      # A root-level `enabled:` key (outside deployment.enabled) is rejected -- never fires for nested enabled fields like ingress.enabled.
      condition     = !try(contains(keys(each.value), "enabled"), false)
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: root-level enabled is no longer supported; use deployment.enabled only."
    }
    precondition {
      # runtime.enabled (a second presence switch) is rejected; nested flags like runtime.csi.enabled are untouched.
      condition     = !try(contains(keys(each.value.runtime), "enabled"), false)
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.enabled is no longer supported as a runtime presence control; use deployment.enabled only."
    }
    precondition {
      # A top-level `replication` key of any shape is rejected -- replication is configured manually through the GoldenGate UI.
      condition     = !local.goldengate_replication_key_present[each.key]
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: unsupported descriptor key: top-level replication automation has been retired; configure database connections and replication processes manually through the GoldenGate UI."
    }
    precondition {
      # ingress.hostDomain/alb fields are shared environment config, injected by the workflow -- forbidden in descriptors. groupOrder stays required.
      condition     = try(each.value.ingress.hostDomain, null) == null
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: ingress.hostDomain is a forbidden override -- it is shared environment configuration and must not be set in a runtime descriptor."
    }
    precondition {
      condition     = try(each.value.ingress.alb.groupName, null) == null
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: ingress.alb.groupName is a forbidden override -- it is shared environment configuration and must not be set in a runtime descriptor."
    }
    precondition {
      condition     = try(each.value.ingress.alb.certificateArn, null) == null
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: ingress.alb.certificateArn is a forbidden override -- it is shared environment configuration and must not be set in a runtime descriptor."
    }
    precondition {
      condition     = !contains(local.goldengate_duplicate_alb_group_order_ids, each.key)
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: ingress.alb.groupOrder duplicates another enabled runtime's ALB group order."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_declared[each.key]
        || local.goldengate_persistence_enabled_jsonenc[each.key] == "true"
        || local.goldengate_persistence_enabled_jsonenc[each.key] == "false"
      )
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: persistence.enabled must be a literal Boolean, not a Boolean-like string, when persistence is present."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || local.goldengate_runtime_storage_u02_type_raw[each.key] == "efs"
      )
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: runtime.storage.u02.type must be \"efs\" when persistence.enabled=true and persistence.provider=efs."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || contains(["managed", "existing"], local.goldengate_persistence_efs_mode_raw[each.key])
      )
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: persistence.efs.mode must be explicitly \"managed\" or \"existing\" when persistence.enabled=true and persistence.provider=efs."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || local.goldengate_persistence_efs_mode_raw[each.key] != "existing"
        || can(regex("^fs-[0-9a-f]+$", local.goldengate_persistence_efs_filesystem_id_raw[each.key]))
      )
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: persistence.efs.fileSystemId is not a safe EFS filesystem ID (required when persistence.efs.mode=existing)."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || local.goldengate_persistence_efs_mode_raw[each.key] != "managed"
        || !local.goldengate_persistence_efs_filesystem_id_declared[each.key]
      )
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: persistence.efs.fileSystemId must not be set when persistence.efs.mode=managed -- Terraform provisions and resolves it."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || local.goldengate_persistence_efs_mode_raw[each.key] != "managed"
        || length("${var.environment}-${each.key}-efs") <= 64
      )
      error_message = "envs/${var.environment}/pipelines/.../${each.key}/values.yaml: derived EFS creation token \"${var.environment}-${each.key}-efs\" exceeds the 64-character AWS EFS creation-token limit."
    }
  }
}

# Aggregate, plan-blocking cross-pipeline rules; check blocks below are diagnostic-only and never block plan/apply on their own.
resource "terraform_data" "goldengate_cross_pipeline_contract" {
  input = "goldengate-cross-pipeline-contract"

  lifecycle {
    precondition {
      condition = alltrue([
        for pipeline in local.goldengate_pipeline_names : length([
          for id in local.goldengate_deployment_names : id
          if try(local.goldengate_enabled_deployments[id].deployment.pipeline, "") == pipeline
          && try(local.goldengate_enabled_deployments[id].deployment.role, "") == "source"
        ]) <= 1
      ])
      error_message = "A GoldenGate pipeline has more than one enabled source deployment."
    }
    precondition {
      condition = alltrue([
        for pipeline in local.goldengate_pipeline_names : length([
          for id in local.goldengate_deployment_names : id
          if try(local.goldengate_enabled_deployments[id].deployment.pipeline, "") == pipeline
          && try(local.goldengate_enabled_deployments[id].deployment.role, "") == "target"
        ]) <= 1
      ])
      error_message = "A GoldenGate pipeline has more than one enabled target deployment."
    }
    precondition {
      condition = alltrue([
        for id in local.goldengate_deployment_names : !contains(local.goldengate_duplicate_alb_group_order_ids, id)
      ])
      error_message = "Two or more enabled GoldenGate deployments share the same ALB group order."
    }
    precondition {
      # Trust subject must be exactly the canonical gg-runtime-sa identity -- no wildcard, no per-engine entry. try() guards against an empty-list index crash.
      condition = (
        length(local.goldengate_secrets_trust_subjects) == 1
        && try(local.goldengate_secrets_trust_subjects[0], "") == local.goldengate_canonical_runtime_trust_subject
      )
      error_message = "envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json trust subjects must be exactly one entry: the canonical system:serviceaccount:<namespace>:gg-runtime-sa subject. No wildcard, no per-engine subject, no migration-compatibility entry. Onboarding a new deploymentType must never require editing this file."
    }
  }
}

check "goldengate_candidate_count_matches_value_file_count" {
  assert {
    condition     = length(local.goldengate_runtime_candidates) == length(local.goldengate_runtime_value_files)
    error_message = "The number of structurally valid GoldenGate runtime candidates does not equal the number of non-ignored runtime value files."
  }
}

check "goldengate_deployment_ids_unique" {
  assert {
    condition     = length(local.goldengate_deployment_names) == length(distinct(local.goldengate_deployment_names))
    error_message = "Duplicate GoldenGate deployment IDs discovered across envs/dev/*/values.yaml."
  }
}

check "goldengate_at_most_one_source_per_pipeline" {
  assert {
    condition = alltrue([
      for pipeline in local.goldengate_pipeline_names : length([
        for id in local.goldengate_deployment_names : id
        if try(local.goldengate_enabled_deployments[id].deployment.pipeline, "") == pipeline
        && try(local.goldengate_enabled_deployments[id].deployment.role, "") == "source"
      ]) <= 1
    ])
    error_message = "A GoldenGate pipeline has more than one enabled source deployment."
  }
}

check "goldengate_at_most_one_target_per_pipeline" {
  assert {
    condition = alltrue([
      for pipeline in local.goldengate_pipeline_names : length([
        for id in local.goldengate_deployment_names : id
        if try(local.goldengate_enabled_deployments[id].deployment.pipeline, "") == pipeline
        && try(local.goldengate_enabled_deployments[id].deployment.role, "") == "target"
      ]) <= 1
    ])
    error_message = "A GoldenGate pipeline has more than one enabled target deployment."
  }
}

# The full repository is always derived from ECR registry + repositoryName -- this just checks every enabled deployment supplies a non-empty repositoryName.
check "goldengate_image_repository_name_present" {
  assert {
    condition = alltrue([
      for id in local.goldengate_deployment_names :
      try(local.goldengate_enabled_deployments[id].runtime.image.repositoryName, "") != ""
    ])
    error_message = "An enabled GoldenGate deployment is missing runtime.image.repositoryName."
  }
}

check "goldengate_managed_efs_creation_tokens_unique" {
  assert {
    condition = length(local.goldengate_managed_efs_deployments) == length(distinct([
      for id, v in local.goldengate_managed_efs_deployments : v.creation_token
    ]))
    error_message = "Two GoldenGate managed-EFS deployments derive the same EFS creation token -- storage identities must never collide."
  }
}
