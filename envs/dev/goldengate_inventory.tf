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

  goldengate_default_admin_secret_names = {
    for id in keys(local.goldengate_runtime_documents) : id =>
    "${var.environment}/goldengate/runtime/${id}/admin"
  }

  # Precomputed via try() since Terraform's &&/|| do not short-circuit around attribute-access errors.
  goldengate_admin_secret_declared = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.deployment.adminSecret, null) != null
  }
  goldengate_admin_secret_name_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.deployment.adminSecret.name, "")
  }
  goldengate_admin_secret_managed_is_bool = {
    for id, doc in local.goldengate_runtime_documents : id => try(can(tobool(doc.deployment.adminSecret.managed)), false)
  }
  goldengate_admin_secret_managed_bool = {
    for id, doc in local.goldengate_runtime_documents : id => try(tobool(doc.deployment.adminSecret.managed), false)
  }
  goldengate_resolved_admin_secret_name = {
    for id in keys(local.goldengate_runtime_documents) : id =>
    local.goldengate_admin_secret_declared[id] ? local.goldengate_admin_secret_name_raw[id] : local.goldengate_default_admin_secret_names[id]
  }
  goldengate_csi_admin_object_name_raw = {
    for id, doc in local.goldengate_runtime_documents : id => try(doc.runtime.csi.admin.objectName, null)
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

  goldengate_admin_secret_managed = {
    for id in local.goldengate_deployment_names : id =>
    try(local.goldengate_enabled_deployments[id].deployment.adminSecret.managed, true)
  }

  goldengate_admin_secret_names = {
    for id in local.goldengate_deployment_names : id =>
    try(local.goldengate_enabled_deployments[id].deployment.adminSecret.name, local.goldengate_default_admin_secret_names[id])
  }

  goldengate_managed_admin_secrets = {
    for id in local.goldengate_deployment_names : id => local.goldengate_admin_secret_names[id]
    if local.goldengate_admin_secret_managed[id]
  }

  goldengate_platform_values = yamldecode(file("${path.module}/../../platform/${var.environment}/goldengate-platform/values.yaml"))
  goldengate_monitor_values  = yamldecode(file("${path.module}/goldengate-monitor/values.yaml"))
  goldengate_monitor_host    = try(local.goldengate_monitor_values.ingress.host, "")

  goldengate_shared_environment = {
    environment         = var.environment
    runtimeNamespace    = try(local.goldengate_platform_values.namespaces.runtime.name, "goldengate-${var.environment}")
    monitoringNamespace = try(local.goldengate_platform_values.fluentBit.namespaces.monitoring, "goldengate-monitoring")
    dnsDomain           = local.goldengate_monitor_host != "" ? trimprefix(local.goldengate_monitor_host, "monitor.") : "goldengate-${var.environment}.adcbmis.local"
    tlsSecret           = "${var.environment}/goldengate/tls-certificate"
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
      condition     = can(tobool(each.value.deployment.enabled))
      error_message = "envs/${var.environment}/${each.key}/values.yaml: deployment.enabled must be a literal Boolean."
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
      condition     = try(each.value.global.environment, "") == var.environment
      error_message = "envs/${var.environment}/${each.key}/values.yaml: global.environment must match the scanned environment."
    }
    precondition {
      condition     = try(each.value.runtime.deploymentType, "") != "" && can(regex("^[a-z][a-z0-9-]*$", each.value.runtime.deploymentType))
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.deploymentType must be a safe lowercase token."
    }
    precondition {
      condition = (
        startswith(try(each.value.runtime.image.repository, ""), "229410149234.dkr.ecr.eu-west-1.amazonaws.com/")
        && try(each.value.runtime.image.repository, "") != "229410149234.dkr.ecr.eu-west-1.amazonaws.com/"
        && can(regex("^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$",
        trimprefix(try(each.value.runtime.image.repository, ""), "229410149234.dkr.ecr.eu-west-1.amazonaws.com/")))
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.image.repository must be a private ECR repository in the approved account/region with a safe, non-empty suffix."
    }
    precondition {
      condition     = try(each.value.runtime.image.tag, "") != "" && try(each.value.runtime.image.tag, "latest") != "latest"
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.image.tag must be explicit and must not be \"latest\"."
    }
    precondition {
      condition     = try(each.value.runtime.serviceAccount.name, "") == "gg-runtime-sa"
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.serviceAccount.name must be \"gg-runtime-sa\"."
    }
    precondition {
      condition     = try(each.value.runtime.serviceAccount.create, true) == false
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.serviceAccount.create must be literal false."
    }
    precondition {
      condition = (
        !local.goldengate_admin_secret_declared[each.key]
        || (
          local.goldengate_admin_secret_managed_is_bool[each.key]
          && local.goldengate_admin_secret_name_raw[each.key] != ""
          && startswith(local.goldengate_admin_secret_name_raw[each.key], "${var.environment}/")
          && !can(regex("\\.\\.", local.goldengate_admin_secret_name_raw[each.key]))
          && !startswith(local.goldengate_admin_secret_name_raw[each.key], "/")
          && !startswith(local.goldengate_admin_secret_name_raw[each.key], "arn:")
          && (
            !local.goldengate_admin_secret_managed_bool[each.key]
            || local.goldengate_admin_secret_name_raw[each.key] == local.goldengate_default_admin_secret_names[each.key]
          )
        )
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: deployment.adminSecret is malformed, out of environment scope, or managed=true does not use the deterministic deployment-specific secret name."
    }
    precondition {
      condition     = try(each.value.lifecycle, null) == null || contains(["active", "absent"], try(each.value.lifecycle.state, "active"))
      error_message = "envs/${var.environment}/${each.key}/values.yaml: lifecycle.state must be \"active\" or \"absent\" when lifecycle is present."
    }
    precondition {
      condition     = try(each.value.replication.enabled, false) == false
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.enabled must be false in Phase 6D0."
    }
    precondition {
      condition     = try(each.value.ingress.hostDomain, "") == local.goldengate_shared_environment.dnsDomain
      error_message = "envs/${var.environment}/${each.key}/values.yaml: ingress.hostDomain must match the shared DNS domain."
    }
    precondition {
      condition     = try(each.value.runtime.csi.certificate.objectName, "") == local.goldengate_shared_environment.tlsSecret
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.csi.certificate.objectName must equal the approved shared TLS secret."
    }
    precondition {
      condition = (
        local.goldengate_csi_admin_object_name_raw[each.key] == null
        || local.goldengate_csi_admin_object_name_raw[each.key] == local.goldengate_resolved_admin_secret_name[each.key]
      )
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.csi.admin.objectName does not match the resolved admin-secret name."
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

check "goldengate_approved_ecr_registry_only" {
  assert {
    condition = alltrue([
      for id in local.goldengate_deployment_names :
      startswith(try(local.goldengate_enabled_deployments[id].runtime.image.repository, ""),
      "229410149234.dkr.ecr.eu-west-1.amazonaws.com/")
    ])
    error_message = "An enabled GoldenGate deployment references an image outside the approved private ECR account/region."
  }
}

check "goldengate_no_enabled_replication_bootstrap" {
  assert {
    condition = alltrue([
      for id in local.goldengate_deployment_names :
      try(local.goldengate_enabled_deployments[id].replication.enabled, false) == false
    ])
    error_message = "Replication bootstrap activation is not available in Phase 6D0. Complete the approved database and GoldenGate Admin REST validation phase first."
  }
}
