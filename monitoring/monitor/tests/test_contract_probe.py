"""Synthetic-only tests for tools/gg_api_contract_probe.py: canonical
deployment resolution, port selection, unsafe-path/deployment rejection,
sanitized structural output, closed error-category classification, and
proof that no DynamoDB write, CloudWatch call, or GoldenGate-modifying
request is ever made. No real credentials or corporate hostnames are used
anywhere in this file."""
import json
import os
import ssl
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import collector as core  # noqa: E402
import config as cfgmod  # noqa: E402
import gg_api_contract_probe as probe  # noqa: E402

SYNTHETIC_HOST_SUFFIX = "svc.cluster.local"
SYNTHETIC_USER = "synthetic-test-oggadmin"
SYNTHETIC_PASSWORD = "synthetic-test-P@ssw0rd!"


def _deployment(name="gg-oracle-payments-01", enabled=True, dtype="oracle"):
    return {
        "name": name,
        "type": dtype,
        "pipeline": "payments-ora-to-pg-001",
        "role": "source",
        "enabled": enabled,
        "adminSecret": "some-secret-name",
        "adminHost": f"{name}.goldengate-dev.{SYNTHETIC_HOST_SUFFIX}",
        "adminPort": 8443,
        "tlsServerName": f"{name}.goldengate-dev.example-internal",
        "metricsPort": 9015,
    }


class PathValidationTests(unittest.TestCase):
    def test_accepts_confirmed_paths(self):
        for path in ("/services/v2/extracts", "/services/v2/replicats", "/services/v2/sources"):
            self.assertEqual(probe.validate_path(path), path)

    def test_accepts_explicit_unconfirmed_metrics_path(self):
        # allowed when the operator passes it explicitly -- never a default.
        self.assertEqual(probe.validate_path("/services/v2/metrics"), "/services/v2/metrics")

    def test_rejects_url_with_scheme(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("https://evil.example/services/v2/extracts")

    def test_rejects_host_smuggled_via_double_slash(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("//evil.example/services/v2/extracts")

    def test_rejects_query_parameters(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/v2/extracts?x=1")

    def test_rejects_fragment(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/v2/extracts#frag")

    def test_rejects_non_services_path(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/etc/passwd")

    def test_rejects_empty_path(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("")

    def test_rejects_whitespace(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/v2/ext racts")

    def test_rejects_dot_dot_traversal(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/../admin")

    def test_rejects_dot_segment(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/./v2/metrics")

    def test_rejects_percent_encoded_dot_dot(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/%2e%2e/admin")

    def test_rejects_percent_encoded_dot_dot_uppercase(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/%2E%2E/admin")

    def test_rejects_percent_encoded_double_slash(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/%2F%2Fevil")

    def test_rejects_percent_encoded_backslash(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/v2/%5cextracts")

    def test_rejects_backslash_path_separator(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services\\v2\\metrics")

    def test_rejects_literal_control_character(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/v2/ex\x00tracts")

    def test_rejects_percent_encoded_control_character(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/v2/%00extracts")

    def test_rejects_double_percent_encoded_traversal(self):
        # %252e%252e decodes once to "%2e%2e", then again to ".." -- bounded
        # iterative decoding must still catch this.
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/v2/%252e%252e/admin")

    def test_rejects_malformed_percent_encoding(self):
        # %ff is not valid standalone UTF-8 -- unquote(errors="strict") must
        # raise, and validate_path must turn that into a rejection.
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/v2/%ffextracts")

    def test_rejects_non_normalized_trailing_slash(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services/v2/extracts/")

    def test_rejects_duplicate_slashes(self):
        with self.assertRaises(probe.ProbeValidationError):
            probe.validate_path("/services//v2/extracts")

    def test_still_accepts_other_legitimate_services_paths(self):
        # no broad allowlist: any other well-formed /services/... path
        # remains probeable for future legitimate read-only endpoints.
        self.assertEqual(probe.validate_path("/services/v2/deployments"), "/services/v2/deployments")


class DeploymentResolutionTests(unittest.TestCase):
    def _with_doc(self, deployments):
        return {"environment": "dev", "runtimeNamespace": "goldengate-dev",
                "monitoringNamespace": "goldengate-monitoring", "dnsDomain": "example-internal",
                "deployments": deployments}

    def test_resolves_enabled_deployment(self):
        doc = self._with_doc([_deployment()])
        with mock.patch.object(cfgmod, "load_deployments", return_value=doc):
            resolved = probe.resolve_deployment("gg-oracle-payments-01")
        self.assertEqual(resolved["name"], "gg-oracle-payments-01")

    def test_rejects_unknown_deployment(self):
        doc = self._with_doc([_deployment()])
        with mock.patch.object(cfgmod, "load_deployments", return_value=doc):
            with self.assertRaises(probe.ProbeValidationError):
                probe.resolve_deployment("gg-does-not-exist")

    def test_rejects_disabled_deployment(self):
        doc = self._with_doc([_deployment(enabled=False)])
        with mock.patch.object(cfgmod, "load_deployments", return_value=doc):
            with self.assertRaises(probe.ProbeValidationError):
                probe.resolve_deployment("gg-oracle-payments-01")


class PortSelectionTests(unittest.TestCase):
    def test_admin_port_selected(self):
        d = _deployment()
        base = probe._port_and_base(d, "admin")
        self.assertTrue(base.endswith(":8443"))
        self.assertIn(d["adminHost"], base)

    def test_admin_port_uses_https(self):
        # the confirmed secure PMS route: HTTPS + authenticated.
        d = _deployment()
        self.assertTrue(probe._port_and_base(d, "admin").startswith("https://"))

    def test_metrics_port_selected(self):
        d = _deployment()
        base = probe._port_and_base(d, "metrics")
        self.assertTrue(base.endswith(":9015"))
        self.assertIn(d["adminHost"], base)

    def test_metrics_port_uses_plain_http_not_https(self):
        # live-confirmed: metricsPort 9015 is plain HTTP -- never HTTPS.
        d = _deployment()
        self.assertTrue(probe._port_and_base(d, "metrics").startswith("http://"))
        self.assertFalse(probe._port_and_base(d, "metrics").startswith("https://"))


class ErrorClassificationTests(unittest.TestCase):
    def test_auth_failed_401(self):
        self.assertEqual(probe._classify_request_error(Exception(), http_status=401), "AUTH_FAILED")

    def test_auth_failed_403(self):
        self.assertEqual(probe._classify_request_error(Exception(), http_status=403), "AUTH_FAILED")

    def test_tls_failed(self):
        self.assertEqual(probe._classify_request_error(ssl.SSLError("bad cert")), "TLS_FAILED")

    def test_not_found_404(self):
        self.assertEqual(probe._classify_request_error(Exception(), http_status=404), "NOT_FOUND")

    def test_endpoint_unavailable_5xx(self):
        self.assertEqual(probe._classify_request_error(Exception(), http_status=502), "ENDPOINT_UNAVAILABLE")

    def test_endpoint_unavailable_connection_error(self):
        self.assertEqual(
            probe._classify_request_error(urllib.error.URLError("connection refused")), "ENDPOINT_UNAVAILABLE")

    def test_unknown_fallback(self):
        self.assertEqual(probe._classify_request_error(ValueError("something else")), "UNKNOWN")


class WrappedTlsClassificationTests(unittest.TestCase):
    """A TLS failure must classify as TLS_FAILED regardless of how deep it is
    wrapped -- direct, URLError.reason, or chained __cause__/__context__."""

    def test_direct_ssl_error(self):
        self.assertTrue(probe._contains_tls_error(ssl.SSLError("bad cert")))
        self.assertEqual(probe._classify_request_error(ssl.SSLError("bad cert")), "TLS_FAILED")

    def test_direct_ssl_cert_verification_error(self):
        exc = ssl.SSLCertVerificationError("certificate verify failed")
        self.assertTrue(probe._contains_tls_error(exc))
        self.assertEqual(probe._classify_request_error(exc), "TLS_FAILED")

    def test_urlerror_wrapping_ssl_cert_verification_error(self):
        wrapped = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        self.assertTrue(probe._contains_tls_error(wrapped))
        self.assertEqual(probe._classify_request_error(wrapped), "TLS_FAILED")

    def test_urlerror_wrapping_connection_refused_is_not_tls(self):
        wrapped = urllib.error.URLError(ConnectionRefusedError("connection refused"))
        self.assertFalse(probe._contains_tls_error(wrapped))
        self.assertEqual(probe._classify_request_error(wrapped), "ENDPOINT_UNAVAILABLE")

    def test_nested_cause_chain(self):
        try:
            try:
                raise ssl.SSLCertVerificationError("certificate verify failed")
            except ssl.SSLCertVerificationError as inner:
                raise RuntimeError("connection failed") from inner
        except RuntimeError as outer:
            self.assertTrue(probe._contains_tls_error(outer))
            self.assertEqual(probe._classify_request_error(outer), "TLS_FAILED")

    def test_nested_context_chain_without_explicit_cause(self):
        try:
            try:
                raise ssl.SSLError("bad handshake")
            except ssl.SSLError:
                raise urllib.error.URLError("generic failure")  # implicit __context__
        except urllib.error.URLError as outer:
            self.assertTrue(probe._contains_tls_error(outer))
            self.assertEqual(probe._classify_request_error(outer), "TLS_FAILED")

    def test_cycle_protection_does_not_hang(self):
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a  # cycle
        # must terminate promptly and simply report no TLS error found
        self.assertFalse(probe._contains_tls_error(a))

    def test_bounded_traversal_does_not_hang_on_long_chain(self):
        root = ssl.SSLError("deep")
        current = root
        for i in range(50):
            nxt = RuntimeError(f"wrapper-{i}")
            nxt.__cause__ = current
            current = nxt
        # root TLS error is beyond max_nodes -- bounded traversal is allowed
        # to miss it, but must not hang or crash.
        result = probe._contains_tls_error(current)
        self.assertIn(result, (True, False))

    def test_no_raw_certificate_text_in_cli_output(self):
        d = _deployment()
        fake_opener = MagicMock()
        fake_opener.open.side_effect = urllib.error.URLError(
            ssl.SSLCertVerificationError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "Hostname mismatch, CERTIFICATE_SECRET_DETAIL_xyz"))

        with tempfile.TemporaryDirectory() as tmp:
            user_file = os.path.join(tmp, "user")
            pwd_file = os.path.join(tmp, "pwd")
            with open(user_file, "w") as f:
                f.write(SYNTHETIC_USER)
            with open(pwd_file, "w") as f:
                f.write(SYNTHETIC_PASSWORD)
            with mock.patch.object(cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "_basic_opener", return_value=fake_opener):
                with self.assertRaises(probe.ProbeRequestError) as ctx:
                    probe.run_probe(d, "admin", "/services/v2/extracts")

        self.assertEqual(ctx.exception.category, "TLS_FAILED")
        self.assertNotIn("CERTIFICATE_SECRET_DETAIL_xyz", str(ctx.exception))
        self.assertNotIn("Hostname mismatch", str(ctx.exception))


class SummarizeJsonTests(unittest.TestCase):
    def test_successful_schema_extraction(self):
        payload = {"response": {"items": [
            {"name": "EXT1", "status": "RUNNING", "lag": 5},
            {"name": "EXT2", "status": "ABENDED", "lag": "not-a-number"},
        ]}}
        summary = probe.summarize_json(payload)
        self.assertEqual(summary["topLevelKeys"], ["response"])
        self.assertEqual(summary["responseKeys"], ["items"])
        self.assertEqual(summary["itemCount"], 2)
        self.assertEqual(summary["itemFieldNames"], ["lag", "name", "status"])
        self.assertEqual(summary["fieldTypes"]["lag"], ["number", "string"])
        self.assertEqual(summary["fieldTypes"]["name"], ["string"])

    def test_non_dict_payload_is_unexpected(self):
        self.assertIsNone(probe.summarize_json([1, 2, 3]))
        self.assertIsNone(probe.summarize_json("just a string"))
        self.assertIsNone(probe.summarize_json(None))

    def test_field_values_never_appear_in_summary(self):
        payload = {"response": {"items": [
            {"name": "SECRET_PROCESS_NAME_XYZ", "status": "RUNNING"}]}}
        summary = probe.summarize_json(payload)
        blob = json.dumps(summary)
        self.assertNotIn("SECRET_PROCESS_NAME_XYZ", blob)
        self.assertNotIn("RUNNING", blob)

    def test_process_names_never_printed_field_names_only(self):
        payload = {"response": {"items": [{"name": "EXT_PAYMENTS_CONFIDENTIAL"}]}}
        summary = probe.summarize_json(payload)
        self.assertIn("name", summary["itemFieldNames"])
        self.assertNotIn("EXT_PAYMENTS_CONFIDENTIAL", json.dumps(summary))


class CollectionSummarizationTests(unittest.TestCase):
    """Every list-valued response.* field -- not just response.items --
    becomes its own sanitized entry in "collections"."""

    def test_response_processes_empty_list(self):
        payload = {"response": {"processes": []}}
        summary = probe.summarize_json(payload)
        self.assertEqual(summary["collections"]["processes"],
                         {"itemCount": 0, "itemFieldNames": [], "fieldTypes": {}, "truncated": False})

    def test_response_processes_with_synthetic_entries(self):
        payload = {"response": {"processes": [
            {"processName": "SYNTHETIC_EXTRACT_01", "processType": "extract", "lag": 5},
            {"processName": "SYNTHETIC_REPLICAT_01", "processType": "replicat", "lag": 12},
        ]}}
        summary = probe.summarize_json(payload)
        coll = summary["collections"]["processes"]
        self.assertEqual(coll["itemCount"], 2)
        self.assertEqual(coll["itemFieldNames"], ["lag", "processName", "processType"])
        self.assertEqual(coll["fieldTypes"]["lag"], ["number"])
        self.assertEqual(coll["fieldTypes"]["processName"], ["string"])
        self.assertFalse(coll["truncated"])

    def test_response_status_change_empty_list(self):
        payload = {"response": {"statusChange": []}}
        summary = probe.summarize_json(payload)
        self.assertEqual(summary["collections"]["statusChange"]["itemCount"], 0)

    def test_response_status_change_with_synthetic_entries(self):
        payload = {"response": {"statusChange": [
            {"id": 1, "change": "SYNTHETIC_STARTED", "timestamp": 1234567890},
        ]}}
        summary = probe.summarize_json(payload)
        coll = summary["collections"]["statusChange"]
        self.assertEqual(coll["itemCount"], 1)
        self.assertEqual(coll["itemFieldNames"], ["change", "id", "timestamp"])
        self.assertEqual(coll["fieldTypes"]["id"], ["number"])

    def test_multiple_list_collections_in_one_response(self):
        payload = {"response": {
            "processes": [{"a": 1}],
            "statusChange": [{"b": 2}],
            "items": [{"c": 3}],
        }}
        summary = probe.summarize_json(payload)
        self.assertEqual(set(summary["collections"].keys()), {"processes", "statusChange", "items"})
        for name in ("processes", "statusChange", "items"):
            self.assertEqual(summary["collections"][name]["itemCount"], 1)

    def test_non_list_response_fields_excluded_from_collections(self):
        payload = {"response": {
            "processes": [{"a": 1}],
            "summary": {"totalCount": 5},
            "generatedAt": "2026-01-01T00:00:00Z",
            "ok": True,
        }}
        summary = probe.summarize_json(payload)
        self.assertEqual(set(summary["collections"].keys()), {"processes"})
        self.assertIn("summary", summary["responseKeys"])
        self.assertIn("generatedAt", summary["responseKeys"])

    def test_malformed_list_members_skipped_but_counted(self):
        payload = {"response": {"processes": [
            {"name": "OK1"}, None, "garbage", 42, [1, 2], {"name": "OK2"},
        ]}}
        summary = probe.summarize_json(payload)
        coll = summary["collections"]["processes"]
        self.assertEqual(coll["itemCount"], 6)  # true list length, malformed members counted, not inspected
        self.assertEqual(coll["itemFieldNames"], ["name"])

    def test_sorted_field_names(self):
        payload = {"response": {"processes": [{"zeta": 1, "alpha": 2, "mid": 3}]}}
        summary = probe.summarize_json(payload)
        self.assertEqual(summary["collections"]["processes"]["itemFieldNames"], ["alpha", "mid", "zeta"])

    def test_normalized_field_types(self):
        payload = {"response": {"processes": [{
            "s": "text", "n": 5, "f": 1.5, "b": True, "o": {"x": 1}, "a": [1, 2], "z": None,
        }]}}
        summary = probe.summarize_json(payload)
        types = summary["collections"]["processes"]["fieldTypes"]
        self.assertEqual(types["s"], ["string"])
        self.assertEqual(types["n"], ["number"])
        self.assertEqual(types["f"], ["number"])
        self.assertEqual(types["b"], ["boolean"])
        self.assertEqual(types["o"], ["object"])
        self.assertEqual(types["a"], ["array"])
        self.assertEqual(types["z"], ["null"])

    def test_no_actual_field_values_printed(self):
        payload = {"response": {"processes": [{"lag": 999999, "count": 42}]}}
        blob = json.dumps(probe.summarize_json(payload))
        self.assertNotIn("999999", blob)
        self.assertNotIn(": 42", blob)

    def test_synthetic_process_names_absent(self):
        payload = {"response": {"processes": [{"processName": "SYNTHETIC_TOP_SECRET_PROC"}]}}
        blob = json.dumps(probe.summarize_json(payload))
        self.assertNotIn("SYNTHETIC_TOP_SECRET_PROC", blob)

    def test_synthetic_hostnames_absent(self):
        payload = {"response": {"processes": [
            {"host": "gg-oracle-payments-01.goldengate-dev.svc.cluster.local"}]}}
        blob = json.dumps(probe.summarize_json(payload))
        self.assertNotIn("svc.cluster.local", blob)

    def test_synthetic_credentials_absent(self):
        payload = {"response": {"processes": [
            {"username": SYNTHETIC_USER, "password": SYNTHETIC_PASSWORD}]}}
        blob = json.dumps(probe.summarize_json(payload))
        self.assertNotIn(SYNTHETIC_USER, blob)
        self.assertNotIn(SYNTHETIC_PASSWORD, blob)

    def test_nested_objects_reported_only_as_object(self):
        payload = {"response": {"processes": [
            {"config": {"secretKey": "should-not-leak", "nested": {"deeper": 1}}}]}}
        summary = probe.summarize_json(payload)
        self.assertEqual(summary["collections"]["processes"]["fieldTypes"]["config"], ["object"])
        blob = json.dumps(summary)
        self.assertNotIn("should-not-leak", blob)
        self.assertNotIn("secretKey", blob)

    def test_nested_arrays_reported_only_as_array(self):
        payload = {"response": {"processes": [{"history": ["SECRET_A", "SECRET_B", 3]}]}}
        summary = probe.summarize_json(payload)
        self.assertEqual(summary["collections"]["processes"]["fieldTypes"]["history"], ["array"])
        blob = json.dumps(summary)
        self.assertNotIn("SECRET_A", blob)
        self.assertNotIn("SECRET_B", blob)

    def test_item_inspection_limit_and_truncation_flag(self):
        items = [{"a": i} for i in range(probe.MAX_ITEMS_PER_COLLECTION + 10)]
        payload = {"response": {"processes": items}}
        coll = probe.summarize_json(payload)["collections"]["processes"]
        self.assertEqual(coll["itemCount"], probe.MAX_ITEMS_PER_COLLECTION + 10)
        self.assertTrue(coll["truncated"])

    def test_field_name_limit_and_truncation_flag(self):
        wide_item = {f"field{i}": "x" for i in range(probe.MAX_FIELD_NAMES_PER_COLLECTION + 10)}
        payload = {"response": {"processes": [wide_item]}}
        coll = probe.summarize_json(payload)["collections"]["processes"]
        self.assertEqual(len(coll["itemFieldNames"]), probe.MAX_FIELD_NAMES_PER_COLLECTION)
        self.assertTrue(coll["truncated"])

    def test_collection_key_limit_and_truncation_flag(self):
        payload = {"response": {f"list{i}": [1, 2] for i in range(probe.MAX_COLLECTION_KEYS + 5)}}
        summary = probe.summarize_json(payload)
        self.assertEqual(len(summary["collections"]), probe.MAX_COLLECTION_KEYS)
        self.assertTrue(summary["collectionsTruncated"])

    def test_no_truncation_flag_when_within_limits(self):
        payload = {"response": {"processes": [{"a": 1}]}}
        summary = probe.summarize_json(payload)
        self.assertFalse(summary["collections"]["processes"]["truncated"])
        self.assertFalse(summary["collectionsTruncated"])

    def test_existing_response_items_backward_compatibility(self):
        payload = {"response": {"items": [{"name": "X"}]}}
        summary = probe.summarize_json(payload)
        self.assertIn("items", summary["collections"])
        self.assertEqual(summary["itemCount"], 1)
        self.assertEqual(summary["itemFieldNames"], ["name"])
        self.assertEqual(summary["fieldTypes"], {"name": ["string"]})

    def test_response_processes_never_mapped_into_legacy_items_fields(self):
        payload = {"response": {"processes": [{"name": "X"}]}}
        summary = probe.summarize_json(payload)
        self.assertNotIn("itemCount", summary)
        self.assertNotIn("itemFieldNames", summary)
        self.assertNotIn("fieldTypes", summary)


class RunProbeTests(unittest.TestCase):
    """run_probe exercised with a fully mocked HTTP layer -- never touches a
    real socket, DynamoDB, or CloudWatch."""

    def _deployment_with_creds(self, tmp):
        user_file = os.path.join(tmp, "user")
        pwd_file = os.path.join(tmp, "pwd")
        with open(user_file, "w") as f:
            f.write(SYNTHETIC_USER)
        with open(pwd_file, "w") as f:
            f.write(SYNTHETIC_PASSWORD)
        return user_file, pwd_file

    def test_credentials_never_printed_on_missing_credentials(self):
        d = _deployment()
        with tempfile.TemporaryDirectory() as tmp:
            missing_user = os.path.join(tmp, "no-such-user")
            missing_pwd = os.path.join(tmp, "no-such-pwd")
            with mock.patch.object(cfgmod, "credential_paths", return_value=(missing_user, missing_pwd)):
                with self.assertRaises(probe.ProbeValidationError) as ctx:
                    probe.run_probe(d, "admin", "/services/v2/extracts")
        self.assertNotIn(SYNTHETIC_USER, str(ctx.exception))
        self.assertNotIn(SYNTHETIC_PASSWORD, str(ctx.exception))

    def test_successful_probe_returns_sanitized_result_and_no_raw_body(self):
        d = _deployment()
        resp_body = json.dumps({"response": {"items": [
            {"name": "TOP_SECRET_PROCESS", "status": "RUNNING", "lag": 3}]}}).encode()

        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.headers = {"Content-Type": "application/json"}
        fake_resp.read.return_value = resp_body
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False

        fake_opener = MagicMock()
        fake_opener.open.return_value = fake_resp

        with tempfile.TemporaryDirectory() as tmp:
            user_file, pwd_file = self._deployment_with_creds(tmp)
            with mock.patch.object(cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "_basic_opener", return_value=fake_opener):
                result = probe.run_probe(d, "admin", "/services/v2/extracts")

        self.assertEqual(result["deploymentName"], "gg-oracle-payments-01")
        self.assertEqual(result["deploymentType"], "oracle")
        self.assertEqual(result["portType"], "admin")
        self.assertEqual(result["httpStatus"], 200)
        self.assertEqual(result["itemCount"], 1)
        self.assertEqual(result["itemFieldNames"], ["lag", "name", "status"])
        blob = json.dumps(result)
        self.assertNotIn("TOP_SECRET_PROCESS", blob)
        self.assertNotIn(SYNTHETIC_USER, blob)
        self.assertNotIn(SYNTHETIC_PASSWORD, blob)

    def test_auth_failure_classification_from_http_error(self):
        d = _deployment()
        fake_opener = MagicMock()
        fake_opener.open.side_effect = urllib.error.HTTPError(
            "https://internal/services/v2/extracts", 401, "Unauthorized", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            user_file, pwd_file = self._deployment_with_creds(tmp)
            with mock.patch.object(cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "_basic_opener", return_value=fake_opener):
                with self.assertRaises(probe.ProbeRequestError) as ctx:
                    probe.run_probe(d, "admin", "/services/v2/extracts")
        self.assertEqual(ctx.exception.category, "AUTH_FAILED")
        self.assertEqual(ctx.exception.http_status, 401)

    def test_tls_failure_classification(self):
        d = _deployment()
        fake_opener = MagicMock()
        fake_opener.open.side_effect = ssl.SSLError("certificate verify failed")

        with tempfile.TemporaryDirectory() as tmp:
            user_file, pwd_file = self._deployment_with_creds(tmp)
            with mock.patch.object(cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "_basic_opener", return_value=fake_opener):
                with self.assertRaises(probe.ProbeRequestError) as ctx:
                    probe.run_probe(d, "admin", "/services/v2/extracts")
        self.assertEqual(ctx.exception.category, "TLS_FAILED")

    def test_404_classification(self):
        d = _deployment()
        fake_opener = MagicMock()
        fake_opener.open.side_effect = urllib.error.HTTPError(
            "https://internal/services/v2/extracts", 404, "Not Found", {}, None)

        with tempfile.TemporaryDirectory() as tmp:
            user_file, pwd_file = self._deployment_with_creds(tmp)
            with mock.patch.object(cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "_basic_opener", return_value=fake_opener):
                with self.assertRaises(probe.ProbeRequestError) as ctx:
                    probe.run_probe(d, "admin", "/services/v2/extracts")
        self.assertEqual(ctx.exception.category, "NOT_FOUND")

    def test_invalid_json_classification(self):
        d = _deployment()
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.headers = {"Content-Type": "text/html"}
        fake_resp.read.return_value = b"<html>not json</html>"
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False

        fake_opener = MagicMock()
        fake_opener.open.return_value = fake_resp

        with tempfile.TemporaryDirectory() as tmp:
            user_file, pwd_file = self._deployment_with_creds(tmp)
            with mock.patch.object(cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "_basic_opener", return_value=fake_opener):
                with self.assertRaises(probe.ProbeRequestError) as ctx:
                    probe.run_probe(d, "admin", "/services/v2/extracts")
        self.assertEqual(ctx.exception.category, "INVALID_JSON")

    def test_raw_exception_never_printed(self):
        d = _deployment()
        fake_opener = MagicMock()
        fake_opener.open.side_effect = RuntimeError("SECRET_INTERNAL_DETAIL_should_not_leak")

        with tempfile.TemporaryDirectory() as tmp:
            user_file, pwd_file = self._deployment_with_creds(tmp)
            with mock.patch.object(cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "_basic_opener", return_value=fake_opener):
                with self.assertRaises(probe.ProbeRequestError) as ctx:
                    probe.run_probe(d, "admin", "/services/v2/extracts")
        self.assertEqual(ctx.exception.category, "UNKNOWN")
        self.assertNotIn("SECRET_INTERNAL_DETAIL_should_not_leak", str(ctx.exception))


class ResponseSizeLimitTests(unittest.TestCase):
    """The response body must be read bounded (MAX_RESPONSE_BYTES + 1, never
    unbounded), and an oversized body must never be parsed, sized, or
    echoed -- only a fixed, sanitized UNEXPECTED_RESPONSE."""

    def _deployment_with_creds(self, tmp):
        user_file = os.path.join(tmp, "user")
        pwd_file = os.path.join(tmp, "pwd")
        with open(user_file, "w") as f:
            f.write(SYNTHETIC_USER)
        with open(pwd_file, "w") as f:
            f.write(SYNTHETIC_PASSWORD)
        return user_file, pwd_file

    def _run_with_body(self, body_bytes):
        d = _deployment()
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.headers = {"Content-Type": "application/json"}
        # Mirrors the real http.client behaviour that resp.read(n) returns
        # at most n bytes -- exercises the exact bounded-read contract.
        fake_resp.read.side_effect = lambda n=None: body_bytes[:n] if n is not None else body_bytes
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        fake_opener = MagicMock()
        fake_opener.open.return_value = fake_resp

        with tempfile.TemporaryDirectory() as tmp:
            user_file, pwd_file = self._deployment_with_creds(tmp)
            with mock.patch.object(cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "_basic_opener", return_value=fake_opener):
                return probe.run_probe(d, "admin", "/services/v2/mpoints/processes"), fake_resp

    def test_read_called_with_max_response_bytes_plus_one(self):
        body = json.dumps({"response": {"processes": []}}).encode()
        _, fake_resp = self._run_with_body(body)
        fake_resp.read.assert_called_once_with(probe.MAX_RESPONSE_BYTES + 1)

    def test_body_exactly_at_limit_is_accepted(self):
        # Pad a valid JSON document with whitespace so its exact byte length
        # equals MAX_RESPONSE_BYTES while still parsing successfully.
        base = {"response": {"processes": []}}
        core_bytes = json.dumps(base).encode()
        pad = b" " * (probe.MAX_RESPONSE_BYTES - len(core_bytes))
        body = pad + core_bytes
        self.assertEqual(len(body), probe.MAX_RESPONSE_BYTES)
        result, _ = self._run_with_body(body)
        self.assertIn("collections", result)

    def test_body_above_limit_returns_unexpected_response(self):
        oversized = b" " * (probe.MAX_RESPONSE_BYTES + 1)
        with self.assertRaises(probe.ProbeRequestError) as ctx:
            self._run_with_body(oversized)
        self.assertEqual(ctx.exception.category, "UNEXPECTED_RESPONSE")

    def test_no_response_body_fragment_in_error_output(self):
        marker = b"SECRET_OVERSIZED_BODY_MARKER_xyz"
        oversized = marker + b" " * probe.MAX_RESPONSE_BYTES
        with self.assertRaises(probe.ProbeRequestError) as ctx:
            self._run_with_body(oversized)
        self.assertNotIn("SECRET_OVERSIZED_BODY_MARKER_xyz", str(ctx.exception))
        self.assertNotIn(str(len(oversized)), str(ctx.exception))

    def test_probe_still_performs_exactly_one_get(self):
        body = json.dumps({"response": {"processes": []}}).encode()
        d = _deployment()
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.headers = {"Content-Type": "application/json"}
        fake_resp.read.return_value = body
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        fake_opener = MagicMock()
        fake_opener.open.return_value = fake_resp

        with tempfile.TemporaryDirectory() as tmp:
            user_file, pwd_file = self._deployment_with_creds(tmp)
            with mock.patch.object(cfgmod, "credential_paths", return_value=(user_file, pwd_file)), \
                 mock.patch.object(core, "_build_ssl_context", return_value=MagicMock()), \
                 mock.patch.object(core, "_basic_opener", return_value=fake_opener):
                probe.run_probe(d, "admin", "/services/v2/mpoints/processes")
        self.assertEqual(fake_opener.open.call_count, 1)

    def test_oversized_response_makes_no_dynamodb_or_cloudwatch_call(self):
        with mock.patch("boto3.resource") as mock_resource, mock.patch("boto3.client") as mock_client:
            oversized = b" " * (probe.MAX_RESPONSE_BYTES + 1)
            with self.assertRaises(probe.ProbeRequestError):
                self._run_with_body(oversized)
            mock_resource.assert_not_called()
            mock_client.assert_not_called()


class KeyOutputBoundingTests(unittest.TestCase):
    """topLevelKeys/responseKeys/collection field names are sorted,
    length-bounded, and count-bounded -- an omitted key is never emitted
    partial, and the *Truncated flags say when something was dropped."""

    def test_excess_top_level_keys_capped_and_flagged(self):
        payload = {f"k{i:04d}": i for i in range(probe.MAX_TOP_LEVEL_KEYS + 20)}
        payload["response"] = {"processes": []}
        summary = probe.summarize_json(payload)
        self.assertEqual(len(summary["topLevelKeys"]), probe.MAX_TOP_LEVEL_KEYS)
        self.assertTrue(summary["topLevelKeysTruncated"])
        self.assertEqual(summary["topLevelKeys"], sorted(summary["topLevelKeys"]))

    def test_excess_response_keys_capped_and_flagged(self):
        payload = {"response": {f"k{i:04d}": i for i in range(probe.MAX_RESPONSE_KEYS + 20)}}
        summary = probe.summarize_json(payload)
        self.assertEqual(len(summary["responseKeys"]), probe.MAX_RESPONSE_KEYS)
        self.assertTrue(summary["responseKeysTruncated"])

    def test_no_truncation_flags_within_limits(self):
        payload = {"a": 1, "response": {"processes": []}}
        summary = probe.summarize_json(payload)
        self.assertFalse(summary["topLevelKeysTruncated"])
        self.assertFalse(summary["responseKeysTruncated"])

    def test_overlong_top_level_key_omitted(self):
        overlong = "x" * (probe.MAX_KEY_LENGTH + 1)
        payload = {overlong: 1, "short": 2, "response": {"processes": []}}
        summary = probe.summarize_json(payload)
        self.assertNotIn(overlong, summary["topLevelKeys"])
        self.assertIn("short", summary["topLevelKeys"])
        self.assertTrue(summary["topLevelKeysTruncated"])
        for k in summary["topLevelKeys"]:
            self.assertLessEqual(len(k), probe.MAX_KEY_LENGTH)

    def test_overlong_response_key_omitted(self):
        overlong = "y" * (probe.MAX_KEY_LENGTH + 1)
        payload = {"response": {overlong: 1, "short": 2}}
        summary = probe.summarize_json(payload)
        self.assertNotIn(overlong, summary["responseKeys"])
        self.assertIn("short", summary["responseKeys"])
        self.assertTrue(summary["responseKeysTruncated"])

    def test_overlong_collection_field_name_omitted_and_truncated(self):
        overlong = "z" * (probe.MAX_KEY_LENGTH + 1)
        payload = {"response": {"processes": [{overlong: "val", "ok": 1}]}}
        summary = probe.summarize_json(payload)
        coll = summary["collections"]["processes"]
        self.assertNotIn(overlong, coll["itemFieldNames"])
        self.assertIn("ok", coll["itemFieldNames"])
        self.assertTrue(coll["truncated"])
        self.assertEqual(coll["itemCount"], 1)  # itemCount semantics unaffected

    def test_no_partial_overlong_key_ever_emitted(self):
        overlong = "SECRET_PREFIX_" + ("q" * probe.MAX_KEY_LENGTH)
        payload = {"response": {"processes": [{overlong: "val"}]}}
        blob = json.dumps(probe.summarize_json(payload))
        self.assertNotIn("SECRET_PREFIX_", blob)  # not even a truncated prefix

    def test_key_length_boundary_exactly_at_limit_is_kept(self):
        exact = "w" * probe.MAX_KEY_LENGTH
        payload = {exact: 1, "response": {"processes": []}}
        summary = probe.summarize_json(payload)
        self.assertIn(exact, summary["topLevelKeys"])
        self.assertFalse(summary["topLevelKeysTruncated"])

    def test_processes_status_change_items_behaviour_unchanged_alongside_new_bounds(self):
        payload = {"response": {
            "processes": [{"processName": "EXT1", "status": "RUNNING"}],
            "statusChange": [{"id": 1, "change": "started"}],
            "items": [{"name": "LEGACY1"}],
        }}
        summary = probe.summarize_json(payload)
        self.assertEqual(summary["collections"]["processes"]["itemCount"], 1)
        self.assertEqual(summary["collections"]["statusChange"]["itemCount"], 1)
        self.assertEqual(summary["collections"]["items"]["itemCount"], 1)
        self.assertEqual(summary["itemCount"], 1)  # legacy top-level mirror still from items
        self.assertEqual(summary["itemFieldNames"], ["name"])
        self.assertFalse(summary["topLevelKeysTruncated"])
        self.assertFalse(summary["responseKeysTruncated"])


class MetricsPortSecurityTests(unittest.TestCase):
    """Direct metricsPort 9015 is confirmed plain HTTP in the live
    environment: the probe must never read credential files, build a
    Basic-Auth opener, or otherwise attach the mounted admin credentials to
    a metrics-port request -- there is no HTTP credential-transport
    fallback path in this tool at all."""

    def test_metrics_port_never_reads_credential_files(self):
        d = _deployment()
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.headers = {"Content-Type": "application/json"}
        fake_resp.read.return_value = b'{"response": {"items": []}}'
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        fake_opener = MagicMock()
        fake_opener.open.return_value = fake_resp

        with mock.patch.object(cfgmod, "credential_paths") as mock_creds, \
             mock.patch.object(core, "_read_secret_file") as mock_read_secret, \
             mock.patch.object(core, "_build_ssl_context") as mock_ssl_ctx, \
             mock.patch.object(core, "_basic_opener") as mock_basic_opener, \
             mock.patch.object(probe.urllib.request, "build_opener", return_value=fake_opener):
            probe.run_probe(d, "metrics", "/services/v2/metrics")

        mock_creds.assert_not_called()
        mock_read_secret.assert_not_called()
        mock_ssl_ctx.assert_not_called()
        mock_basic_opener.assert_not_called()

    def test_metrics_port_opener_has_no_auth_handler(self):
        # build_opener() with no arguments installs only the default
        # handlers -- no HTTPBasicAuthHandler, so no Authorization header
        # can ever be attached to a metrics-port request.
        opener = __import__("urllib.request", fromlist=["build_opener"]).build_opener()
        import urllib.request as _ur
        self.assertFalse(any(isinstance(h, _ur.HTTPBasicAuthHandler) for h in opener.handlers))

    def test_admin_port_still_requires_credentials(self):
        d = _deployment()
        with tempfile.TemporaryDirectory() as tmp:
            missing_user = os.path.join(tmp, "no-user")
            missing_pwd = os.path.join(tmp, "no-pwd")
            with mock.patch.object(cfgmod, "credential_paths", return_value=(missing_user, missing_pwd)):
                with self.assertRaises(probe.ProbeValidationError):
                    probe.run_probe(d, "admin", "/services/v2/mpoints/processes")


class DocumentationClaimsTests(unittest.TestCase):
    def test_recommended_pms_paths_documented_with_admin_port(self):
        src = probe.__doc__
        self.assertIn("/services/v2/mpoints/processes", src)
        self.assertIn("/services/v2/monitoring/statusChanges", src)
        self.assertIn("--port admin", src)

    def test_metrics_path_not_documented_as_production_pms(self):
        src = (probe.__doc__ or "").lower()
        self.assertIn("/services/v2/metrics", src)
        self.assertIn("confirmed invalid", src)
        self.assertIn("not the production pms endpoint", src)

    def test_direct_9015_documented_as_unapproved_authenticated_path(self):
        src = (probe.__doc__ or "").lower()
        self.assertIn("plain http", src)
        self.assertIn("not an approved authenticated", src)

    def test_readme_documents_confirmed_routes(self):
        readme_path = os.path.join(os.path.dirname(__file__), "..", "README.md")
        with open(readme_path) as f:
            text = f.read()
        self.assertIn("/services/v2/mpoints/processes", text)
        self.assertIn("/services/v2/monitoring/statusChanges", text)
        self.assertIn("--port admin", text)
        self.assertIn("confirmed plain HTTP", text)
        self.assertIn("confirmed invalid", text.lower())


class NoSideEffectTests(unittest.TestCase):
    """The probe tool must never write DynamoDB, never call CloudWatch, and
    never issue a request that could modify a GoldenGate deployment (GET
    only, on the confirmed read-only Admin REST paths)."""

    def test_module_never_imports_boto3_dynamodb_write_apis(self):
        names = set()
        for fn in (probe.run_probe, probe.resolve_deployment, probe.validate_path, probe.main):
            names |= set(fn.__code__.co_names)
        for forbidden in ("put_item", "update_item", "delete_item", "put_metric_data",
                          "Table", "cloudwatch"):
            self.assertNotIn(forbidden, names)

    def test_module_has_no_dynamodb_or_cloudwatch_client_construction(self):
        with open(probe.__file__) as f:
            src = f.read()
        self.assertNotIn("boto3.client", src)
        self.assertNotIn("boto3.resource", src)

    def test_only_http_get_is_used(self):
        with open(probe.__file__) as f:
            src = f.read()
        # opener.open(...) is a GET by construction (no data= is ever passed,
        # which would turn a urllib request into a POST).
        self.assertNotIn("data=", src)
        self.assertNotIn("method=\"POST\"", src)
        self.assertNotIn("method='POST'", src)


class FollowProcessesTests(unittest.TestCase):
    """--follow-processes: inventory GET + up to MAX_FOLLOWED_PROCESSES
    sequential, bounded per-process detail GETs, merging structural schema.
    Never outputs a process name, process ID, or constructed URL. Synthetic
    data only."""

    SYNTHETIC_NAMES = ("SYNTHETIC_EXTRACT_01", "SYNTHETIC_REPLICAT_01")

    def _inventory(self, names=SYNTHETIC_NAMES, extra_items=()):
        processes = [{"processName": n, "processId": i + 1000} for i, n in enumerate(names)]
        processes.extend(extra_items)
        return {"response": {"processes": processes}}

    def _stub_fetch(self, inventory_payload, detail_payload=None, detail_side_effect=None,
                    detail_status=200):
        calls = []

        def _stub(dep, port_type, path, timeout=5):
            calls.append((port_type, path))
            if path == probe.INVENTORY_PATH:
                return 200, "application/json", inventory_payload
            if detail_side_effect is not None:
                outcome = detail_side_effect(path)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return detail_status, "application/json", detail_payload
        return _stub, calls

    def test_inventory_then_process_detail_requests(self):
        stub, calls = self._stub_fetch(self._inventory(), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(result["attemptedCount"], 2)
        self.assertEqual(result["successCount"], 2)
        self.assertEqual(calls[0], ("admin", probe.INVENTORY_PATH))
        self.assertTrue(all(c[1].endswith("/process") for c in calls[1:]))

    def test_each_detail_value_builds_correct_suffix(self):
        for detail in probe.DETAIL_ENDPOINTS:
            stub, calls = self._stub_fetch(self._inventory(names=("P1",)), {"response": {"a": 1}})
            d = _deployment()
            with mock.patch.object(probe, "_fetch_json", side_effect=stub):
                result = probe.follow_processes(d, detail)
            self.assertEqual(result["detail"], detail)
            self.assertEqual(result["successCount"], 1)
            self.assertTrue(calls[1][1].endswith(f"/{detail}"))

    def test_fixed_detail_allowlist_enforced(self):
        self.assertEqual(
            set(probe.DETAIL_ENDPOINTS),
            {"process", "processPerformance", "threadPerformance", "serviceHealth", "heartbeat"})

    def test_invalid_detail_rejected(self):
        d = _deployment()
        with self.assertRaises(probe.ProbeValidationError):
            probe.follow_processes(d, "notARealDetail")

    def test_invalid_detail_rejected_before_any_request(self):
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json") as mock_fetch:
            with self.assertRaises(probe.ProbeValidationError):
                probe.follow_processes(d, "arbitrarySuffix")
        mock_fetch.assert_not_called()

    def test_process_name_url_encoded_as_one_segment(self):
        name = "PROC/WITH/SLASHES"
        stub, calls = self._stub_fetch(self._inventory(names=(name,)), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            probe.follow_processes(d, "process")
        detail_call_path = calls[1][1]
        # exactly 5 segments: '', services, v2, mpoints, <encoded>, process --
        # i.e. the encoded name is ONE segment, never introducing a new '/'.
        segments = detail_call_path.split("/")
        self.assertEqual(len(segments), 6)
        self.assertNotIn("PROC/WITH/SLASHES", detail_call_path)

    def test_traversal_process_name_rejected_not_followed(self):
        stub, calls = self._stub_fetch(self._inventory(names=("..", "SAFE_NAME")), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(result["attemptedCount"], 2)
        self.assertEqual(result["successCount"], 1)
        self.assertEqual(result["errorCategoryCounts"].get("UNKNOWN"), 1)
        # only one real detail GET was ever issued (for the safe name)
        detail_calls = [c for c in calls if c[1] != probe.INVENTORY_PATH]
        self.assertEqual(len(detail_calls), 1)

    def test_process_name_never_printed(self):
        secret_name = "SUPER_SECRET_PROCESS_NAME_XYZ"
        stub, _ = self._stub_fetch(self._inventory(names=(secret_name,)), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertNotIn(secret_name, json.dumps(result))

    def test_process_id_never_printed(self):
        stub, _ = self._stub_fetch(self._inventory(), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        blob = json.dumps(result)
        self.assertNotIn("1000", blob)
        self.assertNotIn("1001", blob)
        self.assertNotIn("processId", blob)

    def test_constructed_url_never_printed(self):
        stub, _ = self._stub_fetch(self._inventory(), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        blob = json.dumps(result)
        self.assertNotIn("/services/v2/mpoints/SYNTHETIC", blob)
        self.assertNotIn(d["adminHost"], blob)

    def test_maximum_20_followed_items(self):
        many_names = tuple(f"PROC{i}" for i in range(30))
        stub, calls = self._stub_fetch(self._inventory(names=many_names), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(result["inventoryItemCount"], 30)
        self.assertEqual(result["attemptedCount"], 20)
        detail_calls = [c for c in calls if c[1] != probe.INVENTORY_PATH]
        self.assertEqual(len(detail_calls), 20)

    def test_malformed_inventory_items_skipped(self):
        extra = [None, "garbage", 42, [1, 2], {"noProcessNameHere": True}]
        stub, _ = self._stub_fetch(self._inventory(names=("OK1",), extra_items=extra),
                                   {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(result["inventoryItemCount"], 1 + len(extra))
        self.assertEqual(result["attemptedCount"], 1)

    def test_missing_process_name_skipped_never_falls_back_to_process_id(self):
        extra = [{"processId": 9999, "processType": "extract"}]  # no processName at all
        stub, calls = self._stub_fetch(self._inventory(names=(), extra_items=extra),
                                       {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            with self.assertRaises(probe.ProbeValidationError):
                probe.follow_processes(d, "process")
        detail_calls = [c for c in calls if c[1] != probe.INVENTORY_PATH]
        self.assertEqual(detail_calls, [])

    def test_one_failed_detail_does_not_stop_remaining(self):
        names = ("P1", "P2", "P3")

        def side_effect(path):
            if path.endswith("/P2/process") or "P2" in path:
                return ProbeRequestErrorFactory("ENDPOINT_UNAVAILABLE")
            return (200, "application/json", {"response": {"a": 1}})

        stub, calls = self._stub_fetch(self._inventory(names=names), detail_side_effect=side_effect)
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(result["attemptedCount"], 3)
        self.assertEqual(result["successCount"], 2)
        self.assertEqual(result["failureCount"], 1)
        self.assertEqual(result["errorCategoryCounts"].get("ENDPOINT_UNAVAILABLE"), 1)

    def test_aggregate_http_status_counts(self):
        names = ("P1", "P2")

        def side_effect(path):
            if "P1" in path:
                return (200, "application/json", {"response": {"a": 1}})
            return ProbeRequestErrorFactory("NOT_FOUND", http_status=404)

        stub, _ = self._stub_fetch(self._inventory(names=names), detail_side_effect=side_effect)
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(result["httpStatusCounts"], {"200": 1, "404": 1})

    def test_aggregate_closed_error_category_counts_only(self):
        names = ("P1", "P2", "P3")

        def side_effect(path):
            if "P1" in path:
                return ProbeRequestErrorFactory("AUTH_FAILED", http_status=401)
            if "P2" in path:
                return ProbeRequestErrorFactory("TLS_FAILED")
            return (200, "application/json", {"response": {"a": 1}})

        stub, _ = self._stub_fetch(self._inventory(names=names), detail_side_effect=side_effect)
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        for category in result["errorCategoryCounts"]:
            self.assertIn(category, probe.ERROR_CATEGORIES)
        self.assertEqual(result["errorCategoryCounts"]["AUTH_FAILED"], 1)
        self.assertEqual(result["errorCategoryCounts"]["TLS_FAILED"], 1)

    def test_merged_field_names_and_broad_types(self):
        names = ("P1", "P2")
        payloads = {
            "P1": {"response": {"lag": 5, "status": "RUNNING"}},
            "P2": {"response": {"lag": 6, "extraField": True}},
        }

        def side_effect(path):
            for n, payload in payloads.items():
                if n in path:
                    return (200, "application/json", payload)
            raise AssertionError("unexpected path")

        stub, _ = self._stub_fetch(self._inventory(names=names), detail_side_effect=side_effect)
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(sorted(result["schema"]["fieldNames"]), ["extraField", "lag", "status"])
        self.assertEqual(result["schema"]["fieldTypes"]["lag"], ["number"])
        self.assertEqual(result["schema"]["fieldTypes"]["status"], ["string"])
        self.assertEqual(result["schema"]["fieldTypes"]["extraField"], ["boolean"])

    def test_nested_object_and_array_values_not_exposed(self):
        stub, _ = self._stub_fetch(
            self._inventory(names=("P1",)),
            {"response": {"config": {"secretKey": "SHOULD_NOT_LEAK"}, "history": ["A_SECRET", "B_SECRET"]}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(result["schema"]["fieldTypes"]["config"], ["object"])
        self.assertEqual(result["schema"]["fieldTypes"]["history"], ["array"])
        blob = json.dumps(result)
        self.assertNotIn("SHOULD_NOT_LEAK", blob)
        self.assertNotIn("A_SECRET", blob)
        self.assertNotIn("B_SECRET", blob)

    def test_oversized_detail_response_counted_as_failure_not_parsed(self):
        names = ("P1",)

        def side_effect(path):
            return ProbeRequestErrorFactory("UNEXPECTED_RESPONSE", http_status=200)

        stub, _ = self._stub_fetch(self._inventory(names=names), detail_side_effect=side_effect)
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(result["successCount"], 0)
        self.assertEqual(result["errorCategoryCounts"].get("UNEXPECTED_RESPONSE"), 1)

    def test_no_raw_values_credentials_or_hostnames_in_output(self):
        stub, _ = self._stub_fetch(
            self._inventory(names=("P1",)),
            {"response": {"user": SYNTHETIC_USER, "pass": SYNTHETIC_PASSWORD,
                         "host": "gg-oracle-payments-01.goldengate-dev.svc.cluster.local",
                         "lag": 42}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        blob = json.dumps(result)
        self.assertNotIn(SYNTHETIC_USER, blob)
        self.assertNotIn(SYNTHETIC_PASSWORD, blob)
        self.assertNotIn("svc.cluster.local", blob)
        self.assertNotIn("42", blob)

    def test_no_dynamodb_or_cloudwatch_call(self):
        stub, _ = self._stub_fetch(self._inventory(names=("P1",)), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch("boto3.resource") as mock_resource, mock.patch("boto3.client") as mock_client:
            with mock.patch.object(probe, "_fetch_json", side_effect=stub):
                probe.follow_processes(d, "process")
            mock_resource.assert_not_called()
            mock_client.assert_not_called()

    def test_get_only_no_authenticated_call_over_9015(self):
        # follow_processes always calls _fetch_json with port_type="admin"
        stub, calls = self._stub_fetch(self._inventory(names=("P1",)), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            probe.follow_processes(d, "process")
        self.assertTrue(all(port_type == "admin" for port_type, _ in calls))

    def test_no_valid_process_items_raises(self):
        stub, _ = self._stub_fetch(self._inventory(names=()), {"response": {"a": 1}})
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            with self.assertRaises(probe.ProbeValidationError):
                probe.follow_processes(d, "process")

    def test_all_detail_requests_failing_still_returns_aggregate(self):
        stub, _ = self._stub_fetch(
            self._inventory(names=("P1", "P2")),
            detail_side_effect=lambda path: ProbeRequestErrorFactory("ENDPOINT_UNAVAILABLE"))
        d = _deployment()
        with mock.patch.object(probe, "_fetch_json", side_effect=stub):
            result = probe.follow_processes(d, "process")
        self.assertEqual(result["successCount"], 0)
        self.assertEqual(result["failureCount"], 2)
        self.assertEqual(result["errorCategoryCounts"]["ENDPOINT_UNAVAILABLE"], 2)


def ProbeRequestErrorFactory(category, http_status=None):
    """Helper: builds the exception, for use as a detail_side_effect return
    value that _stub_fetch raises."""
    return probe.ProbeRequestError(category, http_status=http_status)


class FollowProcessesCliTests(unittest.TestCase):
    """CLI wiring for --follow-processes / --detail; the existing
    explicit-path mode must remain fully unchanged."""

    def _doc(self):
        return {"environment": "dev", "runtimeNamespace": "goldengate-dev",
               "monitoringNamespace": "goldengate-monitoring", "dnsDomain": "example-internal",
               "deployments": [_deployment()]}

    def test_detail_choices_enforced_by_argparse(self):
        with self.assertRaises(SystemExit):
            probe.main(["--deployment", "gg-oracle-payments-01", "--port", "admin",
                       "--follow-processes", "--detail", "notARealDetail"])

    def test_follow_processes_requires_detail(self):
        rc = probe.main(["--deployment", "gg-oracle-payments-01", "--port", "admin",
                         "--follow-processes"])
        self.assertEqual(rc, 2)

    def test_follow_processes_rejects_path(self):
        rc = probe.main(["--deployment", "gg-oracle-payments-01", "--port", "admin",
                         "--follow-processes", "--detail", "process", "--path", "/services/v2/extracts"])
        self.assertEqual(rc, 2)

    def test_follow_processes_requires_admin_port(self):
        rc = probe.main(["--deployment", "gg-oracle-payments-01", "--port", "metrics",
                         "--follow-processes", "--detail", "process"])
        self.assertEqual(rc, 2)

    def test_follow_processes_rejects_unknown_deployment(self):
        with mock.patch.object(cfgmod, "load_deployments", return_value=self._doc()):
            rc = probe.main(["--deployment", "gg-does-not-exist", "--port", "admin",
                             "--follow-processes", "--detail", "process"])
        self.assertEqual(rc, 2)

    def test_explicit_path_mode_unchanged_without_follow_processes(self):
        with mock.patch.object(probe, "resolve_deployment") as mock_resolve:
            rc = probe.main(["--deployment", "gg-oracle-payments-01", "--port", "admin",
                             "--path", "http://evil/services/v2/extracts"])
        self.assertEqual(rc, 2)
        mock_resolve.assert_not_called()


class CliMainTests(unittest.TestCase):
    def test_main_rejects_unsafe_path_before_any_network_call(self):
        with mock.patch.object(probe, "resolve_deployment") as mock_resolve:
            rc = probe.main(["--deployment", "gg-oracle-payments-01", "--port", "admin",
                             "--path", "http://evil/services/v2/extracts"])
        self.assertEqual(rc, 2)
        mock_resolve.assert_not_called()

    def test_main_rejects_unknown_deployment(self):
        doc = {"environment": "dev", "runtimeNamespace": "goldengate-dev",
               "monitoringNamespace": "goldengate-monitoring", "dnsDomain": "example-internal",
               "deployments": [_deployment()]}
        with mock.patch.object(cfgmod, "load_deployments", return_value=doc):
            rc = probe.main(["--deployment", "gg-does-not-exist", "--port", "admin",
                             "--path", "/services/v2/extracts"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
