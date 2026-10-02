import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.snapshot.schema import SNAPSHOT_SCHEMA
from beverage_ops_foundation.snapshot.service import SnapshotService, previous_period
from beverage_ops_foundation.storage import Database

DEFINITION = {
    "presentation_currency": "CNY",
    "metrics": [
        {"metric_id": "revenue", "kind": "sum", "measure": "revenue"},
        {"metric_id": "volume", "kind": "sum", "measure": "volume_hl"},
        {"metric_id": "premium_revenue", "kind": "sum", "measure": "premium_revenue"},
        {"metric_id": "premium_share", "kind": "ratio",
         "numerator_measure": "premium_revenue", "denominator_measure": "revenue"},
        {"metric_id": "unit_cost", "kind": "ratio",
         "numerator_measure": "cogs", "denominator_measure": "volume_hl"},
        {"metric_id": "ebitda", "kind": "sum", "measure": "ebitda"},
        {"metric_id": "ebitda_margin", "kind": "ratio",
         "numerator_measure": "ebitda", "denominator_measure": "revenue"},
        {"metric_id": "revenue_yoy", "kind": "yoy", "base_metric": "revenue"},
    ],
    "checks": [
        {"type": "required_measures", "measures": ["revenue", "volume_hl"], "severity": "blocking"},
        {"type": "measure_order", "rules": ["premium_revenue <= revenue"], "severity": "blocking"},
    ],
    "required_signoff_roles": ["admin", "reviewer"],
}

NODES = [
    {"node_id": "g1", "node_type": "group", "name": "集团"},
    {"node_id": "beer", "parent_id": "g1", "node_type": "segment", "name": "啤酒",
     "segment": "beer"},
    {"node_id": "snow", "parent_id": "beer", "node_type": "brand", "name": "雪花",
     "brand_id": "snow", "region": "east"},
    {"node_id": "baijiu", "parent_id": "g1", "node_type": "segment", "name": "白酒",
     "segment": "baijiu"},
]
CURRENCIES = {"rates": {"CNY": "1", "EUR": "7.8"}, "as_of_date": "2026-06-30"}


class SnapshotTestBase(unittest.TestCase):
    def setUp(self):
        self.database = Database(extra_schema=SNAPSHOT_SCHEMA)
        self.clock = FixedClock(datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.domain = DomainService(self.database, self.clock)
        self.snap = SnapshotService(self.database, self.clock)
        self.domain.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="集团")
        self.domain.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
        self.domain.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                   display_name="经办", role="operator", organization_id="o1")
        self.domain.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                                   display_name="审核", role="reviewer", organization_id="o1")
        self.domain.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                   display_name="审计", role="auditor", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def register_catalog(self):
        self.snap.register_metric_definition(request_id="def", actor_id="a1",
                                             definition_id="d1", body=DEFINITION)
        self.snap.register_scope(request_id="scope", actor_id="a1", scope_id="s1",
                                 nodes=NODES, currencies=CURRENCIES)


class CatalogTest(SnapshotTestBase):
    def test_definition_requires_admin_and_validates(self):
        with self.assertRaises(PermissionDenied):
            self.snap.register_metric_definition(request_id="req-x", actor_id="op1",
                                                 definition_id="d1", body=DEFINITION)
        bad = dict(DEFINITION, metrics=[{"metric_id": "x", "kind": "sum", "measure": "nope"}])
        with self.assertRaises(ValidationError):
            self.snap.register_metric_definition(request_id="req-x", actor_id="a1",
                                                 definition_id="d1", body=bad)

    def test_definition_versions_and_rejects_unchanged(self):
        self.register_catalog()
        with self.assertRaises(ConflictError):
            self.snap.register_metric_definition(request_id="def2", actor_id="a1",
                                                 definition_id="d1", body=DEFINITION)
        changed = {**DEFINITION, "metrics": DEFINITION["metrics"] + [
            {"metric_id": "gross", "kind": "sum", "measure": "revenue"}]}
        with self.assertRaises(ValidationError):  # 升版必须写变化原因
            self.snap.register_metric_definition(request_id="def3", actor_id="a1",
                                                 definition_id="d1", body=changed)
        result = self.snap.register_metric_definition(
            request_id="def3", actor_id="a1", definition_id="d1", body=changed,
            change_reason="增加毛利口径")
        self.assertEqual(2, result["version_no"])
        self.assertEqual(1, self.snap.get_definition("d1", 1)["version_no"])
        self.assertEqual(2, self.snap.get_definition("d1")["version_no"])

    def test_scope_rejects_unknown_parent_and_cycle(self):
        self.snap.register_metric_definition(request_id="def", actor_id="a1",
                                             definition_id="d1", body=DEFINITION)
        with self.assertRaises(ValidationError):
            self.snap.register_scope(request_id="sc", actor_id="a1", scope_id="s1",
                                     nodes=[{"node_id": "a", "node_type": "group", "name": "A",
                                             "parent_id": "ghost"}], currencies=CURRENCIES)
        with self.assertRaises(ValidationError):
            self.snap.register_scope(request_id="sc", actor_id="a1", scope_id="s1",
                                     nodes=[{"node_id": "a", "node_type": "group", "name": "A",
                                             "parent_id": "b"},
                                            {"node_id": "b", "node_type": "group", "name": "B",
                                             "parent_id": "a"}], currencies=CURRENCIES)

    def test_currency_must_be_positive_code(self):
        self.snap.register_metric_definition(request_id="def", actor_id="a1",
                                             definition_id="d1", body=DEFINITION)
        with self.assertRaises(ValidationError):
            self.snap.register_scope(request_id="sc", actor_id="a1", scope_id="s1",
                                     nodes=NODES, currencies={"rates": {"cny": "1"}})
        with self.assertRaises(ValidationError):
            self.snap.register_scope(request_id="sc", actor_id="a1", scope_id="s1",
                                     nodes=NODES, currencies={"rates": {"EUR": "0"}})

    def test_previous_period_rules(self):
        self.assertEqual("2025H1", previous_period("2026H1"))
        self.assertEqual("2025Q4", previous_period("2026Q4"))
        self.assertEqual("2025", previous_period("2026"))
        self.assertEqual("2025-06", previous_period("2026-06"))
        self.assertEqual("2025-01", previous_period("2026-01"))
        self.assertIsNone(previous_period("bad"))


class FactTest(SnapshotTestBase):
    def setUp(self):
        super().setUp()
        self.register_catalog()

    def test_fact_requires_known_node_period_currency_measure(self):
        with self.assertRaises(NotFoundError):
            self.snap.record_fact(request_id="f1", actor_id="op1", fact_id="x1", node_id="ghost",
                                  batch_id="b1", source="erp", period_code="2026H1",
                                  currency_code="CNY", measures={"revenue": 1})
        with self.assertRaises(ValidationError):
            self.snap.record_fact(request_id="f1", actor_id="op1", fact_id="x1", node_id="snow",
                                  batch_id="b1", source="erp", period_code="2026X",
                                  currency_code="CNY", measures={"revenue": 1})
        with self.assertRaises(ValidationError):
            self.snap.record_fact(request_id="f1", actor_id="op1", fact_id="x1", node_id="snow",
                                  batch_id="b1", source="erp", period_code="2026H1",
                                  currency_code="cny", measures={"revenue": 1})
        with self.assertRaises(ValidationError):
            self.snap.record_fact(request_id="f1", actor_id="op1", fact_id="x1", node_id="snow",
                                  batch_id="b1", source="erp", period_code="2026H1",
                                  currency_code="CNY", measures={"mystery": 1})

    def test_same_fact_is_duplicate_revision_leaves_trace(self):
        kwargs = dict(actor_id="op1", fact_id="x1", node_id="snow", batch_id="b1",
                      source="erp", period_code="2026H1", currency_code="CNY",
                      measures={"revenue": 100, "volume_hl": 10})
        first = self.snap.record_fact(request_id="f1", **kwargs)
        self.assertFalse(first["revised"])
        again = self.snap.record_fact(request_id="f2", **kwargs)
        self.assertFalse(again["revised"])
        self.assertTrue(again["duplicate"])
        revised = self.snap.record_fact(
            request_id="f3", actor_id="op1", fact_id="x1", node_id="snow", batch_id="b2",
            source="erp", period_code="2026H1", currency_code="CNY",
            measures={"revenue": 110, "volume_hl": 10}, note="渠道更正")
        self.assertTrue(revised["revised"])
        revisions = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM snap_fact_revisions WHERE fact_id='x1'").fetchone()["c"]
        self.assertEqual(1, revisions)


class VersionLifecycleTest(SnapshotTestBase):
    def setUp(self):
        super().setUp()
        self.register_catalog()
        self.snap.record_fact(request_id="f1", actor_id="op1", fact_id="snow-26",
                              node_id="snow", batch_id="b1", source="erp",
                              period_code="2026H1", currency_code="CNY",
                              measures={"revenue": 1100, "premium_revenue": 440,
                                        "volume_hl": 110, "cogs": 640, "ebitda": 260})
        self.snap.record_fact(request_id="f2", actor_id="op1", fact_id="snow-25",
                              node_id="snow", batch_id="b0", source="erp",
                              period_code="2025H1", currency_code="CNY",
                              measures={"revenue": 1000, "premium_revenue": 300,
                                        "volume_hl": 100, "cogs": 600, "ebitda": 200})

    def _publish(self, version_id, req):
        self.snap.submit_version(request_id=f"{req}-s", actor_id="op1", version_id=version_id)
        self.snap.sign_off(request_id=f"{req}-r", actor_id="rv1", version_id=version_id,
                           decision="approved")
        self.snap.sign_off(request_id=f"{req}-a", actor_id="a1", version_id=version_id,
                           decision="approved")
        self.snap.sanction_publication(request_id=f"{req}-p", actor_id="a1", version_id=version_id)

    def test_computed_metrics_and_yoy(self):
        prior = self.snap.build_version(request_id="v25", actor_id="op1", scope_id="s1",
                                        definition_id="d1", period_code="2025H1")
        self._publish(prior["version_id"], "p25")
        current = self.snap.build_version(request_id="v26", actor_id="op1", scope_id="s1",
                                          definition_id="d1", period_code="2026H1")
        self.assertEqual(0, current["blocking_findings"])
        data = self.snap.query_snapshot("a1", current["version_id"], node_id="snow")
        metrics = next(i["metrics"] for i in data["items"] if i["node_id"] == "snow")
        self.assertAlmostEqual(0.4, metrics["premium_share"], places=6)
        self.assertAlmostEqual(640 / 110, metrics["unit_cost"], places=6)
        self.assertAlmostEqual(260 / 1100, metrics["ebitda_margin"], places=6)
        self.assertAlmostEqual(0.1, metrics["revenue_yoy"], places=6)

    def test_blocking_finding_blocks_submit_and_cannot_be_explained_away(self):
        self.snap.record_fact(request_id="bad", actor_id="op1", fact_id="bad-1", node_id="baijiu",
                              batch_id="b2", source="ch", period_code="2026H1",
                              currency_code="CNY",
                              measures={"revenue": 1, "premium_revenue": 9, "volume_hl": 1})
        version = self.snap.build_version(request_id="vbad", actor_id="op1", scope_id="s1",
                                          definition_id="d1", period_code="2026H1")
        self.assertGreaterEqual(version["blocking_findings"], 1)
        with self.assertRaises(ConflictError):
            self.snap.submit_version(request_id="sbad", actor_id="op1",
                                     version_id=version["version_id"])
        key = self.snap.list_findings("op1", version["version_id"])["items"][0]["finding_key"]
        with self.assertRaises(ConflictError):
            self.snap.acknowledge_findings(request_id="ackbad", actor_id="op1",
                                           version_id=version["version_id"],
                                           finding_keys=[key], note="说明也不行")

    def test_missing_fx_rate_is_blocking(self):
        self.snap.record_fact(request_id="eur", actor_id="op1", fact_id="eur-1", node_id="snow",
                              batch_id="be", source="eu", period_code="2026H1",
                              currency_code="USD",
                              measures={"revenue": 10, "volume_hl": 1})
        version = self.snap.build_version(request_id="veur", actor_id="op1", scope_id="s1",
                                          definition_id="d1", period_code="2026H1")
        findings = self.snap.list_findings("op1", version["version_id"])["items"]
        self.assertTrue(any(f["check_type"] == "fx_rate_missing" for f in findings))

    def test_signoff_roles_and_rejection(self):
        version = self.snap.build_version(request_id="vv", actor_id="op1", scope_id="s1",
                                          definition_id="d1", period_code="2026H1")
        self.snap.submit_version(request_id="ss", actor_id="op1", version_id=version["version_id"])
        with self.assertRaises(PermissionDenied):  # operator 无会签权
            self.snap.sign_off(request_id="so", actor_id="op1", version_id=version["version_id"],
                               decision="approved")
        self.snap.sign_off(request_id="sr", actor_id="rv1", version_id=version["version_id"],
                           decision="rejected", comment="数据存疑")
        self.assertEqual("rejected", self.snap.get_version("a1", version["version_id"])["status"])
        with self.assertRaises(ConflictError):
            self.snap.sign_off(request_id="sa", actor_id="a1", version_id=version["version_id"],
                               decision="approved")

    def test_publish_freezes_and_restatement_carries_diff(self):
        prior = self.snap.build_version(request_id="v25", actor_id="op1", scope_id="s1",
                                        definition_id="d1", period_code="2025H1")
        self._publish(prior["version_id"], "p25")
        current = self.snap.build_version(request_id="v26", actor_id="op1", scope_id="s1",
                                          definition_id="d1", period_code="2026H1")
        self._publish(current["version_id"], "p26")
        meta = self.snap.get_version("a1", current["version_id"])
        self.assertEqual("published", meta["status"])
        self.assertTrue(self.snap.recompute_version("a1", current["version_id"])["matches"])
        # 已发布版本不能重建或撤销。
        with self.assertRaises(ConflictError):
            self.snap.rebuild_version(request_id="rb", actor_id="op1",
                                      version_id=current["version_id"])
        with self.assertRaises(ConflictError):
            self.snap.cancel_version(request_id="cx", actor_id="a1",
                                     version_id=current["version_id"])
        frozen_hash = meta["results_hash"]
        # 晚到批次：只能重述。
        self.snap.record_fact(request_id="late", actor_id="op1", fact_id="late-1", node_id="snow",
                              batch_id="late-b", source="channel", period_code="2026H1",
                              currency_code="CNY",
                              measures={"revenue": 50, "premium_revenue": 20, "volume_hl": 5,
                                        "cogs": 25, "ebitda": 10})
        self.assertEqual(["late-b"],
                         self.snap.get_version("a1", current["version_id"])["facts"]["late_batches"])
        restatement = self.snap.build_version(
            request_id="vr", actor_id="op1", scope_id="s1", definition_id="d1",
            period_code="2026H1", kind="restatement",
            restates_version_id=current["version_id"], change_reason="晚到渠道批次补录")
        diff = self.snap.get_version("a1", restatement["version_id"])["diff"]
        self.assertEqual(["late-1"], diff["facts_added"])
        self.assertIn("snow", diff["impacted_node_ids"])
        self._publish(restatement["version_id"], "pr")
        old = self.snap.get_version("a1", current["version_id"])
        self.assertEqual("superseded", old["status"])
        self.assertEqual(restatement["version_id"], old["restated_by_version_id"])
        self.assertEqual(frozen_hash, old["results_hash"])  # 旧版数字永不改变
        self.assertTrue(self.snap.recompute_version("a1", current["version_id"])["matches"])

    def test_restatement_requires_reason_and_published_target(self):
        current = self.snap.build_version(request_id="v26", actor_id="op1", scope_id="s1",
                                          definition_id="d1", period_code="2026H1")
        with self.assertRaises(ValidationError):
            self.snap.build_version(request_id="vr1", actor_id="op1", scope_id="s1",
                                    definition_id="d1", period_code="2026H1", kind="restatement")
        with self.assertRaises(ConflictError):
            self.snap.build_version(request_id="vr2", actor_id="op1", scope_id="s1",
                                    definition_id="d1", period_code="2026H1", kind="restatement",
                                    restates_version_id=current["version_id"],
                                    change_reason="尚未发布不能重述")

    def test_batch_traces_versions_from_frozen_input(self):
        prior = self.snap.build_version(request_id="v25", actor_id="op1", scope_id="s1",
                                        definition_id="d1", period_code="2025H1")
        self._publish(prior["version_id"], "p25")
        current = self.snap.build_version(request_id="v26", actor_id="op1", scope_id="s1",
                                          definition_id="d1", period_code="2026H1")
        self._publish(current["version_id"], "p26")
        by_batch = self.snap.list_versions("a1", batch_id="b1")["items"]
        self.assertEqual([current["version_id"]], [v["version_id"] for v in by_batch])
        self.assertEqual([prior["version_id"]],
                         [v["version_id"] for v in self.snap.list_versions("a1", batch_id="b0")["items"]])
        self.assertEqual([], self.snap.list_versions("a1", batch_id="nope")["items"])

    def test_open_version_conflict_and_unique_serial(self):
        first = self.snap.build_version(request_id="va", actor_id="op1", scope_id="s1",
                                        definition_id="d1", period_code="2026H1")
        with self.assertRaises(ConflictError):
            self.snap.build_version(request_id="vb", actor_id="op1", scope_id="s1",
                                    definition_id="d1", period_code="2026H1")
        self.snap.cancel_version(request_id="vc", actor_id="a1", version_id=first["version_id"])
        second = self.snap.build_version(request_id="vd", actor_id="op1", scope_id="s1",
                                         definition_id="d1", period_code="2026H1")
        self.assertEqual(2, second["serial"])

    def test_resume_pending_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path, extra_schema=SNAPSHOT_SCHEMA)
            domain = DomainService(database, self.clock)
            snap = SnapshotService(database, self.clock)
            domain.register_organization(request_id="req-org", actor_id="bootstrap",
                                         organization_id="o1", name="集团")
            domain.register_actor(request_id="aa", actor_id="bootstrap", new_actor_id="a1",
                                  display_name="管", role="admin", organization_id="o1")
            domain.register_actor(request_id="oo", actor_id="a1", new_actor_id="op1",
                                  display_name="办", role="operator", organization_id="o1")
            domain.register_actor(request_id="rr", actor_id="a1", new_actor_id="rv1",
                                  display_name="审", role="reviewer", organization_id="o1")
            snap.register_metric_definition(request_id="req-def", actor_id="a1",
                                            definition_id="d1", body=DEFINITION)
            snap.register_scope(request_id="req-scope", actor_id="a1", scope_id="s1",
                                nodes=NODES, currencies=CURRENCIES)
            snap.record_fact(request_id="req-fact", actor_id="op1", fact_id="fx", node_id="snow",
                             batch_id="b1", source="erp", period_code="2026H1",
                             currency_code="CNY",
                             measures={"revenue": 100, "premium_revenue": 20, "volume_hl": 10})
            version = snap.build_version(request_id="bv", actor_id="op1", scope_id="s1",
                                         definition_id="d1", period_code="2026H1")
            snap.submit_version(request_id="sv", actor_id="op1", version_id=version["version_id"])
            database.close()

            database = Database(path, extra_schema=SNAPSHOT_SCHEMA)
            snap = SnapshotService(database, self.clock)
            self.assertEqual("pending",
                             snap.get_version("a1", version["version_id"])["status"])
            snap.sign_off(request_id="rs", actor_id="rv1", version_id=version["version_id"],
                          decision="approved")
            snap.sign_off(request_id="as", actor_id="a1", version_id=version["version_id"],
                          decision="approved")
            snap.sanction_publication(request_id="ps", actor_id="a1",
                                      version_id=version["version_id"])
            self.assertEqual("published",
                             snap.get_version("a1", version["version_id"])["status"])
            database.close()


class DrillAndPermissionTest(SnapshotTestBase):
    def setUp(self):
        super().setUp()
        self.register_catalog()
        for fid, node, revenue in (("f1", "snow", 1000), ("f2", "baijiu", 900)):
            self.snap.record_fact(request_id=fid, actor_id="op1", fact_id=fid, node_id=node,
                                  batch_id="b1", source="erp", period_code="2026H1",
                                  currency_code="CNY",
                                  measures={"revenue": revenue, "volume_hl": 100,
                                            "premium_revenue": 300 if node == "snow" else 0,
                                            "cogs": 500, "ebitda": 200})
        version = self.snap.build_version(request_id="bv", actor_id="op1", scope_id="s1",
                                          definition_id="d1", period_code="2026H1")
        self.version_id = version["version_id"]
        self.snap.grant_node_access(request_id="req-grant", actor_id="a1", target_actor_id="rv1",
                                    node_ids=["beer"])

    def test_admin_sees_all_other_role_sees_subtree_only(self):
        admin = self.snap.query_snapshot("a1", self.version_id)
        self.assertEqual({"g1", "beer", "snow", "baijiu"}, {i["node_id"] for i in admin["items"]})
        self.assertEqual(1, admin["totals"]["node_count"])  # 不相交最高节点只有集团根
        restricted = self.snap.query_snapshot("rv1", self.version_id)
        self.assertEqual({"beer", "snow"}, {i["node_id"] for i in restricted["items"]})
        self.assertTrue(restricted["partial"])
        self.assertGreaterEqual(restricted["hidden_nodes_count"], 2)
        with self.assertRaises(PermissionDenied):
            self.snap.query_snapshot("rv1", self.version_id, node_id="g1")
        with self.assertRaises(PermissionDenied):
            self.snap.query_snapshot("rv1", self.version_id, node_id="baijiu")

    def test_filters_and_drill(self):
        east = self.snap.query_snapshot("a1", self.version_id, region="east")
        self.assertEqual({"snow"}, {i["node_id"] for i in east["items"]})
        beer = self.snap.query_snapshot("a1", self.version_id, segment="beer", drill="brand")
        self.assertEqual([{"brand": "snow", **{k: beer["drill"][0][k] for k in
                          ("node_count", "measures", "metrics")}}], beer["drill"])
        by_segment = self.snap.query_snapshot("a1", self.version_id, drill="segment")
        self.assertEqual({"beer", "baijiu"}, {row["segment"] for row in by_segment["drill"]})
        # 过滤合计只含匹配子树，白酒不串到啤酒。
        self.assertAlmostEqual(1000, beer["totals"]["measures"]["revenue"])

    def test_auditor_without_grant_sees_nothing_but_can_verify_hash(self):
        empty = self.snap.query_snapshot("au1", self.version_id)
        self.assertEqual([], empty["items"])
        self.assertTrue(empty["partial"])
        self.assertTrue(self.snap.recompute_version("au1", self.version_id)["matches"])


if __name__ == "__main__":
    unittest.main()
