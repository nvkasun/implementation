#!/usr/bin/env python3
"""automation/goldengate-environment.py: the single canonical reader/validator/deriver for envs/<environment>/environment.yaml -- the sole committed source of truth for shared AWS/EKS/network/IAM environment identity. Never a second environment-config parser: automation/goldengate-deployment-model.py imports this module's load/derive functions rather than re-implementing them. Never prints secret VALUES -- environment.yaml itself must only ever contain non-secret names/paths/ARNs that reference where secrets live, never secret material, and this module fails closed if it detects a credential-shaped key or value."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

SUPPORTED_SCHEMA_VERSION = 1

# The destroyed-cluster OIDC ID that must never appear in production configuration again.
OLD_DESTROYED_OIDC_ID = "407C4385FF87947926730569F1E564FB"

_ENV_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*\Z")
_ACCOUNT_ID_RE = re.compile(r"^\d{12}\Z")
_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d\Z")
_CLUSTER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,99}\Z")
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")
_DNS_DOMAIN_RE = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\Z")
_IAM_NAME_RE = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}\Z")
_OIDC_ISSUER_RE = re.compile(
    r"^https://oidc\.eks\.(?P<region>[a-z]{2}-[a-z]+-\d)\.amazonaws\.com/id/(?P<id>[0-9A-Fa-f]{32})\Z"
)
_ACM_ARN_RE = re.compile(
    r"^arn:aws:acm:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):certificate/[0-9a-f-]+\Z"
)
_ROLE_ARN_RE = re.compile(
    r"^arn:aws:iam::(?P<account>\d{12}):role/(?P<name>[A-Za-z0-9+=,.@_/-]+)\Z"
)
_KMS_ARN_RE = re.compile(
    r"^arn:aws:kms:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):key/[0-9a-fA-F-]+\Z"
)

_GITHUB_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*\Z")
_GITHUB_ENV_FORBIDDEN_VALUE_CHARS = ("\n", "\r", "\x00")

_REQUIRED_TAG_KEYS = (
    "applicationName", "businessCriticality", "businessUnit", "businessUnitOwner",
    "costCenter", "mapMigrated", "requestReference", "dataClassification",
)
_REQUIRED_ROLE_KEYS = (
    "eksDeploy", "runtime", "monitor", "argocdEcrRead", "platformLogging", "cloudwatchMetrics",
)

_CREDENTIAL_KEY_FRAGMENTS = (
    "password", "passwd", "pwd", "secretvalue", "connectionstring", "conn_str",
    "username", "token", "apikey", "api_key", "privatekey", "private_key",
)
_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class _StrictLoader(yaml.SafeLoader):
    pass


def _no_duplicate_keys(loader, node):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate key in mapping", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def load_yaml_strict(path):
    with open(path) as f:
        return yaml.load(f, Loader=_StrictLoader)


def environment_file_path(environment):
    return os.path.join(REPO_ROOT, "envs", environment, "environment.yaml")


def _contains_credential_like_key(node):
    """Fails closed on password/token/private-key-shaped keys; a mere secret NAME/path reference (e.g. ecrSyncRoleArn, sharedSecurityGroupDescription) is never flagged since "secret"/"arn" alone are not forbidden fragments."""
    if isinstance(node, dict):
        for key, value in node.items():
            key_lower = str(key).lower()
            if any(fragment in key_lower for fragment in _CREDENTIAL_KEY_FRAGMENTS):
                return key
            found = _contains_credential_like_key(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _contains_credential_like_key(item)
            if found:
                return found
    return None


def _contains_credential_like_value(node):
    """Fails closed on AWS access-key-ID-shaped or PEM-private-key-shaped string VALUES anywhere in the document -- distinct from _contains_credential_like_key, which looks at key names."""
    if isinstance(node, dict):
        for value in node.values():
            found = _contains_credential_like_value(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _contains_credential_like_value(item)
            if found:
                return found
    elif isinstance(node, str):
        for pattern in _CREDENTIAL_VALUE_PATTERNS:
            if pattern.search(node):
                return pattern.pattern
    return None


def _require(problems, condition, message):
    if not condition:
        problems.append(message)


def _validate_config(environment, doc):
    """Fail-closed structural + cross-field validation. Returns a list of problem strings; empty means valid."""
    problems = []

    if not isinstance(doc, dict):
        return ["top-level document must be a mapping"]

    _require(problems, doc.get("schemaVersion") == SUPPORTED_SCHEMA_VERSION,
              f"schemaVersion must be exactly {SUPPORTED_SCHEMA_VERSION}")

    doc_environment = doc.get("environment")
    _require(problems, isinstance(doc_environment, str) and bool(_ENV_TOKEN_RE.match(doc_environment)),
              "environment must be a safe lowercase token")
    _require(problems, doc_environment == environment,
              f"environment field ({doc_environment!r}) must match the requested environment ({environment!r})")

    aws = doc.get("aws") if isinstance(doc.get("aws"), dict) else {}
    region = aws.get("region")
    workload_account_id = aws.get("workloadAccountId")
    build_account_id = aws.get("buildAccountId")
    _require(problems, isinstance(region, str) and bool(_REGION_RE.match(region)), "aws.region must be a safe AWS region token")
    _require(problems, isinstance(workload_account_id, str) and bool(_ACCOUNT_ID_RE.match(workload_account_id)),
              "aws.workloadAccountId must be exactly 12 digits")
    _require(problems, isinstance(build_account_id, str) and bool(_ACCOUNT_ID_RE.match(build_account_id)),
              "aws.buildAccountId must be exactly 12 digits")

    eks = doc.get("eks") if isinstance(doc.get("eks"), dict) else {}
    cluster_name = eks.get("clusterName")
    oidc_issuer = eks.get("oidcIssuer")
    _require(problems, isinstance(cluster_name, str) and bool(_CLUSTER_NAME_RE.match(cluster_name)),
              "eks.clusterName must be non-empty and safe")
    _require(problems, isinstance(oidc_issuer, str) and oidc_issuer.startswith("https://"),
              "eks.oidcIssuer must use HTTPS")
    if isinstance(oidc_issuer, str):
        _require(problems, OLD_DESTROYED_OIDC_ID not in oidc_issuer,
                  f"eks.oidcIssuer must not reference the destroyed-cluster OIDC ID {OLD_DESTROYED_OIDC_ID}")
        m = _OIDC_ISSUER_RE.match(oidc_issuer)
        _require(problems, m is not None, "eks.oidcIssuer must have the exact EKS OIDC issuer HTTPS structure (https://oidc.eks.<region>.amazonaws.com/id/<32-hex-id>)")
        if m and isinstance(region, str):
            _require(problems, m.group("region") == region, "eks.oidcIssuer region must agree with aws.region")

    namespaces = doc.get("namespaces") if isinstance(doc.get("namespaces"), dict) else {}
    for ns_key in ("runtime", "monitoring", "argocd", "observability"):
        ns_value = namespaces.get(ns_key)
        _require(problems, isinstance(ns_value, str) and bool(_NAMESPACE_RE.match(ns_value)),
                  f"namespaces.{ns_key} must be a valid Kubernetes namespace name")

    network = doc.get("network") if isinstance(doc.get("network"), dict) else {}
    dns_domain = network.get("dnsDomain")
    alb_group_name = network.get("albGroupName")
    certificate_arn = network.get("certificateArn")
    _require(problems, isinstance(dns_domain, str) and bool(_DNS_DOMAIN_RE.match(dns_domain)), "network.dnsDomain must be a valid DNS domain")
    _require(problems, isinstance(alb_group_name, str) and bool(alb_group_name), "network.albGroupName must be a non-empty string")
    _require(problems, isinstance(certificate_arn, str) and bool(_ACM_ARN_RE.match(certificate_arn)), "network.certificateArn must be a valid ACM certificate ARN")
    if isinstance(certificate_arn, str):
        m = _ACM_ARN_RE.match(certificate_arn)
        if m and isinstance(workload_account_id, str):
            _require(problems, m.group("account") == workload_account_id, "network.certificateArn account must equal aws.workloadAccountId")
        if m and isinstance(region, str):
            _require(problems, m.group("region") == region, "network.certificateArn region must equal aws.region")

    iam = doc.get("iam") if isinstance(doc.get("iam"), dict) else {}
    roles = iam.get("roles") if isinstance(iam.get("roles"), dict) else {}
    for role_key in _REQUIRED_ROLE_KEYS:
        role_name = roles.get(role_key)
        _require(problems, isinstance(role_name, str) and bool(_IAM_NAME_RE.match(role_name)),
                  f"iam.roles.{role_key} must be a non-empty, safe IAM role name")
    runner_role_name = iam.get("runnerRoleName")
    _require(problems, isinstance(runner_role_name, str) and bool(_IAM_NAME_RE.match(runner_role_name)),
              "iam.runnerRoleName must be a non-empty, safe IAM role name")
    ecr_sync_role_arn = iam.get("ecrSyncRoleArn")
    _require(problems, isinstance(ecr_sync_role_arn, str) and bool(_ROLE_ARN_RE.match(ecr_sync_role_arn)),
              "iam.ecrSyncRoleArn must be a valid IAM role ARN")
    if isinstance(ecr_sync_role_arn, str):
        m = _ROLE_ARN_RE.match(ecr_sync_role_arn)
        if m and isinstance(build_account_id, str):
            _require(problems, m.group("account") == build_account_id, "iam.ecrSyncRoleArn account must equal aws.buildAccountId")

    kms = doc.get("kms") if isinstance(doc.get("kms"), dict) else {}
    monitor_kms_arn = kms.get("monitorDynamoDbKeyArn")
    _require(problems, isinstance(monitor_kms_arn, str) and bool(_KMS_ARN_RE.match(monitor_kms_arn)),
              "kms.monitorDynamoDbKeyArn must be a valid KMS key ARN")
    if isinstance(monitor_kms_arn, str):
        m = _KMS_ARN_RE.match(monitor_kms_arn)
        if m and isinstance(workload_account_id, str):
            _require(problems, m.group("account") == workload_account_id, "kms.monitorDynamoDbKeyArn account must equal aws.workloadAccountId")
        if m and isinstance(region, str):
            _require(problems, m.group("region") == region, "kms.monitorDynamoDbKeyArn region must equal aws.region")

    efs = doc.get("efs") if isinstance(doc.get("efs"), dict) else {}
    _require(problems, isinstance(efs.get("sharedSecurityGroupDescription"), str) and bool(efs.get("sharedSecurityGroupDescription")),
              "efs.sharedSecurityGroupDescription must be a non-empty string")

    tags = doc.get("tags") if isinstance(doc.get("tags"), dict) else {}
    for tag_key in _REQUIRED_TAG_KEYS:
        _require(problems, isinstance(tags.get(tag_key), str) and bool(tags.get(tag_key)),
                  f"tags.{tag_key} must be a non-empty string")

    credential_key = _contains_credential_like_key(doc)
    _require(problems, credential_key is None, f"environment.yaml must not contain a credential-shaped key: {credential_key!r}")
    credential_value_pattern = _contains_credential_like_value(doc)
    _require(problems, credential_value_pattern is None,
              f"environment.yaml must not contain a credential-shaped value (matched pattern {credential_value_pattern!r})")

    return problems


def load_environment_config(environment):
    """Loads and fully validates envs/<environment>/environment.yaml. Raises ValueError with every problem found -- never returns a partially-valid config."""
    path = environment_file_path(environment)
    if not os.path.isfile(path):
        raise ValueError(f"{path} does not exist")
    try:
        doc = load_yaml_strict(path)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid or duplicate-key YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top-level document must be a mapping")

    problems = _validate_config(environment, doc)
    if problems:
        raise ValueError(f"{path} failed validation: " + "; ".join(problems))
    return doc


def derive_values(doc):
    """Derives every value consumed by Terraform/Python/workflows/Helm from the validated environment.yaml document. Never duplicated/re-typed at any call site -- this is the one place derivation formulas live."""
    environment = doc["environment"]
    aws = doc["aws"]
    region = aws["region"]
    workload_account_id = aws["workloadAccountId"]
    build_account_id = aws["buildAccountId"]

    eks = doc["eks"]
    cluster_name = eks["clusterName"]
    oidc_issuer = eks["oidcIssuer"]
    oidc_hostpath = oidc_issuer[len("https://"):]

    namespaces = doc["namespaces"]
    network = doc["network"]
    iam = doc["iam"]
    roles = iam["roles"]
    kms = doc["kms"]
    efs = doc["efs"]
    tags = doc["tags"]

    ecr_registry = f"{build_account_id}.dkr.ecr.{region}.amazonaws.com"
    eks_cluster_arn = f"arn:aws:eks:{region}:{workload_account_id}:cluster/{cluster_name}"
    oidc_provider_arn = f"arn:aws:iam::{workload_account_id}:oidc-provider/{oidc_hostpath}"

    def role_arn(role_name):
        return f"arn:aws:iam::{workload_account_id}:role/{role_name}"

    dns_domain = network["dnsDomain"]

    values = {
        "GG_ENVIRONMENT": environment,
        "AWS_REGION": region,
        "WORKLOAD_ACCOUNT_ID": workload_account_id,
        "ECR_ACCOUNT_ID": build_account_id,
        "ECR_REGISTRY": ecr_registry,

        "EKS_CLUSTER_NAME": cluster_name,
        "EKS_CLUSTER_ARN": eks_cluster_arn,

        "EKS_OIDC_ISSUER": oidc_issuer,
        "EKS_OIDC_HOSTPATH": oidc_hostpath,
        "EKS_OIDC_PROVIDER_ARN": oidc_provider_arn,

        "EKS_DEPLOY_ROLE_NAME": roles["eksDeploy"],
        "EKS_DEPLOY_ROLE_ARN": role_arn(roles["eksDeploy"]),

        "RUNTIME_ROLE_NAME": roles["runtime"],
        "RUNTIME_ROLE_ARN": role_arn(roles["runtime"]),

        "MONITOR_ROLE_NAME": roles["monitor"],
        "MONITOR_ROLE_ARN": role_arn(roles["monitor"]),

        "ARGOCD_ECR_READ_ROLE_NAME": roles["argocdEcrRead"],
        "ARGOCD_ECR_READ_ROLE_ARN": role_arn(roles["argocdEcrRead"]),

        "PLATFORM_LOGGING_ROLE_NAME": roles["platformLogging"],
        "PLATFORM_LOGGING_ROLE_ARN": role_arn(roles["platformLogging"]),

        "CLOUDWATCH_METRICS_ROLE_NAME": roles["cloudwatchMetrics"],
        "CLOUDWATCH_METRICS_ROLE_ARN": role_arn(roles["cloudwatchMetrics"]),

        "RUNNER_ROLE_ARN": f"arn:aws:iam::{build_account_id}:role/{iam['runnerRoleName']}",
        "ECR_SYNC_ROLE_ARN": iam["ecrSyncRoleArn"],

        "RUNTIME_NAMESPACE": namespaces["runtime"],
        "MONITOR_NAMESPACE": namespaces["monitoring"],
        "ARGOCD_NAMESPACE": namespaces["argocd"],
        "OBSERVABILITY_NAMESPACE": namespaces["observability"],

        "DNS_DOMAIN": dns_domain,
        "MONITOR_HOST": f"monitor.{dns_domain}",
        "ARGOCD_HOST": f"argocd.{dns_domain}",

        "ALB_GROUP_NAME": network["albGroupName"],
        "ACM_CERTIFICATE_ARN": network["certificateArn"],

        "SOURCE_ADMIN_SECRET_NAME": f"{environment}/goldengate/source/admin",
        "TARGET_ADMIN_SECRET_NAME": f"{environment}/goldengate/target/admin",
        "TLS_SECRET_NAME": f"{environment}/goldengate/tls-certificate",

        "RUNTIME_LOG_GROUP": f"/adcb/goldengate/{environment}/runtime",
        "MONITOR_LOG_GROUP": f"/adcb/goldengate/{environment}/monitor",
        "CONTAINER_INSIGHTS_LOG_GROUP": f"/aws/containerinsights/{cluster_name}/performance",

        "EFS_SHARED_SECURITY_GROUP_DESCRIPTION": efs["sharedSecurityGroupDescription"],

        "MONITOR_DYNAMODB_KMS_KEY_ARN": kms["monitorDynamoDbKeyArn"],

        "TAG_APPLICATION_NAME": tags["applicationName"],
        "TAG_BUSINESS_CRITICALITY": tags["businessCriticality"],
        "TAG_BUSINESS_UNIT": tags["businessUnit"],
        "TAG_BUSINESS_UNIT_OWNER": tags["businessUnitOwner"],
        "TAG_COST_CENTER": tags["costCenter"],
        "TAG_MAP_MIGRATED": tags["mapMigrated"],
        "TAG_REQUEST_REFERENCE": tags["requestReference"],
        "TAG_DATA_CLASSIFICATION": tags["dataClassification"],
    }
    return values


def format_github_env(values):
    """The single trust boundary between derive_values() and any GITHUB_ENV consumer: validates every name/value pair BEFORE returning anything, so a single unsafe derived value can never manufacture a second, independent GITHUB_ENV record via an embedded line break -- these are single-line infrastructure identity values, never multiline documents, so a bare CR/LF/NUL is rejected outright rather than stripped or normalized. Raises ValueError (never partially formats) naming only the offending KEY, never its value. Returns deterministic 'KEY=value' lines sorted by key -- byte-for-byte identical to the prior unvalidated output for every currently valid environment.yaml."""
    problems = []
    for key, value in values.items():
        if not isinstance(key, str) or not _GITHUB_ENV_NAME_RE.match(key):
            problems.append(f"{key!r} is not a safe GITHUB_ENV variable name")
            continue
        if not isinstance(value, str):
            problems.append(f"{key!r} value is not a string (got {type(value).__name__})")
            continue
        if any(forbidden in value for forbidden in _GITHUB_ENV_FORBIDDEN_VALUE_CHARS):
            problems.append(f"{key!r} contains a forbidden line-break/control character")
    if problems:
        raise ValueError("; ".join(problems))
    return [f"{key}={values[key]}" for key in sorted(values)]


# --- IAM policy generation (Phase 4/16): environment.yaml -> generated policy_folder JSON artifacts. ---

# (policy_folder, subject, condition shape) for every assume_role_policy/sts.json whose Principal is IRSA-OIDC-federated (all except goldengate-eks-deploy-dev, which trusts the cross-account runner role directly). "shape" preserves each role's own already-reviewed condition structure exactly -- AWS IAM evaluates StringEquals and a no-wildcard StringLike identically, so these are a structural choice already made per-role, never re-derived: "list" (StringEquals aud + StringLike sub as a JSON array -- the only shape envs/dev/goldengate_inventory.tf's Terraform-side re-validation ever reads back, so it must stay list-shaped even for a single subject), "single_stringlike" (StringEquals aud + StringLike sub as a plain string), "flat_stringequals" (aud and sub both under one StringEquals block).
_IRSA_ROLE_FOLDERS = {
    "goldengate-secrets-read-dev": {
        "sid": "AllowGoldenGatePodsToAssumeRoleWithIRSA",
        "subjects": lambda v: [f"system:serviceaccount:{v['RUNTIME_NAMESPACE']}:gg-runtime-sa"],
        "shape": "list",
    },
    "goldengate-monitor-read-dev": {
        "sid": "AllowGoldenGateMonitorToAssumeRoleWithIRSA",
        "subjects": lambda v: [f"system:serviceaccount:{v['MONITOR_NAMESPACE']}:gg-monitor"],
        "shape": "flat_stringequals",
    },
    "goldengate-platform-logging-dev": {
        "sid": "AllowGoldenGateFluentBitToAssumeRoleWithIRSA",
        "subjects": lambda v: [f"system:serviceaccount:{v['RUNTIME_NAMESPACE']}:gg-fluent-bit"],
        "shape": "flat_stringequals",
    },
    "goldengate-cloudwatch-metrics-dev": {
        "sid": "AllowGoldenGateCloudWatchAgentToAssumeRoleWithIRSA",
        "subjects": lambda v: [f"system:serviceaccount:{v['OBSERVABILITY_NAMESPACE']}:cloudwatch-agent"],
        "shape": "flat_stringequals",
    },
    "argocd-ecr-oci-read-dev": {
        "sid": "AllowArgocdEcrTokenSyncToAssumeRoleWithIRSA",
        "subjects": lambda v: [f"system:serviceaccount:{v['ARGOCD_NAMESPACE']}:argocd-ecr-token-sync"],
        "shape": "single_stringlike",
    },
}


def _irsa_assume_role_policy(sid, oidc_provider_arn, oidc_hostpath, subjects, shape):
    """Exact-identity IRSA trust, no wildcard subject ever produced. Condition structure follows "shape" exactly (see _IRSA_ROLE_FOLDERS)."""
    if shape == "list":
        condition = {
            "StringEquals": {f"{oidc_hostpath}:aud": "sts.amazonaws.com"},
            "StringLike": {f"{oidc_hostpath}:sub": list(subjects)},
        }
    elif shape == "single_stringlike":
        condition = {
            "StringEquals": {f"{oidc_hostpath}:aud": "sts.amazonaws.com"},
            "StringLike": {f"{oidc_hostpath}:sub": subjects[0]},
        }
    elif shape == "flat_stringequals":
        condition = {
            "StringEquals": {
                f"{oidc_hostpath}:aud": "sts.amazonaws.com",
                f"{oidc_hostpath}:sub": subjects[0],
            },
        }
    else:
        raise ValueError(f"unknown IRSA condition shape: {shape!r}")
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": sid,
                "Effect": "Allow",
                "Principal": {"Federated": oidc_provider_arn},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": condition,
            }
        ],
    }


def _eks_deploy_assume_role_policy(runner_role_arn):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowGoldenGateRunnerRoleToAssume",
                "Effect": "Allow",
                "Principal": {"AWS": runner_role_arn},
                "Action": "sts:AssumeRole",
            }
        ],
    }


# Permission-content (policies_1.json) documents: built directly from derive_values(doc) plus these fixed, environment-neutral application constants (approved repository/table names) -- never by reading a previously generated policies_1.json as a template. Every approved Sid/Action/Condition below mirrors the currently-approved policy content exactly; only the environment-identity literals inside Resource/Condition values are ever derived.
_ECR_OCI_READ_ACTIONS = [
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "ecr:DescribeImages",
    "ecr:DescribeRepositories",
]

# (repository name, Sid) for every approved Helm OCI repository the argocd-ecr-token-sync CronJob reads -- application constants, never per-environment.
_ARGOCD_ECR_OCI_REPOSITORIES = [
    ("helm/goldengate", "AllowReadGoldengateHelmOciRepository"),
    ("helm/goldengate-monitor", "AllowReadGoldengateMonitorHelmOciRepository"),
    ("helm/goldengate-platform", "AllowReadGoldengatePlatformHelmOciRepository"),
    ("helm/gg-monitor", "AllowReadGgMonitorHelmOciRepository"),
    ("helm/amazon-cloudwatch-observability", "AllowReadAmazonCloudWatchObservabilityHelmOciRepository"),
]

# GoldenGate's one shared canonical pipeline-state/monitoring DynamoDB table (see envs/dev/dynamodb.tf) -- a fixed application constant, never per-environment.
_MONITOR_DYNAMODB_TABLE_NAME = "gg-eks-pipeline"


def _argocd_ecr_oci_read_policy(v):
    statements = [
        {
            "Sid": "AllowGetEcrAuthorizationToken",
            "Effect": "Allow",
            "Action": ["ecr:GetAuthorizationToken"],
            "Resource": "*",
        }
    ]
    for repo_name, sid in _ARGOCD_ECR_OCI_REPOSITORIES:
        statements.append({
            "Sid": sid,
            "Effect": "Allow",
            "Action": list(_ECR_OCI_READ_ACTIONS),
            "Resource": f"arn:aws:ecr:{v['AWS_REGION']}:{v['ECR_ACCOUNT_ID']}:repository/{repo_name}",
        })
    return {"Version": "2012-10-17", "Statement": statements}


def _cloudwatch_metrics_policy(v):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowPutContainerInsightsMetricData",
                "Effect": "Allow",
                "Action": ["cloudwatch:PutMetricData"],
                "Resource": "*",
                "Condition": {"StringEqualsIfExists": {"cloudwatch:namespace": "ContainerInsights"}},
            },
            {
                "Sid": "AllowWriteContainerInsightsPerformanceLogEvents",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:log-group:{v['CONTAINER_INSIGHTS_LOG_GROUP']}:*",
            },
            {
                "Sid": "AllowDescribeLogGroupsForContainerInsightsDiscovery",
                "Effect": "Allow",
                "Action": ["logs:DescribeLogGroups"],
                "Resource": "*",
            },
            {
                "Sid": "AllowEc2MetadataRequiredByCloudWatchAgent",
                "Effect": "Allow",
                "Action": ["ec2:DescribeTags", "ec2:DescribeVolumes"],
                "Resource": "*",
            },
        ],
    }


def _eks_deploy_policy(v):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowDescribeGoldenGateEksCluster",
                "Effect": "Allow",
                "Action": ["eks:DescribeCluster"],
                "Resource": v["EKS_CLUSTER_ARN"],
            },
            {
                "Sid": "AllowQueryGoldenGateLegacyInventoryDynamoDbTable",
                "Effect": "Allow",
                "Action": ["dynamodb:Query"],
                "Resource": f"arn:aws:dynamodb:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:table/{_MONITOR_DYNAMODB_TABLE_NAME}",
            },
            {
                "Sid": "AllowDescribeGoldenGateLegacyInventoryEfsResources",
                "Effect": "Allow",
                "Action": ["elasticfilesystem:DescribeAccessPoints", "elasticfilesystem:DescribeFileSystems"],
                "Resource": "*",
            },
            {
                "Sid": "AllowReadOnlyGoldenGateSharedSecretValidation",
                "Effect": "Allow",
                "Action": ["secretsmanager:DescribeSecret", "secretsmanager:ListSecretVersionIds"],
                "Resource": [
                    f"arn:aws:secretsmanager:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:secret:{v['SOURCE_ADMIN_SECRET_NAME']}-??????",
                    f"arn:aws:secretsmanager:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:secret:{v['TARGET_ADMIN_SECRET_NAME']}-??????",
                    f"arn:aws:secretsmanager:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:secret:{v['TLS_SECRET_NAME']}-??????",
                ],
            },
        ],
    }


def _monitor_read_policy(v):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowReadGoldenGateMonitorSecrets",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                "Resource": [f"arn:aws:secretsmanager:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:secret:{v['GG_ENVIRONMENT']}/goldengate/*"],
            },
            {
                "Sid": "AllowDecryptGoldenGateMonitorSecretsKms",
                "Effect": "Allow",
                "Action": ["kms:Decrypt"],
                "Resource": "*",
            },
            {
                "Sid": "AllowReadWriteGoldenGateMonitoringState",
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DescribeTable"],
                "Resource": f"arn:aws:dynamodb:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:table/{_MONITOR_DYNAMODB_TABLE_NAME}",
            },
            {
                "Sid": "AllowDecryptGoldenGateMonitoringTable",
                "Effect": "Allow",
                "Action": "kms:Decrypt",
                "Resource": v["MONITOR_DYNAMODB_KMS_KEY_ARN"],
                "Condition": {
                    "StringEquals": {
                        "kms:ViaService": f"dynamodb.{v['AWS_REGION']}.amazonaws.com",
                        "kms:CallerAccount": v["WORKLOAD_ACCOUNT_ID"],
                        "kms:EncryptionContext:aws:dynamodb:tableName": _MONITOR_DYNAMODB_TABLE_NAME,
                        "kms:EncryptionContext:aws:dynamodb:subscriberId": v["WORKLOAD_ACCOUNT_ID"],
                    },
                },
            },
            {
                "Sid": "AllowPublishGoldenGateMonitoringMetrics",
                "Effect": "Allow",
                "Action": ["cloudwatch:PutMetricData"],
                "Resource": "*",
                "Condition": {"StringEquals": {"cloudwatch:namespace": "GoldenGate/Pipelines"}},
            },
        ],
    }


def _platform_logging_policy(v):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowWriteGoldenGateContainerLogsToPreCreatedGroups",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutLogEvents"],
                "Resource": [
                    f"arn:aws:logs:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:log-group:{v['RUNTIME_LOG_GROUP']}:*",
                    f"arn:aws:logs:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:log-group:{v['MONITOR_LOG_GROUP']}:*",
                ],
            },
        ],
    }


def _secrets_read_policy(v):
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowReadGoldenGateDevSecrets",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                "Resource": [f"arn:aws:secretsmanager:{v['AWS_REGION']}:{v['WORKLOAD_ACCOUNT_ID']}:secret:{v['GG_ENVIRONMENT']}/goldengate/*"],
            },
            {
                "Sid": "AllowDecryptGoldenGateSecretsKms",
                "Effect": "Allow",
                "Action": ["kms:Decrypt"],
                "Resource": "*",
            },
        ],
    }


# (policy_folder -> builder(v)) for every generated policies_1.json -- the ONLY place permission-policy content is constructed; generate_policy_files() below never reads a generated file as input.
_PERMISSION_POLICY_BUILDERS = {
    "argocd-ecr-oci-read-dev": _argocd_ecr_oci_read_policy,
    "goldengate-cloudwatch-metrics-dev": _cloudwatch_metrics_policy,
    "goldengate-eks-deploy-dev": _eks_deploy_policy,
    "goldengate-monitor-read-dev": _monitor_read_policy,
    "goldengate-platform-logging-dev": _platform_logging_policy,
    "goldengate-secrets-read-dev": _secrets_read_policy,
}


def generate_policy_files(doc):
    """Returns {relative_path: parsed_json_dict} for every generated envs/dev/policies/** file, derived purely from environment.yaml (via derive_values) plus this module's fixed, reviewed permission-policy builders. Never reads a previously generated policies_1.json/sts.json as input -- deterministic and independent of prior output: calling this twice on the same environment.yaml input always produces byte-identical output, even after environment.yaml has changed multiple times in a row."""
    v = derive_values(doc)
    out = {}

    for folder, spec in _IRSA_ROLE_FOLDERS.items():
        out[f"envs/{doc['environment']}/policies/{folder}/assume_role_policy/sts.json"] = _irsa_assume_role_policy(
            spec["sid"], v["EKS_OIDC_PROVIDER_ARN"], v["EKS_OIDC_HOSTPATH"], spec["subjects"](v), spec["shape"]
        )

    out[f"envs/{doc['environment']}/policies/goldengate-eks-deploy-dev/assume_role_policy/sts.json"] = (
        _eks_deploy_assume_role_policy(v["RUNNER_ROLE_ARN"])
    )

    for folder, builder in _PERMISSION_POLICY_BUILDERS.items():
        rel = f"envs/{doc['environment']}/policies/{folder}/policies/policies_1.json"
        out[rel] = builder(v)

    return out


def render_iam_policies(environment, write):
    doc = load_environment_config(environment)
    generated = generate_policy_files(doc)
    mismatches = []
    for rel_path, content in generated.items():
        abs_path = os.path.join(REPO_ROOT, rel_path)
        rendered = json.dumps(content, indent=2) + "\n"
        current = None
        if os.path.isfile(abs_path):
            with open(abs_path) as f:
                current = f.read()
        if current != rendered:
            mismatches.append(rel_path)
            if write:
                with open(abs_path, "w") as f:
                    f.write(rendered)
    return mismatches


# --- CLI ---

def cmd_validate(args):
    try:
        load_environment_config(args.environment)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"OK: envs/{args.environment}/environment.yaml is valid")
    return 0


def cmd_get(args):
    try:
        doc = load_environment_config(args.environment)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    values = derive_values(doc)
    if args.field not in values:
        print(f"FAIL: unknown field {args.field!r}")
        return 1
    print(values[args.field])
    return 0


def cmd_github_env(args):
    try:
        doc = load_environment_config(args.environment)
        values = derive_values(doc)
        lines = format_github_env(values)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


def cmd_render_iam_policies(args):
    try:
        if args.check:
            mismatches = render_iam_policies(args.environment, write=False)
            if mismatches:
                print("FAIL: environment.yaml and generated IAM policies are out of sync.")
                for rel_path in sorted(mismatches):
                    print(f"  out of sync: {rel_path}")
                print("The correction command is:")
                print(f"  python3 automation/goldengate-environment.py --environment {args.environment} render-iam-policies --write")
                return 1
            print(f"OK: all generated IAM policies for {args.environment} are in sync with environment.yaml")
            return 0
        elif args.write:
            mismatches = render_iam_policies(args.environment, write=True)
            if mismatches:
                print(f"OK: regenerated {len(mismatches)} IAM policy file(s) from environment.yaml")
                for rel_path in sorted(mismatches):
                    print(f"  regenerated: {rel_path}")
            else:
                print(f"OK: all generated IAM policies for {args.environment} were already in sync")
            return 0
        else:
            print("FAIL: render-iam-policies requires --write or --check")
            return 1
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="dev")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate").set_defaults(func=cmd_validate)

    get_parser = sub.add_parser("get")
    get_parser.add_argument("field")
    get_parser.set_defaults(func=cmd_get)

    sub.add_parser("github-env").set_defaults(func=cmd_github_env)

    render_parser = sub.add_parser("render-iam-policies")
    render_parser.add_argument("--write", action="store_true")
    render_parser.add_argument("--check", action="store_true")
    render_parser.set_defaults(func=cmd_render_iam_policies)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
