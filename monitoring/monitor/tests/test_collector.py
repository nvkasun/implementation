import os
import sys
import unittest
from unittest import mock
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import boto3
from moto import mock_aws

import collector as core
import health_rules as gh


def make_table():
    client = boto3.client("dynamodb", region_name="eu-west-1")
    client.create_table(
        TableName="gg-eks-pipeline",
        KeySchema=[{"AttributeName": "pipeline", "KeyType": "HASH"},
                  {"AttributeName": "recordType", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pipeline", "AttributeType": "S"},
                              {"AttributeName": "recordType", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return boto3.resource("dynamodb", region_name="eu-west-1").Table("gg-eks-pipeline")


class LeaseManagerTests(unittest.TestCase):
    @mock_aws
    def test_acquire_and_renew(self):
        table = make_table()
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        self.assertTrue(mgr.acquire())
        self.assertTrue(mgr.renew())

    @mock_aws
    def test_second_holder_cannot_acquire_active_lease(self):
        table = make_table()
        first = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        self.assertTrue(first.acquire())
        second = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-1", ttl=30)
        self.assertFalse(second.acquire())

    @mock_aws
    def test_renew_fails_once_lease_expired_and_taken_by_another(self):
        table = make_table()
        first = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30,
                                  clock=lambda: 1000)
        self.assertTrue(first.acquire())
        second = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-1", ttl=30,
                                   clock=lambda: 2000)
        self.assertTrue(second.acquire())
        self.assertFalse(first.renew())


class WriteProcessStateTests(unittest.TestCase):
    @mock_aws
    def test_write_and_read_process_state(self):
        table = make_table()
        mgr = core.LeaseManager(table, "gg-oracle-payments-01", "gg-monitor-0", ttl=30)
        self.assertTrue(mgr.acquire())
        ok = core.write_process_state(
            table, mgr, "gg-oracle-payments-01", "oracle", "EXTORA1",
            {"status": "RUNNING", "recordedAt": 1000, "processType": "extract"},
            lambda: True)
        self.assertTrue(ok)
        row = core.read_process_state(table, "gg-oracle-payments-01", "EXTORA1")
        self.assertEqual(row["status"], "RUNNING")
        self.assertEqual(row["deploymentType"], "oracle")

    def test_write_refused_when_not_leader(self):
        table = MagicMock()
        mgr = MagicMock()
        ok = core.write_process_state(table, mgr, "p", "oracle", "X", {"status": "RUNNING"}, lambda: False)
        self.assertFalse(ok)
        table.update_item.assert_not_called()

    def test_write_refused_when_lease_renew_fails(self):
        table = MagicMock()
        mgr = MagicMock()
        mgr.renew.return_value = False
        ok = core.write_process_state(table, mgr, "p", "oracle", "X", {"status": "RUNNING"}, lambda: True)
        self.assertFalse(ok)
        table.update_item.assert_not_called()

    @mock_aws
    def test_config_never_written_by_collector(self):
        """Terraform owns CONFIG -- the collector module has no code path
        that writes it."""
        import inspect
        src = inspect.getsource(core)
        self.assertNotIn('"CONFIG"', src.replace('recordType": "CONFIG"', ""))

    def test_read_config_uses_get_item_only(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"deploymentType": "oracle"}}
        core.read_config(table, "gg-oracle-payments-01")
        table.get_item.assert_called_once()
        table.scan.assert_not_called()


class CheckStaticPrerequisitesTests(unittest.TestCase):
    def test_missing_config_item_not_ready(self):
        table = MagicMock()
        table.get_item.return_value = {}
        deployment = {"name": "gg-oracle-payments-01", "type": "oracle"}
        with mock.patch.object(core, "_read_secret_file", return_value="secret"), \
             mock.patch.object(core, "_build_ssl_context"):
            ok, reason = core.check_static_prerequisites(deployment, table)
        self.assertFalse(ok)
        self.assertIn("CONFIG", reason)

    def test_never_calls_lease_apis(self):
        """check_static_prerequisites must not call acquire/renew -- an
        early test-acquire would desync LeaseState.is_leader()."""
        import inspect
        src = inspect.getsource(core.check_static_prerequisites)
        self.assertNotIn(".acquire(", src)
        self.assertNotIn(".renew(", src)


class CloudWatchGateTests(unittest.TestCase):
    def test_disabled_by_default(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = False
        self.assertFalse(core.cloudwatch_enabled_for({"metricsEnabled": True}))

    def test_requires_both_flags(self):
        core.CLOUDWATCH_PUBLISH_ENABLED = True
        self.assertFalse(core.cloudwatch_enabled_for({"metricsEnabled": False}))
        self.assertTrue(core.cloudwatch_enabled_for({"metricsEnabled": True}))
        core.CLOUDWATCH_PUBLISH_ENABLED = False


class NoActiveHealingTests(unittest.TestCase):
    def test_no_kubernetes_client_import(self):
        with open(core.__file__) as f:
            src = f.read()
        self.assertNotIn("import kubernetes", src)
        self.assertNotIn("client.CoreV1Api", src)

    def test_failover_flag_computed_but_never_acted_on(self):
        counters, act = gh.abend_step(
            status="ABENDED", state={}, now=1000,
            rule={"abendRecheckSeconds": 1, "maxConsecutiveAbends": 1, "alertEachAbend": False,
                  "failoverEnabled": True},
            alerts_enabled=True)
        self.assertIn("failover", act)
        import inspect
        polling_src = inspect.getsource(core.polling_loop)
        self.assertIn('act["failover"] is never acted on', polling_src)


if __name__ == "__main__":
    unittest.main()
