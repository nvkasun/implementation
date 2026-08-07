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
    ? "${var.environment}/goldengate/source/admin"
    : "${var.environment}/goldengate/target/admin"
  }

  goldengate_tls_secret_name = "${var.environment}/goldengate/tls-certificate"

  # Deterministic naming, never a hardcoded map: deploymentType alone selects the ServiceAccount name. Only a safe token (enforced by the precondition below) ever reaches this string interpolation.
  goldengate_runtime_service_account_names = {
    for id in local.goldengate_deployment_names : id =>
    "gg-${try(local.goldengate_enabled_deployments[id].runtime.deploymentType, "")}-sa"
  }

  # Unique enabled deployment types, sorted deterministically; mirrors hack/goldengate-deployment-model.py's runtime-identities command.
  goldengate_enabled_deployment_types = sort(distinct([
    for id in local.goldengate_deployment_names : local.goldengate_deployment_type_raw[id]
  ]))

  goldengate_runtime_identity_inventory = {
    for t in local.goldengate_enabled_deployment_types : t => "gg-${t}-sa"
  }

  # The retained, honestly-unresolved legacy exception (see envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json); requires live-cluster evidence before removal, never generated or removed by this file.
  goldengate_legacy_wildcard_trust_subject = "system:serviceaccount:gg-dev-*:ogg-oracle-sa"

  goldengate_expected_irsa_trust_subjects = sort([
    for t in local.goldengate_enabled_deployment_types :
    "system:serviceaccount:${local.goldengate_shared_environment.runtimeNamespace}:gg-${t}-sa"
  ])

  goldengate_secrets_trust_policy = jsondecode(file("${path.module}/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json"))
  goldengate_secrets_trust_subjects = local.goldengate_secrets_trust_policy.Statement[0].Condition.StringLike[
    "oidc.eks.eu-west-1.amazonaws.com/id/407C4385FF87947926730569F1E564FB:sub"
  ]
  goldengate_secrets_trust_subjects_non_legacy = sort([
    for s in local.goldengate_secrets_trust_subjects : s
    if s != local.goldengate_legacy_wildcard_trust_subject
  ])

  goldengate_platform_values = yamldecode(file("${path.module}/../../platform/${var.environment}/goldengate-platform/values.yaml"))
  goldengate_monitor_values  = yamldecode(file("${path.module}/goldengate-monitor/values.yaml"))
  goldengate_monitor_host    = try(local.goldengate_monitor_values.ingress.host, "")

  goldengate_shared_environment = {
    environment         = var.environment
    runtimeNamespace    = try(local.goldengate_platform_values.namespaces.runtime.name, "goldengate-${var.environment}")
    monitoringNamespace = try(local.goldengate_platform_values.fluentBit.namespaces.monitoring, "goldengate-monitoring")
    dnsDomain           = local.goldengate_monitor_host != "" ? trimprefix(local.goldengate_monitor_host, "monitor.") : "goldengate-${var.environment}.adcbmis.local"
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
      condition     = try(each.value.global.environment, "") == var.environment
      error_message = "envs/${var.environment}/${each.key}/values.yaml: global.environment must match the scanned environment."
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
      condition     = !local.goldengate_service_account_declared[each.key]
      error_message = "envs/${var.environment}/${each.key}/values.yaml: runtime.serviceAccount is a forbidden override -- it is derived solely from runtime.deploymentType."
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
      condition     = try(each.value.replication.enabled, false) == false
      error_message = "envs/${var.environment}/${each.key}/values.yaml: replication.enabled must be false in Phase 6D0."
    }
    precondition {
      condition     = try(each.value.ingress.hostDomain, "") == local.goldengate_shared_environment.dnsDomain
      error_message = "envs/${var.environment}/${each.key}/values.yaml: ingress.hostDomain must match the shared DNS domain."
    }
    precondition {
      condition     = !contains(local.goldengate_duplicate_alb_group_order_ids, each.key)
      error_message = "envs/${var.environment}/${each.key}/values.yaml: ingress.alb.groupOrder duplicates another enabled runtime's ALB group order."
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
        for id in local.goldengate_deployment_names :
        try(local.goldengate_enabled_deployments[id].replication.enabled, false) == false
      ])
      error_message = "Replication bootstrap activation is not available in Phase 6D0. Complete the approved database and GoldenGate Admin REST validation phase first."
    }
    precondition {
      condition     = local.goldengate_secrets_trust_subjects_non_legacy == local.goldengate_expected_irsa_trust_subjects
      error_message = "envs/dev/policies/goldengate-secrets-read-dev/assume_role_policy/sts.json trust subjects do not match the folder-driven runtime identity inventory -- add or remove the exact system:serviceaccount:<namespace>:gg-<type>-sa subject for every enabled deployment type (the legacy gg-dev-*:ogg-oracle-sa wildcard is retained separately and is never generated or removed by this check)."
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
