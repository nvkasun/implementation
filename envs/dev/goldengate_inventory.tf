# Folder-driven GoldenGate deployment inventory (mirrors hack/goldengate-deployment-model.py); every consumer reads these locals, never the folder tree directly.

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
  goldengate_ignored_runtime_folders = ["argocd", "goldengate-monitor"]

  goldengate_runtime_value_files = sort([
    for f in fileset(path.module, "*/values.yaml") : f
    if !contains(local.goldengate_ignored_runtime_folders, split("/", f)[0])
  ])

  goldengate_runtime_documents = {
    for f in local.goldengate_runtime_value_files :
    split("/", f)[0] => yamldecode(file("${path.module}/${f}"))
  }

  # jsonencode() roundtrip proves a literal Boolean: can(tobool("true")) is also true for the STRING "true".
  goldengate_enabled_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.deployment.enabled), "")
  }
  goldengate_replication_declared = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.replication, null) != null
  }
  goldengate_replication_enabled_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.replication.enabled), "")
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

  goldengate_deployment_type_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.runtime.deploymentType, "")
  }

  # Phase 6D1 replication contract: structural gates only, mirroring hack/goldengate-deployment-model.py; never REST reconciliation logic.
  goldengate_replication_enabled_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.enabled, false) == true
  }
  goldengate_replication_role_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.deployment.role, "")
  }
  goldengate_replication_extract_enabled_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.replication.extract.enabled), "")
  }
  goldengate_replication_distribution_enabled_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.replication.distribution.enabled), "")
  }
  goldengate_replication_checkpoint_enabled_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.replication.checkpoint.enabled), "")
  }
  goldengate_replication_replicat_enabled_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.replication.replicat.enabled), "")
  }
  goldengate_replication_extract_start_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.replication.extract.startOnCreate), "")
  }
  goldengate_replication_distribution_start_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.replication.distribution.startOnCreate), "")
  }
  goldengate_replication_replicat_start_jsonenc = {
    for id, doc in local.goldengate_runtime_documents : id => try(jsonencode(doc.replication.replicat.startOnCreate), "")
  }
  goldengate_replication_extract_enabled_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.extract.enabled, false) == true
  }
  goldengate_replication_distribution_enabled_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.distribution.enabled, false) == true
  }
  goldengate_replication_checkpoint_enabled_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.checkpoint.enabled, false) == true
  }
  goldengate_replication_replicat_enabled_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.replicat.enabled, false) == true
  }
  goldengate_replication_extract_name_raw           = { for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.extract.name, "") }
  goldengate_replication_extract_trail_raw          = { for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.extract.trail.name, "") }
  goldengate_replication_replicat_name_raw          = { for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.replicat.name, "") }
  goldengate_replication_replicat_trail_raw         = { for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.replicat.sourceTrailName, "") }
  goldengate_replication_distribution_path_raw      = { for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.distribution.pathName, "") }
  goldengate_replication_distribution_src_trail_raw = { for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.distribution.sourceTrailName, "") }
  goldengate_replication_distribution_tgt_trail_raw = { for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.distribution.targetTrailName, "") }
  goldengate_replication_distribution_target_raw    = { for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.distribution.targetDeployment, "") }
  goldengate_replication_checkpoint_table_raw       = { for id, doc in local.goldengate_runtime_documents : id => try(doc.replication.checkpoint.table, "") }

  # EFS storage cardinality contract: one runtime deployment = one dedicated EFS filesystem, never one shared between source/target. Mirrors hack/goldengate-deployment-model.py's _parse_efs; never a second inventory implementation, only its Terraform-side precondition mirror.
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

  # Keyed by every folder-driven document (not just goldengate_enabled_deployments): storage follows the runtime's Git folder, never deployment.enabled/lifecycle.state. A managed EFS module instance must never disappear from this map merely because a deployment is temporarily disabled -- only physical deletion of the values.yaml file removes an entry here, and that case is guarded upstream by the workflow's managed_efs_deletion_guard job, which must run before any Terraform apply can observe the removal.
  goldengate_managed_efs_deployments = {
    for id, doc in local.goldengate_runtime_documents : id => {
      creation_token = "${var.environment}-${id}-efs"
    }
    if local.goldengate_persistence_efs_declared[id] && local.goldengate_persistence_efs_mode_raw[id] == "managed"
  }

  goldengate_existing_efs_deployments = {
    for id, doc in local.goldengate_runtime_documents : id => {
      filesystem_id = local.goldengate_persistence_efs_filesystem_id_raw[id]
    }
    if local.goldengate_persistence_efs_declared[id] && local.goldengate_persistence_efs_mode_raw[id] == "existing"
  }

  # No filtering here; terraform_data.goldengate_runtime_contract enforces the full contract as a plan-blocking precondition.
  goldengate_runtime_candidates = local.goldengate_runtime_documents

  # try()-guarded so an invalid document fails via the precondition below, not a secondary "unsupported attribute" error.
  goldengate_enabled_deployments = {
    for id, doc in local.goldengate_runtime_candidates : id => doc
    if try(doc.deployment.enabled, false) == true
    && try(doc.lifecycle.state, "active") != "absent"
  }

  goldengate_deployment_names = sort(keys(local.goldengate_enabled_deployments))

  goldengate_pipeline_names = sort(distinct([
    for id in local.goldengate_deployment_names : try(local.goldengate_enabled_deployments[id].deployment.pipeline, "")
  ]))

  goldengate_replication_pipeline_members = {
    for pipeline in local.goldengate_pipeline_names : pipeline => {
      source_id = try([
        for id in local.goldengate_deployment_names : id
        if try(local.goldengate_enabled_deployments[id].deployment.pipeline, "") == pipeline
        && try(local.goldengate_enabled_deployments[id].deployment.role, "") == "source"
      ][0], "")
      target_id = try([
        for id in local.goldengate_deployment_names : id
        if try(local.goldengate_enabled_deployments[id].deployment.pipeline, "") == pipeline
        && try(local.goldengate_enabled_deployments[id].deployment.role, "") == "target"
      ][0], "")
    }
  }

  # Well-formed replication pipelines only: exactly one source and one target, both enabled+replicating; safe to index by source_id/target_id below.
  goldengate_replication_pipelines_enabled = [
    for pipeline in local.goldengate_pipeline_names : pipeline
    if(
      try(local.goldengate_replication_enabled_raw[local.goldengate_replication_pipeline_members[pipeline].source_id], false)
      && try(local.goldengate_replication_enabled_raw[local.goldengate_replication_pipeline_members[pipeline].target_id], false)
    )
  ]

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

  # The sole admin-secret derivation rule: deployment.role alone selects the shared environment-level secret.
  goldengate_admin_secret_names = {
    for id in local.goldengate_deployment_names : id =>
    try(local.goldengate_enabled_deployments[id].deployment.role, "") == "source"
    ? local.gg_env_source_admin_secret_name
    : local.gg_env_target_admin_secret_name
  }

  goldengate_tls_secret_name = local.gg_env_tls_secret_name

  # Restored shared runtime identity: every singleRuntime deployment resolves the SAME platform-owned ServiceAccount regardless of deploymentType -- deploymentType controls image/product/ports/replication semantics, never AWS runtime identity. Mirrors hack/goldengate-deployment-model.py's resolve_runtime_service_account().
  goldengate_runtime_service_account_names = {
    for id in local.goldengate_deployment_names : id => "gg-runtime-sa"
  }

  # Unique enabled deployment types, sorted deterministically; mirrors hack/goldengate-deployment-model.py's runtime-identities command. No longer drives runtime AWS identity (see goldengate_canonical_runtime_trust_subject below) -- retained as the folder-driven type inventory consumed by DynamoDB CONFIG/dashboard generation.
  goldengate_enabled_deployment_types = sort(distinct([
    for id in local.goldengate_deployment_names : local.goldengate_deployment_type_raw[id]
  ]))

  # Descriptive per-type mirror only, since every type now shares one runtime ServiceAccount; never consumed for IRSA trust.
  goldengate_runtime_identity_inventory = {
    for t in local.goldengate_enabled_deployment_types : t => "gg-runtime-sa"
  }

  # The permanent, stable self-service runtime identity: every singleRuntime deployment of every deploymentType (including future ones) shares this ONE IRSA subject, so onboarding a new engine never requires an IAM trust-policy edit. Never derived from the folder inventory -- it is a platform invariant, not a per-type/per-count value. This is a fresh EKS cluster: there is no migration-compatibility trust to preserve, so this is the ONLY approved runtime trust subject.
  goldengate_canonical_runtime_trust_subject = "system:serviceaccount:${local.goldengate_shared_environment.runtimeNamespace}:gg-runtime-sa"

  goldengate_secrets_trust_policy = jsondecode(file("${path.module}/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json"))
  # The OIDC condition-map key is derived from the live-discovered cluster issuer (envs/dev/environment.tf), never a hardcoded destroyed- or new-cluster literal -- a recreated EKS cluster gets a different issuer, and this lookup must follow it automatically.
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
      error_message = "envs/${var.environment}/${each.key}/values.yaml: deploymentModel must be exactly \"singleRuntime\"."
    }
    precondition {
      condition     = local.goldengate_enabled_jsonenc[each.key] == "true" || local.goldengate_enabled_jsonenc[each.key] == "false"
      error_message = "envs/${var.environment}/${each.key}/values.yaml: deployment.enabled must be a literal Boolean, not a Boolean-like string."
    }
    precondition {
      condition     = try(each.value.deployment.pipeline, "") != "" && can(regex("^[a-z][a-z0-9-]*$", each.value.deployment.pipeline))
      error_message = "envs/${var.environment}/${each.key}/values.yaml: deployment.pipeline must be a safe non-empty identifier."
    }
    precondition {
      condition     = contains(["source", "target"], try(each.value.deployment.role, ""))
      error_message = "envs/${var.environment}/${each.key}/values.yaml: deployment.role must be exactly \"source\" or \"target\"."
    }
    precondition {
      # Fresh-EKS Phase A/Phase 9: global.environment is shared environment configuration (envs/dev/environment.yaml), no longer descriptor input -- forbid its reintroduction rather than requiring/validating a descriptor-owned copy.
      condition     = try(each.value.global.environment, null) == null
      error_message = "envs/${var.environment}/${each.key}/values.yaml: global.environment is a forbidden override -- it is shared environment configuration, injected by the deploy workflow from envs/dev/environment.yaml, and must not be set in a runtime descriptor."
    }
    precondition {
      condition = (
        try(each.value.runtime.deploymentType, "") != ""
        && length(try(each.value.runtime.deploymentType, "")) <= 32
        && can(regex("^[a-z][a-z0-9]*(-[a-z0-9]+)*$", each.value.runtime.deploymentType))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.deploymentType must be a safe lowercase token (letters/digits only, internal hyphens only, no leading/trailing hyphen, max 32 characters)."
    }
    precondition {
      # Fresh-EKS Phase A/Phase 9: the descriptor owns only the environment-neutral repositoryName; the full private-ECR repository (local.gg_env_ecr_registry/<repositoryName>) is derived once by the workflow/deployment model, never descriptor input.
      condition = (
        try(each.value.runtime.image.repositoryName, "") != ""
        && can(regex("^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$", each.value.runtime.image.repositoryName))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.image.repositoryName must be a non-empty, safe, environment-neutral ECR repository name (no registry host, tag, digest, whitespace, or traversal)."
    }
    precondition {
      condition     = try(each.value.runtime.image.repository, null) == null
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.image.repository is a forbidden override -- it is shared environment identity, derived from local.gg_env_ecr_registry + runtime.image.repositoryName, and must not be set in a runtime descriptor."
    }
    precondition {
      condition     = try(each.value.runtime.image.tag, "") != "" && try(each.value.runtime.image.tag, "latest") != "latest"
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.image.tag must be explicit and must not be \"latest\"."
    }
    precondition {
      condition     = !local.goldengate_service_account_declared[each.key]
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.serviceAccount is a forbidden override -- every singleRuntime deployment shares the platform-owned gg-runtime-sa identity regardless of runtime.deploymentType."
    }
    precondition {
      condition     = !local.goldengate_deployment_admin_secret_declared[each.key]
      error_message = "envs/${var.environment}/${each.key}/values.yaml: deployment.adminSecret is a forbidden override -- the admin secret is derived solely from deployment.role."
    }
    precondition {
      condition     = !local.goldengate_csi_admin_object_name_declared[each.key]
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.csi.admin.objectName is a forbidden override -- it is derived solely from deployment.role."
    }
    precondition {
      condition     = !local.goldengate_csi_certificate_object_name_declared[each.key]
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.csi.certificate.objectName is a forbidden override -- it is a shared platform invariant."
    }
    precondition {
      condition     = !local.goldengate_csi_service_account_role_arn_declared[each.key]
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.csi.serviceAccountRoleArn is a forbidden override -- it is a shared platform invariant."
    }
    precondition {
      condition     = try(each.value.lifecycle, null) == null || contains(["active", "absent"], try(each.value.lifecycle.state, "active"))
      error_message = "envs/${var.environment}/${each.key}/values.yaml: lifecycle.state must be \"active\" or \"absent\" when lifecycle is present."
    }
    precondition {
      condition = (
        !local.goldengate_replication_declared[each.key]
        || local.goldengate_replication_enabled_jsonenc[each.key] == "true"
        || local.goldengate_replication_enabled_jsonenc[each.key] == "false"
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.enabled must be a literal Boolean, not a Boolean-like string, when replication is present."
    }
    precondition {
      condition = (
        !local.goldengate_replication_enabled_raw[each.key]
        || contains(["source", "target"], local.goldengate_replication_role_raw[each.key])
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.enabled=true requires deployment.role to be exactly \"source\" or \"target\"."
    }
    precondition {
      condition = (
        !local.goldengate_replication_enabled_raw[each.key]
        || (
          local.goldengate_replication_role_raw[each.key] == "source"
          ? local.goldengate_deployment_type_raw[each.key] == "postgresql"
          : local.goldengate_deployment_type_raw[each.key] == "mssql"
        )
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.enabled=true is only supported for a postgresql source paired with an mssql target."
    }
    precondition {
      condition = (
        !local.goldengate_replication_enabled_raw[each.key]
        || local.goldengate_replication_role_raw[each.key] != "source"
        || (
          local.goldengate_replication_extract_enabled_jsonenc[each.key] == "true"
          && local.goldengate_replication_distribution_enabled_jsonenc[each.key] == "true"
          && local.goldengate_replication_checkpoint_enabled_jsonenc[each.key] == "false"
          && local.goldengate_replication_replicat_enabled_jsonenc[each.key] == "false"
        )
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: a replication-enabled source must have extract.enabled=true, distribution.enabled=true, checkpoint.enabled=false, replicat.enabled=false, all as literal Booleans."
    }
    precondition {
      condition = (
        !local.goldengate_replication_enabled_raw[each.key]
        || local.goldengate_replication_role_raw[each.key] != "target"
        || (
          local.goldengate_replication_extract_enabled_jsonenc[each.key] == "false"
          && local.goldengate_replication_distribution_enabled_jsonenc[each.key] == "false"
          && local.goldengate_replication_checkpoint_enabled_jsonenc[each.key] == "true"
          && local.goldengate_replication_replicat_enabled_jsonenc[each.key] == "true"
        )
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: a replication-enabled target must have extract.enabled=false, distribution.enabled=false, checkpoint.enabled=true, replicat.enabled=true, all as literal Booleans."
    }
    precondition {
      condition = (
        !local.goldengate_replication_extract_enabled_raw[each.key]
        || can(regex("^[A-Z][A-Z0-9_$]{0,7}$", local.goldengate_replication_extract_name_raw[each.key]))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.extract.name must be a valid Extract name (uppercase, max 8 characters)."
    }
    precondition {
      condition = (
        !local.goldengate_replication_extract_enabled_raw[each.key]
        || can(regex("^[a-z][a-z0-9]$", local.goldengate_replication_extract_trail_raw[each.key]))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.extract.trail.name must be a valid two-character lowercase trail name."
    }
    precondition {
      condition = (
        !local.goldengate_replication_replicat_enabled_raw[each.key]
        || can(regex("^[A-Z][A-Z0-9_$]{0,7}$", local.goldengate_replication_replicat_name_raw[each.key]))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.replicat.name must be a valid Replicat name (uppercase, max 8 characters)."
    }
    precondition {
      condition = (
        !local.goldengate_replication_replicat_enabled_raw[each.key]
        || can(regex("^[a-z][a-z0-9]$", local.goldengate_replication_replicat_trail_raw[each.key]))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.replicat.sourceTrailName must be a valid two-character lowercase trail name."
    }
    precondition {
      condition = (
        !local.goldengate_replication_distribution_enabled_raw[each.key]
        || can(regex("^[A-Za-z][A-Za-z0-9._-]{0,31}$", local.goldengate_replication_distribution_path_raw[each.key]))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.distribution.pathName must be a valid path name (1-32 characters)."
    }
    precondition {
      condition = (
        !local.goldengate_replication_distribution_enabled_raw[each.key]
        || (
          can(regex("^[a-z][a-z0-9]$", local.goldengate_replication_distribution_src_trail_raw[each.key]))
          && can(regex("^[a-z][a-z0-9]$", local.goldengate_replication_distribution_tgt_trail_raw[each.key]))
          && local.goldengate_replication_distribution_src_trail_raw[each.key] != local.goldengate_replication_distribution_tgt_trail_raw[each.key]
        )
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.distribution sourceTrailName/targetTrailName must be valid, non-colliding two-character trail names."
    }
    precondition {
      condition = (
        !local.goldengate_replication_checkpoint_enabled_raw[each.key]
        || can(regex("^[A-Za-z_][A-Za-z0-9_]*\\.[A-Za-z_][A-Za-z0-9_]*$", local.goldengate_replication_checkpoint_table_raw[each.key]))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.checkpoint.table must be a safe schema.table identifier."
    }
    precondition {
      condition = (
        !local.goldengate_replication_extract_enabled_raw[each.key]
        || contains(["true", "false"], local.goldengate_replication_extract_start_jsonenc[each.key])
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.extract.startOnCreate must be a literal Boolean, not a Boolean-like string."
    }
    precondition {
      condition = (
        !local.goldengate_replication_distribution_enabled_raw[each.key]
        || contains(["true", "false"], local.goldengate_replication_distribution_start_jsonenc[each.key])
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.distribution.startOnCreate must be a literal Boolean, not a Boolean-like string."
    }
    precondition {
      condition = (
        !local.goldengate_replication_replicat_enabled_raw[each.key]
        || contains(["true", "false"], local.goldengate_replication_replicat_start_jsonenc[each.key])
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.replicat.startOnCreate must be a literal Boolean, not a Boolean-like string."
    }
    precondition {
      # Fresh-EKS Phase A/Phase 9: ingress.hostDomain/alb.groupName/alb.certificateArn are shared environment configuration, injected by the deploy workflow from envs/dev/environment.yaml -- forbid their reintroduction rather than requiring/validating a descriptor-owned copy. ingress.alb.groupOrder remains deployment-specific and stays required/validated below.
      condition     = try(each.value.ingress.hostDomain, null) == null
      error_message = "envs/${var.environment}/${each.key}/values.yaml: ingress.hostDomain is a forbidden override -- it is shared environment configuration and must not be set in a runtime descriptor."
    }
    precondition {
      condition     = try(each.value.ingress.alb.groupName, null) == null
      error_message = "envs/${var.environment}/${each.key}/values.yaml: ingress.alb.groupName is a forbidden override -- it is shared environment configuration and must not be set in a runtime descriptor."
    }
    precondition {
      condition     = try(each.value.ingress.alb.certificateArn, null) == null
      error_message = "envs/${var.environment}/${each.key}/values.yaml: ingress.alb.certificateArn is a forbidden override -- it is shared environment configuration and must not be set in a runtime descriptor."
    }
    precondition {
      condition     = !contains(local.goldengate_duplicate_alb_group_order_ids, each.key)
      error_message = "envs/${var.environment}/${each.key}/values.yaml: ingress.alb.groupOrder duplicates another enabled runtime's ALB group order."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_declared[each.key]
        || local.goldengate_persistence_enabled_jsonenc[each.key] == "true"
        || local.goldengate_persistence_enabled_jsonenc[each.key] == "false"
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: persistence.enabled must be a literal Boolean, not a Boolean-like string, when persistence is present."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || local.goldengate_runtime_storage_u02_type_raw[each.key] == "efs"
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.storage.u02.type must be \"efs\" when persistence.enabled=true and persistence.provider=efs."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || contains(["managed", "existing"], local.goldengate_persistence_efs_mode_raw[each.key])
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: persistence.efs.mode must be explicitly \"managed\" or \"existing\" when persistence.enabled=true and persistence.provider=efs."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || local.goldengate_persistence_efs_mode_raw[each.key] != "existing"
        || can(regex("^fs-[0-9a-f]+$", local.goldengate_persistence_efs_filesystem_id_raw[each.key]))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: persistence.efs.fileSystemId is not a safe EFS filesystem ID (required when persistence.efs.mode=existing)."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || local.goldengate_persistence_efs_mode_raw[each.key] != "managed"
        || !local.goldengate_persistence_efs_filesystem_id_declared[each.key]
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: persistence.efs.fileSystemId must not be set when persistence.efs.mode=managed -- Terraform provisions and resolves it."
    }
    precondition {
      condition = (
        !local.goldengate_persistence_efs_declared[each.key]
        || local.goldengate_persistence_efs_mode_raw[each.key] != "managed"
        || length("${var.environment}-${each.key}-efs") <= 64
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: derived EFS creation token \"${var.environment}-${each.key}-efs\" exceeds the 64-character AWS EFS creation-token limit."
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
      condition = alltrue([
        for pipeline in local.goldengate_pipeline_names : (
          length([
            for id in local.goldengate_deployment_names : id
            if try(local.goldengate_enabled_deployments[id].deployment.pipeline, "") == pipeline
            && local.goldengate_replication_enabled_raw[id]
          ]) == 0
          || (
            try(local.goldengate_replication_enabled_raw[local.goldengate_replication_pipeline_members[pipeline].source_id], false)
            && try(local.goldengate_replication_enabled_raw[local.goldengate_replication_pipeline_members[pipeline].target_id], false)
          )
        )
      ])
      error_message = "A pipeline with replication.enabled=true must have exactly one enabled source and one enabled target deployment, both with replication.enabled=true."
    }
    precondition {
      condition = alltrue([
        for pipeline in local.goldengate_replication_pipelines_enabled :
        local.goldengate_replication_distribution_target_raw[local.goldengate_replication_pipeline_members[pipeline].source_id]
        == local.goldengate_replication_pipeline_members[pipeline].target_id
      ])
      error_message = "replication.distribution.targetDeployment must equal the target deployment ID for its pipeline."
    }
    precondition {
      condition = alltrue([
        for pipeline in local.goldengate_replication_pipelines_enabled :
        local.goldengate_replication_distribution_src_trail_raw[local.goldengate_replication_pipeline_members[pipeline].source_id]
        == local.goldengate_replication_extract_trail_raw[local.goldengate_replication_pipeline_members[pipeline].source_id]
      ])
      error_message = "replication.distribution.sourceTrailName must equal replication.extract.trail.name for its pipeline."
    }
    precondition {
      condition = alltrue([
        for pipeline in local.goldengate_replication_pipelines_enabled :
        local.goldengate_replication_distribution_tgt_trail_raw[local.goldengate_replication_pipeline_members[pipeline].source_id]
        == local.goldengate_replication_replicat_trail_raw[local.goldengate_replication_pipeline_members[pipeline].target_id]
      ])
      error_message = "replication.distribution.targetTrailName must equal the target replication.replicat.sourceTrailName for its pipeline."
    }
    precondition {
      # Permanent, fresh-cluster architecture: the runtime trust subject must be EXACTLY the one canonical gg-runtime-sa identity -- no wildcard, no per-engine subject, no migration-compatibility entry. try() guards the [0] index: Terraform's && does not short-circuit evaluation errors, so an empty subjects list must not crash `terraform plan` with "Invalid index" -- it must fail this precondition instead.
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

# Fresh-EKS Phase A/Phase 9: the full repository is always derived as local.gg_env_ecr_registry/repositoryName (never descriptor-asserted), so it is definitionally inside the approved private ECR account/region -- this diagnostic now proves every enabled deployment supplies the one thing it DOES own, a non-empty repositoryName (the plan-blocking grammar/safety check itself lives in the goldengate_runtime_contract precondition above).
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

check "goldengate_replication_pipelines_well_formed" {
  assert {
    condition = alltrue([
      for pipeline in local.goldengate_pipeline_names : (
        length([
          for id in local.goldengate_deployment_names : id
          if try(local.goldengate_enabled_deployments[id].deployment.pipeline, "") == pipeline
          && local.goldengate_replication_enabled_raw[id]
        ]) == 0
        || contains(local.goldengate_replication_pipelines_enabled, pipeline)
      )
    ])
    error_message = "A pipeline with replication.enabled=true must have exactly one enabled source and one enabled target deployment, both with replication.enabled=true."
  }
}
