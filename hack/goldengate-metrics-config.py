"""goldengate-metrics-config.py: exact, conditional CONFIG.metricsEnabled
update helper for a single canonical GoldenGate deployment.

Phase 6C1. This file is never imported -- it is piped as source into a
`python3 -` process running INSIDE the existing, authenticated, Ready
gg-monitor pod (IRSA role GoldenGateMonitorReadRole-dev already grants the
DynamoDB GetItem/UpdateItem this needs on gg-eks-pipeline; no new IAM
permission is required or added). It intentionally has no imports beyond the
standard library plus boto3 (already present in the monitor image), reads
AWS_REGION/DYNAMODB_TABLE from the pod's existing environment, and accepts
exactly four positional arguments -- nothing else:

    python3 - <deployment> <canonical_type> <true|false> <true|false> < this_file

    argv[1]  deployment name (DynamoDB partition key "pipeline")
    argv[2]  canonical deployment type (must match CONFIG.deploymentType)
    argv[3]  desired metricsEnabled value -- literal "true" or "false"
    argv[4]  apply_change -- literal "true" (perform the UpdateItem if
             needed) or "false" (dry run: validate and report only)

Never uses Scan. Never reads or prints the complete CONFIG item, an AWS
credential, or a web-identity token. Only ever updates the single attribute
metricsEnabled -- deploymentType, alertsEnabled, and every other attribute
are read for validation only and are never written by this script.

Phase 6C1 correction: every GetItem (the initial read and the post-update
verification read) uses ConsistentRead=True -- these are low-volume
control-plane reads, at most a couple per invocation, and a rollout decision
must never be made from an eventually consistent value. UpdateItem uses
ReturnValues="ALL_NEW" and the returned attributes are validated directly, in
addition to (not instead of) the separate strongly consistent GetItem
verification that follows. A small bounded retry applies ONLY to the
post-update verification read, and only for transport-level failures
(throttling/internal-server/timeout-shaped codes) -- a
ConditionalCheckFailedException from UpdateItem is never retried, and this
script issues at most one UpdateItem per invocation.
"""
from __future__ import annotations

import os
import sys
import time


def _parse_strict_bool_arg(raw, label):
    """Only the exact strings "true"/"false" are accepted -- matches
    collector.py's own fail-closed Boolean parsing convention. Anything
    else (empty, "True", "1", "yes", ...) is a usage error."""
    if raw == "true":
        return True
    if raw == "false":
        return False
    sys.exit(f"USAGE ERROR: {label} must be exactly \"true\" or \"false\", got {raw!r}")


def main():
    if len(sys.argv) != 5:
        sys.exit(
            "USAGE ERROR: expected exactly 4 arguments: "
            "<deployment> <canonical_type> <desired_metrics_enabled:true|false> <apply_change:true|false>"
        )

    deployment = sys.argv[1]
    canonical_type = sys.argv[2]
    desired_metrics_enabled = _parse_strict_bool_arg(sys.argv[3], "desired_metrics_enabled")
    apply_change = _parse_strict_bool_arg(sys.argv[4], "apply_change")

    if not deployment:
        sys.exit("USAGE ERROR: deployment must be non-empty.")
    if not canonical_type:
        sys.exit("USAGE ERROR: canonical_type must be non-empty.")

    import boto3
    from boto3.dynamodb.conditions import Attr
    from botocore.exceptions import ClientError

    region = os.environ.get("AWS_REGION", "eu-west-1")
    table_name = os.environ.get("DYNAMODB_TABLE")
    if not table_name:
        sys.exit("FAIL: DYNAMODB_TABLE is not set in this pod's environment.")

    table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    TRANSPORT_ERROR_CODES = {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "InternalServerError",
        "RequestLimitExceeded",
        "ServiceUnavailable",
    }

    def _consistent_verification_get_item(max_attempts=3):
        # Bounded retry for the post-update verification read ONLY, and
        # only for transport-shaped failures -- never used for the initial
        # read, and never used to retry a ConditionalCheckFailedException
        # from UpdateItem (that is handled separately below and is never
        # retried).
        for attempt in range(1, max_attempts + 1):
            try:
                return table.get_item(
                    Key={"pipeline": deployment, "recordType": "CONFIG"},
                    ConsistentRead=True,
                ).get("Item")
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
                if code not in TRANSPORT_ERROR_CODES or attempt == max_attempts:
                    raise
                time.sleep(0.5 * attempt)

    # 1. Read the current CONFIG item (GetItem only, never Scan; strongly
    # consistent -- this control-plane read directly drives the rollout
    # decision below and must never be based on a stale replica).
    try:
        item = table.get_item(
            Key={"pipeline": deployment, "recordType": "CONFIG"},
            ConsistentRead=True,
        ).get("Item")
    except ClientError as exc:
        sys.exit(f"FAIL: DynamoDB GetItem failed: {exc.response.get('Error', {}).get('Code', type(exc).__name__)}")

    if not isinstance(item, dict):
        sys.exit(f"FAIL: CONFIG item does not exist for deployment={deployment}.")
    if item.get("recordType") != "CONFIG":
        sys.exit(f"FAIL: unexpected recordType for deployment={deployment}.")
    if item.get("deploymentType") != canonical_type:
        sys.exit(
            f"FAIL: deploymentType mismatch for deployment={deployment} "
            f"(CONFIG has a different value than the canonical registry)."
        )

    current_metrics_enabled = item.get("metricsEnabled")
    alerts_enabled = item.get("alertsEnabled")

    if not isinstance(current_metrics_enabled, bool):
        sys.exit(f"FAIL: CONFIG.metricsEnabled for deployment={deployment} is not a literal Boolean.")
    if alerts_enabled is not False:
        sys.exit(f"FAIL: CONFIG.alertsEnabled for deployment={deployment} is not the literal Boolean false.")

    def report(action):
        # Only ever these six safe fields -- never the complete item.
        print(f"deployment={deployment}")
        print(f"deploymentType={canonical_type}")
        print(f"currentMetricsEnabled={'true' if current_metrics_enabled else 'false'}")
        print(f"desiredMetricsEnabled={'true' if desired_metrics_enabled else 'false'}")
        print("alertsEnabled=false")
        print(f"action={action}")

    if not apply_change:
        # Dry run: validation already passed above -- report the proposed
        # action only, no UpdateItem, exit successfully.
        action = "none" if current_metrics_enabled == desired_metrics_enabled else "plan"
        report(action)
        return

    if current_metrics_enabled == desired_metrics_enabled:
        # Idempotent success: already at the desired value, no UpdateItem.
        report("none")
        return

    # 2. Conditional UpdateItem -- metricsEnabled only, guarded by an
    # optimistic-concurrency condition on every field this decision was
    # based on, so an unnoticed concurrent change (by anyone/anything else)
    # between the GetItem above and this UpdateItem is rejected rather than
    # silently overwritten.
    condition = (
        Attr("pipeline").exists()
        & Attr("recordType").eq("CONFIG")
        & Attr("deploymentType").eq(canonical_type)
        & Attr("alertsEnabled").eq(False)
        & Attr("metricsEnabled").eq(current_metrics_enabled)
    )
    # This is the ONLY UpdateItem call site in this script -- at most one
    # conditional write is ever issued per invocation, and a
    # ConditionalCheckFailedException below is never retried/re-attempted.
    try:
        update_response = table.update_item(
            Key={"pipeline": deployment, "recordType": "CONFIG"},
            UpdateExpression="SET metricsEnabled = :desired",
            ConditionExpression=condition,
            ExpressionAttributeValues={":desired": desired_metrics_enabled},
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", type(exc).__name__)
        if code == "ConditionalCheckFailedException":
            sys.exit(
                f"FAIL: ConditionalCheckFailedException for deployment={deployment} -- "
                "the CONFIG item changed concurrently between read and update. "
                "Not retrying automatically; re-run this workflow to re-validate and try again."
            )
        sys.exit(f"FAIL: DynamoDB UpdateItem failed: {code}")

    # 3. Validate the ALL_NEW attributes DynamoDB returned with the
    # UpdateItem response itself -- before ever issuing a second read.
    new_attributes = update_response.get("Attributes")
    if not isinstance(new_attributes, dict):
        sys.exit(f"FAIL: UpdateItem for deployment={deployment} did not return ALL_NEW attributes.")
    if new_attributes.get("metricsEnabled") is not desired_metrics_enabled:
        sys.exit(
            f"FAIL: UpdateItem ALL_NEW attributes show metricsEnabled="
            f"{new_attributes.get('metricsEnabled')!r}, expected {desired_metrics_enabled!r}."
        )
    if new_attributes.get("alertsEnabled") is not False:
        sys.exit("FAIL: UpdateItem ALL_NEW attributes show alertsEnabled is no longer the literal Boolean false.")
    if new_attributes.get("deploymentType") != canonical_type:
        sys.exit("FAIL: UpdateItem ALL_NEW attributes show deploymentType changed unexpectedly.")

    # 4. A second, independent, strongly consistent GetItem verification
    # (bounded retry for transport failures only -- see
    # _consistent_verification_get_item above).
    try:
        verify_item = _consistent_verification_get_item()
    except ClientError as exc:
        sys.exit(f"FAIL: post-update DynamoDB GetItem failed: {exc.response.get('Error', {}).get('Code', type(exc).__name__)}")

    if not isinstance(verify_item, dict):
        sys.exit(f"FAIL: post-update verification found no CONFIG item for deployment={deployment}.")
    if verify_item.get("metricsEnabled") is not desired_metrics_enabled:
        sys.exit(
            f"FAIL: post-update verification shows metricsEnabled="
            f"{verify_item.get('metricsEnabled')!r}, expected {desired_metrics_enabled!r}."
        )
    if verify_item.get("alertsEnabled") is not False:
        sys.exit("FAIL: post-update verification shows alertsEnabled is no longer the literal Boolean false.")
    if verify_item.get("deploymentType") != canonical_type:
        sys.exit("FAIL: post-update verification shows deploymentType changed unexpectedly.")

    current_metrics_enabled = desired_metrics_enabled
    report("updated")


if __name__ == "__main__":
    main()
