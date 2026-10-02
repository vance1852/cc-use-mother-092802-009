import unittest

from beverage_ops_foundation.snapshot.acceptance import run


class SnapshotAcceptanceTest(unittest.TestCase):
    def test_snapshot_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["old_version_immutable"])
        self.assertTrue(result["resumed_after_restart"])
        self.assertEqual(2, result["restatement_serial"])
        self.assertIn("bud", result["restatement_impacted_nodes"])
        self.assertGreaterEqual(result["hidden_nodes_count"], 2)


if __name__ == "__main__":
    unittest.main()
