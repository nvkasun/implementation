"""Drift guard: monitoring/monitor/inventory.py is a deliberately separate,
portal-local copy of the canonical-inventory-loading concepts also
implemented by monitoring/gg-monitor-core/inventory.py (see the portal
module's own docstring for why it is not simply imported from there). This
test runs BOTH loaders against the SAME repository fixtures and asserts
their canonical runtime/topology output agrees, so the two copies can never
silently drift apart.
"""
import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Both modules are literally named "inventory.py" in their own directories
# (monitoring/monitor/inventory.py and monitoring/gg-monitor-core/inventory.py)
# -- loaded here by explicit file path (never a plain `import inventory` /
# sys.path insertion) so neither can shadow the other via sys.modules
# caching, regardless of what else this test process has already imported.
_portal_inventory_path = REPO_ROOT / "monitoring" / "monitor" / "inventory.py"
_spec = importlib.util.spec_from_file_location("monitor_portal_inventory", _portal_inventory_path)
portal_inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(portal_inventory)

_collector_inventory_path = REPO_ROOT / "monitoring" / "gg-monitor-core" / "inventory.py"
_spec = importlib.util.spec_from_file_location("gg_monitor_core_inventory", _collector_inventory_path)
collector_inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collector_inventory)


class InventoryDriftTests(unittest.TestCase):
    def test_canonical_runtime_names_and_types_agree(self):
        portal_runtimes = portal_inventory.load_runtimes(str(REPO_ROOT))
        collector_runtimes = collector_inventory.load_runtimes(str(REPO_ROOT))

        portal_shape = sorted(
            (r["pipeline"], r["name"], r["type"], r["enabled"]) for r in portal_runtimes
        )
        collector_shape = sorted(
            (r["pipeline"], r["name"], r["type"], r["enabled"]) for r in collector_runtimes
        )
        self.assertEqual(portal_shape, collector_shape)

    def test_logical_pipeline_source_target_roles_agree(self):
        portal_lps = portal_inventory.build_logical_pipelines(str(REPO_ROOT))
        collector_lps = collector_inventory.build_logical_pipelines(str(REPO_ROOT))

        self.assertEqual(len(portal_lps), len(collector_lps))
        portal_by_id = {lp["pipelineId"]: lp for lp in portal_lps}
        collector_by_id = {lp["pipelineId"]: lp for lp in collector_lps}
        self.assertEqual(set(portal_by_id), set(collector_by_id))

        for pipeline_id, portal_lp in portal_by_id.items():
            collector_lp = collector_by_id[pipeline_id]
            # Portal shape: roles[role] = {"pipeline": ..., "deploymentType": ...}
            # Collector shape: roles[role] = canonical_key (bare string)
            portal_roles = {role: info["pipeline"] for role, info in portal_lp["roles"].items()}
            self.assertEqual(portal_roles, collector_lp["roles"])

    def test_real_repo_shows_source_and_target_for_payments_pipeline(self):
        portal_lps = portal_inventory.build_logical_pipelines(str(REPO_ROOT))
        self.assertEqual(len(portal_lps), 1)
        self.assertEqual(portal_lps[0]["pipelineId"], "payments-ora-to-pg-001")
        self.assertEqual(portal_lps[0]["roles"]["source"]["pipeline"], "gg-oracle-payments-01")
        self.assertEqual(portal_lps[0]["roles"]["target"]["pipeline"], "gg-postgresql-payments-01")


if __name__ == "__main__":
    unittest.main()
