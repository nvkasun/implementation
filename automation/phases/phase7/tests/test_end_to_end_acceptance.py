"""Offline tests for automation/orchestration/end_to_end_acceptance.py; run directly via `python3 automation/phases/phase7/tests/test_end_to_end_acceptance.py`. This classifier is pure/offline (no Kubernetes/AWS access at all) so tests call classify() directly with synthetic active-deployment lists and a synthetic captured /api/processes JSON document -- exactly the shape monitoring/monitor/monitor.py's build_processes_payload()/read_deployment_processes_view() actually produce. Exercises the classifier's actual logic (never merely greps its source)."""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[4])
TOOL_PATH = os.path.join(REPO_ROOT, "automation", "orchestration", "end_to_end_acceptance.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("end_to_end_acceptance", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


e2e = _load_tool()

ENVIRONMENT = "dev"

SOURCE_ID = "gg-postgresql-repltest-01"
TARGET_ID = "gg-mssql-repltest-01"


def _active_deployments(source_replication=True, target_replication=True):
    return [
        {"deploymentId": SOURCE_ID, "deploymentType": "postgresql", "replicationEnabled": source_replication},
        {"deploymentId": TARGET_ID, "deploymentType": "mssql", "replicationEnabled": target_replication},
    ]


def _healthy_deployment_entry(name, deployment_type, replication_enabled, processes=None, discovery_status="OK"):
    process_discovery = None
    if replication_enabled or discovery_status is not None:
        process_discovery = {
            "status": discovery_status,
            "collectedAt": 1_700_000_000,
            "extractCount": 1 if replication_enabled else 0,
            "replicatCount": 0,
            "distpathCount": 1 if replication_enabled else 0,
            "totalCount": 2 if replication_enabled else 0,
            "extractsStatus": "OK",
            "replicatsStatus": "OK",
            "sourcesStatus": "OK",
            "detailFailureCount": 0,
        }
    return {
        "deploymentName": name,
        "deploymentType": deployment_type,
        "enabled": True,
        "effectiveStatus": "UP",
        "recordedAt": 1_700_000_100,
        "ageSeconds": 5,
        "fresh": True,
        "lease": {"holder": "gg-monitor-abc123", "expiresAt": 1_700_000_200, "fresh": True},
        "criticalServices": {"admin": True},
        "processDiscovery": process_discovery,
        "processes": processes if processes is not None else [],
    }


def _healthy_api_doc():
    return {
        "generatedAt": 1_700_000_100,
        "deployments": [
            _healthy_deployment_entry(SOURCE_ID, "postgresql", True),
            _healthy_deployment_entry(TARGET_ID, "mssql", True),
        ],
    }


def _assert_broken(test, result, substring):
    test.assertEqual(result["state"], e2e.STATE_BROKEN)
    test.assertTrue(any(substring in r for r in result["reasons"]), f"expected a reason containing {substring!r}, got {result['reasons']!r}")


class EndToEndAcceptanceTests(unittest.TestCase):
    # 1. Exact match, everything healthy -> HEALTHY.
    def test_1_exact_match_is_healthy(self):
        result = e2e.classify(ENVIRONMENT, _active_deployments(), _healthy_api_doc())
        self.assertEqual(result["state"], e2e.STATE_HEALTHY)
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["checks"]["expected_deployment_count"], 2)
        self.assertEqual(result["checks"]["actual_deployment_count"], 2)

    # 2. No active deployments and empty API response -> HEALTHY (trivially).
    def test_2_no_active_deployments_is_healthy(self):
        result = e2e.classify(ENVIRONMENT, [], {"generatedAt": 1, "deployments": []})
        self.assertEqual(result["state"], e2e.STATE_HEALTHY)

    # 3. Malformed /api/processes (missing 'deployments' key) -> BROKEN.
    def test_3_malformed_api_response_is_broken(self):
        result = e2e.classify(ENVIRONMENT, _active_deployments(), {"generatedAt": 1})
        _assert_broken(self, result, "missing a 'deployments' list")

    # B3B closeout Issue 4: a malformed deployment row must never be silently discarded -- it is itself a BROKEN condition, even when the rest of the inventory is exactly correct.
    def test_healthy_inventory_plus_non_object_row_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"].append({"foo": "bar"})
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "is missing deploymentName")
        # The exact-inventory checks (missing/extra) must not be fooled into HEALTHY just because the malformed row happens to add no new name.
        self.assertNotIn("missing expected ACTIVE deployment(s)", " ".join(result["reasons"]))

    def test_healthy_inventory_plus_null_deployment_name_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"].append({"deploymentName": None})
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "is missing deploymentName")

    def test_healthy_inventory_plus_empty_deployment_name_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"].append({"deploymentName": ""})
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "has an empty deploymentName")

    def test_healthy_inventory_plus_integer_deployment_name_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"].append({"deploymentName": 12345})
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "has a non-string deploymentName")

    def test_healthy_inventory_plus_non_object_row_at_list_index_is_reported_with_index(self):
        doc = _healthy_api_doc()
        doc["deployments"].append("not-an-object")
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, f"deployment row #{len(doc['deployments']) - 1} is not an object")

    def test_exact_valid_inventory_with_no_malformed_rows_remains_healthy(self):
        # Positive control: proves the new malformed-row validation does not itself introduce a false positive against the existing exact, valid inventory.
        result = e2e.classify(ENVIRONMENT, _active_deployments(), _healthy_api_doc())
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # 4. Missing expected ACTIVE deployment -> BROKEN.
    def test_4_missing_expected_deployment_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"] = doc["deployments"][:1]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "missing expected ACTIVE deployment(s)")

    # 5. Extra/stale deployment not in the current active inventory -> BROKEN.
    def test_5_extra_stale_deployment_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"].append(_healthy_deployment_entry("gg-decommissioned-01", "postgresql", False, discovery_status=None))
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "unexpected/stale deployment(s)")

    # 6. Duplicate deploymentName in the API response -> BROKEN.
    def test_6_duplicate_deployment_name_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"].append(_healthy_deployment_entry(SOURCE_ID, "postgresql", True))
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "duplicate deploymentName")

    # 7. Wrong deploymentType -> BROKEN.
    def test_7_wrong_deployment_type_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["deploymentType"] = "mssql"
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "deploymentType=")

    # 8. enabled != true -> BROKEN.
    def test_8_enabled_false_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["enabled"] = False
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "enabled=False")

    # 9. effectiveStatus DOWN -> BROKEN.
    def test_9_effective_status_down_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["effectiveStatus"] = "DOWN"
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "effectiveStatus='DOWN'")

    # 10. effectiveStatus STALE -> BROKEN.
    def test_10_effective_status_stale_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["effectiveStatus"] = "STALE"
        doc["deployments"][0]["fresh"] = False
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "effectiveStatus='STALE'")

    # 11. effectiveStatus MISSING -> BROKEN.
    def test_11_effective_status_missing_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["effectiveStatus"] = "MISSING"
        doc["deployments"][0]["fresh"] = False
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "effectiveStatus='MISSING'")

    # 12. effectiveStatus UNKNOWN -> BROKEN.
    def test_12_effective_status_unknown_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["effectiveStatus"] = "UNKNOWN"
        doc["deployments"][0]["fresh"] = False
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "effectiveStatus='UNKNOWN'")

    # 13. fresh=false (even if effectiveStatus somehow still reported UP) -> BROKEN.
    def test_13_fresh_false_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["fresh"] = False
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "fresh=False")

    # 14. ageSeconds negative -> BROKEN.
    def test_14_age_seconds_negative_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["ageSeconds"] = -5
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "is not a sane non-negative integer")

    # 15. ageSeconds None -> BROKEN.
    def test_15_age_seconds_none_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["ageSeconds"] = None
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "is not a sane non-negative integer")

    # 16. lease missing (None) -> BROKEN.
    def test_16_lease_missing_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["lease"] = None
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "no current lease ownership recorded")

    # 17. lease.fresh=false -> BROKEN.
    def test_17_lease_not_fresh_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["lease"]["fresh"] = False
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "lease.fresh=False")

    # 18. lease.holder empty -> BROKEN.
    def test_18_lease_holder_empty_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["lease"]["holder"] = ""
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "lease.holder is empty")

    # 19. criticalServices empty -> BROKEN.
    def test_19_critical_services_empty_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["criticalServices"] = {}
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "criticalServices is empty")

    # 20. criticalServices unreachable -> BROKEN.
    def test_20_critical_service_unreachable_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["criticalServices"] = {"admin": False}
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "critical service(s) not reachable")

    # 21. Non-replication deployment, processDiscovery.status=EMPTY -> HEALTHY (no replication process desired).
    def test_21_non_replication_discovery_empty_is_healthy(self):
        active = _active_deployments(source_replication=False, target_replication=False)
        doc = _healthy_api_doc()
        doc["deployments"][0]["processDiscovery"]["status"] = "EMPTY"
        doc["deployments"][1]["processDiscovery"]["status"] = "EMPTY"
        result = e2e.classify(ENVIRONMENT, active, doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # 22. Non-replication deployment, processDiscovery=None (never reported) -> HEALTHY.
    def test_22_non_replication_discovery_absent_is_healthy(self):
        active = _active_deployments(source_replication=False, target_replication=False)
        doc = _healthy_api_doc()
        doc["deployments"][0]["processDiscovery"] = None
        doc["deployments"][1]["processDiscovery"] = None
        result = e2e.classify(ENVIRONMENT, active, doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # 23. Non-replication deployment, processDiscovery.status=PARTIAL -> BROKEN (never PARTIAL/UNAVAILABLE/INVALID_RESPONSE).
    def test_23_non_replication_discovery_partial_is_broken(self):
        active = _active_deployments(source_replication=False, target_replication=False)
        doc = _healthy_api_doc()
        doc["deployments"][0]["processDiscovery"]["status"] = "PARTIAL"
        result = e2e.classify(ENVIRONMENT, active, doc)
        _assert_broken(self, result, "replication is not enabled but processDiscovery.status='PARTIAL'")

    # 24. Replication-enabled deployment, processDiscovery.status=OK -> HEALTHY.
    def test_24_replication_enabled_discovery_ok_is_healthy(self):
        result = e2e.classify(ENVIRONMENT, _active_deployments(), _healthy_api_doc())
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # 25. Replication-enabled deployment, processDiscovery.status=EMPTY (not OK) -> BROKEN.
    def test_25_replication_enabled_discovery_empty_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processDiscovery"]["status"] = "EMPTY"
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "participates in enabled replication but processDiscovery.status='EMPTY'")

    # 26. Replication-enabled deployment, processDiscovery=None -> BROKEN.
    def test_26_replication_enabled_discovery_absent_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processDiscovery"] = None
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "processDiscovery.status=None")

    # 27. Process row stale=true -> BROKEN.
    def test_27_process_row_stale_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "RUNNING", "stale": True}]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "process 'EXT01': stale=true")

    # 28. Process row status=ABENDED -> BROKEN.
    def test_28_process_row_abended_is_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "ABENDED", "stale": False}]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "process 'EXT01': status=ABENDED")

    # 29. Process row present, RUNNING, not stale -> HEALTHY (no requirement that every arbitrary row be RUNNING beyond this -- that's replication_monitor_acceptance's job).
    def test_29_process_row_running_not_stale_is_healthy(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "RUNNING", "stale": False}]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # 30. Process row present, STOPPED (not ABENDED, not stale) -> HEALTHY -- this tool never requires every row to be RUNNING.
    def test_30_process_row_stopped_not_flagged_is_healthy(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "STOPPED", "stale": False}]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # 31. Invalid folder-driven model -> raises ValueError (load_active_deployments), not silently BROKEN.
    def test_31_invalid_model_raises_value_error(self):
        gdm = e2e._load_deployment_model_module()
        original = gdm._run_full_validation
        try:
            gdm._run_full_validation = lambda environment: ([], [], [], {"synthetic problem"})
            with self.assertRaises(ValueError):
                e2e.load_active_deployments(ENVIRONMENT)
        finally:
            gdm._run_full_validation = original


class MalformedSchemaFailClosedTests(unittest.TestCase):
    """Pre-VDR correction: automation/orchestration/end_to_end_acceptance.py previously fail-opened on malformed monitor API schema -- a non-dict processDiscovery silently became None, a non-list/falsey processes container silently became [], and an empty/malformed process row added no failure reason at all. These tests exercise the REAL classify() against the exact independently-reproduced malformed payload and its schema-validation edges -- never a re-implementation/mock of the classifier's own logic."""

    def _active_non_replication_deployments(self):
        return _active_deployments(source_replication=False, target_replication=False)

    # A. Exact reproduction: the independent review's exact malformed payload against the current two real replication-disabled active deployments must be BROKEN, never HEALTHY.
    def test_A_exact_reproduction_payload_is_broken_not_healthy(self):
        doc = {
            "generatedAt": 1_700_000_100,
            "deployments": [
                {
                    "deploymentName": SOURCE_ID,
                    "deploymentType": "postgresql",
                    "enabled": True,
                    "effectiveStatus": "UP",
                    "ageSeconds": 5,
                    "fresh": True,
                    "lease": {"holder": "gg-monitor-x", "fresh": True},
                    "criticalServices": {"admin": True},
                    "processDiscovery": "MALFORMED-DISCOVERY",
                    "processes": {},
                },
                {
                    "deploymentName": TARGET_ID,
                    "deploymentType": "mssql",
                    "enabled": True,
                    "effectiveStatus": "UP",
                    "ageSeconds": 5,
                    "fresh": True,
                    "lease": {"holder": "gg-monitor-x", "fresh": True},
                    "criticalServices": {"admin": True},
                    "processDiscovery": None,
                    "processes": [{}],
                },
            ],
        }
        result = e2e.classify(ENVIRONMENT, self._active_non_replication_deployments(), doc)
        self.assertEqual(result["state"], e2e.STATE_BROKEN, "malformed monitor API payload must never be accepted as HEALTHY")
        self.assertNotEqual(result["reasons"], [])

    # B. malformed processDiscovery (a non-dict, non-null value) must never be silently coerced into "absent".
    def test_B_malformed_process_discovery_is_broken(self):
        for bad_value in ("BAD", [], 123, True):
            with self.subTest(bad_value=bad_value):
                doc = _healthy_api_doc()
                doc["deployments"][0]["processDiscovery"] = bad_value
                result = e2e.classify(ENVIRONMENT, self._active_non_replication_deployments(), doc)
                _assert_broken(self, result, "processDiscovery must be null or an object")

    # C. a legitimately absent processDiscovery (None) for a replication-disabled deployment remains HEALTHY when everything else is healthy.
    def test_C_legitimate_process_discovery_none_remains_healthy(self):
        active = self._active_non_replication_deployments()
        doc = _healthy_api_doc()
        doc["deployments"][0]["processDiscovery"] = None
        doc["deployments"][1]["processDiscovery"] = None
        result = e2e.classify(ENVIRONMENT, active, doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # D. malformed/falsey processes containers must never be silently coerced into [].
    def test_D_malformed_processes_container_is_broken(self):
        for bad_value in ({}, "", 0, False, None):
            with self.subTest(bad_value=bad_value):
                doc = _healthy_api_doc()
                doc["deployments"][0]["processes"] = bad_value
                result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
                _assert_broken(self, result, "processes must be a JSON array")

    # D2. a MISSING 'processes' key (the current monitor API always includes it) is malformed too, never a silent empty default.
    def test_D2_missing_processes_key_is_broken(self):
        doc = _healthy_api_doc()
        del doc["deployments"][0]["processes"]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "missing the required 'processes' field")

    # E. an actual empty process list remains valid.
    def test_E_actual_empty_process_list_is_healthy(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = []
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # F. every non-object process row must be its own BROKEN condition, never silently discarded.
    def test_F_non_object_process_rows_are_broken(self):
        for bad_row in (None, "BAD", [], 123, True):
            with self.subTest(bad_row=bad_row):
                doc = _healthy_api_doc()
                doc["deployments"][0]["processes"] = [bad_row]
                result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
                _assert_broken(self, result, "process row #0 is not an object")

    # G. every process row must carry a non-empty string process identity.
    def test_G_missing_or_malformed_process_identity_is_broken(self):
        bad_rows = (
            {},
            {"process": ""},
            {"process": 123, "status": "RUNNING", "stale": False},
            {"process": None, "status": "RUNNING", "stale": False},
        )
        for bad_row in bad_rows:
            with self.subTest(bad_row=bad_row):
                doc = _healthy_api_doc()
                doc["deployments"][0]["processes"] = [bad_row]
                result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
                _assert_broken(self, result, "invalid 'process' identity")

    # H. stale must be a literal Boolean -- missing/non-Boolean values are malformed, never silently treated as falsey/not-stale.
    def test_H_malformed_stale_field_is_broken(self):
        for bad_stale in (None, "true", 1, "false", 0):
            with self.subTest(bad_stale=bad_stale):
                doc = _healthy_api_doc()
                doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "RUNNING", "stale": bad_stale}]
                result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
                _assert_broken(self, result, "stale is not a boolean")

    # H (continued). stale=true remains BROKEN (existing rule preserved); stale=false triggers normal evaluation (existing rule preserved).
    def test_H_stale_true_still_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "RUNNING", "stale": True}]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "stale=true")

    def test_H_stale_false_triggers_normal_evaluation(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "RUNNING", "stale": False}]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # I. status must be a non-empty string -- missing/null/non-string/empty values are malformed.
    def test_I_malformed_status_field_is_broken(self):
        for bad_status in (None, 123, "", True, [1, 2]):
            with self.subTest(bad_status=bad_status):
                doc = _healthy_api_doc()
                doc["deployments"][0]["processes"] = [{"process": "EXT01", "stale": False, "status": bad_status}]
                result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
                _assert_broken(self, result, "status is not a non-empty string")

    # I (continued). status=ABENDED remains BROKEN; status=RUNNING/STOPPED (not stale) remain the existing generic accepted states -- this correction is schema validation, not a change to GoldenGate process business semantics.
    def test_I_status_abended_still_broken(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "ABENDED", "stale": False}]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        _assert_broken(self, result, "status=ABENDED")

    def test_I_status_running_not_stale_remains_healthy(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "RUNNING", "stale": False}]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    def test_I_status_stopped_not_stale_remains_healthy(self):
        doc = _healthy_api_doc()
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "STOPPED", "stale": False}]
        result = e2e.classify(ENVIRONMENT, _active_deployments(), doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    # J. existing normal healthy payloads (both replication-enabled and replication-disabled) must remain HEALTHY -- positive control proving this correction is schema validation, not a new false positive.
    def test_J_existing_healthy_payload_remains_healthy(self):
        result = e2e.classify(ENVIRONMENT, _active_deployments(), _healthy_api_doc())
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])

    def test_J_existing_healthy_non_replication_payload_remains_healthy(self):
        active = self._active_non_replication_deployments()
        doc = _healthy_api_doc()
        doc["deployments"][0]["processDiscovery"] = None
        doc["deployments"][1]["processDiscovery"] = None
        doc["deployments"][0]["processes"] = [{"process": "EXT01", "status": "RUNNING", "stale": False}]
        result = e2e.classify(ENVIRONMENT, active, doc)
        self.assertEqual(result["state"], e2e.STATE_HEALTHY, result["reasons"])


class EndToEndAcceptanceNoIOTests(unittest.TestCase):
    """Static source-safety proof: classify() itself never performs I/O, network access, or a mutating/kubectl/helm call -- this tool is offline/pure by construction."""

    FORBIDDEN_SUBSTRINGS = (
        "kubectl", "helm ", "subprocess", "urllib.request", "requests.get", "http.client", "boto3",
    )

    def test_classify_function_source_contains_no_io_or_cluster_access(self):
        import inspect
        source = inspect.getsource(e2e.classify)
        hits = [s for s in self.FORBIDDEN_SUBSTRINGS if s in source]
        self.assertEqual(hits, [], f"classify() contains an I/O/cluster-access-looking construct: {hits}")


if __name__ == "__main__":
    unittest.main()
