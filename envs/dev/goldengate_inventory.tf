# Folder-driven GoldenGate deployment inventory (mirrors hack/goldengate-deployment-model.py); every consumer reads these locals, never the folder tree directly.

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

  # A candidate must satisfy the full folder-driven contract before it is eligible for any active/inactive classification.
  goldengate_runtime_candidates = {
    for id, doc in local.goldengate_runtime_documents : id => doc
    if try(doc.deploymentModel, "") == "singleRuntime"
    && try(doc.deployment.enabled, null) != null
    && try(doc.deployment.pipeline, "") != ""
    && contains(["source", "target"], try(doc.deployment.role, ""))
    && try(doc.runtime.deploymentType, "") != ""
    && try(doc.runtime.image.repository, "") != ""
    && try(doc.runtime.image.tag, "") != ""
    && try(doc.runtime.image.tag, "latest") != "latest"
    && try(doc.runtime.serviceAccount.name, "") == "gg-runtime-sa"
    && try(doc.runtime.serviceAccount.create, true) == false
  }

  goldengate_enabled_deployments = {
    for id, doc in local.goldengate_runtime_candidates : id => doc
    if doc.deployment.enabled == true
    && try(doc.lifecycle.state, "active") != "absent"
  }

  goldengate_deployment_names = sort(keys(local.goldengate_enabled_deployments))

  goldengate_pipeline_names = sort(distinct([
    for id in local.goldengate_deployment_names : local.goldengate_enabled_deployments[id].deployment.pipeline
  ]))

  goldengate_admin_secret_managed = {
    for id in local.goldengate_deployment_names : id =>
    try(local.goldengate_enabled_deployments[id].deployment.adminSecret.managed, true)
  }

  goldengate_admin_secret_names = {
    for id in local.goldengate_deployment_names : id =>
    try(local.goldengate_enabled_deployments[id].deployment.adminSecret.name, "dev/goldengate/runtime/${id}/admin")
  }

  goldengate_managed_admin_secrets = {
    for id in local.goldengate_deployment_names : id => local.goldengate_admin_secret_names[id]
    if local.goldengate_admin_secret_managed[id]
  }

  goldengate_platform_values = yamldecode(file("${path.module}/../../platform/dev/goldengate-platform/values.yaml"))
  goldengate_monitor_values  = yamldecode(file("${path.module}/goldengate-monitor/values.yaml"))
  goldengate_monitor_host    = try(local.goldengate_monitor_values.ingress.host, "")

  goldengate_shared_environment = {
    environment         = "dev"
    runtimeNamespace    = try(local.goldengate_platform_values.namespaces.runtime.name, "goldengate-dev")
    monitoringNamespace = try(local.goldengate_platform_values.fluentBit.namespaces.monitoring, "goldengate-monitoring")
    dnsDomain           = local.goldengate_monitor_host != "" ? trimprefix(local.goldengate_monitor_host, "monitor.") : "goldengate-dev.adcbmis.local"
    tlsSecret           = "dev/goldengate/tls-certificate"
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
        if local.goldengate_enabled_deployments[id].deployment.pipeline == pipeline
        && local.goldengate_enabled_deployments[id].deployment.role == "source"
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
        if local.goldengate_enabled_deployments[id].deployment.pipeline == pipeline
        && local.goldengate_enabled_deployments[id].deployment.role == "target"
      ]) <= 1
    ])
    error_message = "A GoldenGate pipeline has more than one enabled target deployment."
  }
}

check "goldengate_approved_ecr_registry_only" {
  assert {
    condition = alltrue([
      for id in local.goldengate_deployment_names :
      startswith(local.goldengate_enabled_deployments[id].runtime.image.repository,
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
