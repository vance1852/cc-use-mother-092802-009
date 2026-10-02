import unittest
from datetime import datetime, timezone

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.errors import ConflictError, PermissionDenied, ValidationError
from beverage_ops_foundation.service import DomainService
from beverage_snapshot import domain
from beverage_snapshot.service import SnapshotService
from beverage_snapshot.storage import SnapshotDatabase


def catalog():
    specs = {
        "sales_volume": ("销量", "板块单位", ""),
        "premium_volume": ("高端销量", "板块单位", ""),
        "gross_revenue": ("营业额", "CNY", ""),
        "premium_revenue": ("高端营业额", "CNY", ""),
        "cogs": ("销货成本", "CNY", ""),
        "operating_expense": ("期间费用", "CNY", ""),
        "depreciation_amortization": ("折旧摊销", "CNY", ""),
        "premium_volume_share": ("高端销量占比", "ratio", "premium_volume / sales_volume"),
        "premium_revenue_share": ("高端营业额占比", "ratio",
                                  "premium_revenue / gross_revenue"),
        "unit_cost": ("单位成本", "成本/单位", "cogs / sales_volume"),
        "ebitda": ("EBITDA", "CNY",
                   "gross_revenue - cogs - operating_expense + depreciation_amortization"),
        "ebitda_margin": ("EBITDA 率", "ratio", "ebitda / gross_revenue"),
        "yoy_growth": ("同比增长", "ratio", "gross_revenue / 上年同期 - 1"),
    }
    return [{"metric_id": m,
             "metric_kind": "derived" if formula else "fact",
             "display_name": name, "unit": unit, "description": name,
             "formula": formula} for m, (name, unit, formula) in specs.items()]


class SnapshotServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = SnapshotDatabase()
        clock = FixedClock(datetime(2026, 9, 20, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, clock)
        self.service = SnapshotService(self.database, clock)
        self.foundation.register_organization(
            request_id="org", actor_id="bootstrap", organization_id="o1", name="集团")
        self.foundation.register_actor(
            request_id="admin", actor_id="bootstrap", new_actor_id="a1",
            display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(
            request_id="rv", actor_id="a1", new_actor_id="rv1",
            display_name="复核人", role="reviewer", organization_id="o1")
        self.foundation.register_actor(
            request_id="op", actor_id="a1", new_actor_id="op1",
            display_name="分析", role="operator", organization_id="o1")
        self.foundation.register_actor(
            request_id="au", actor_id="a1", new_actor_id="au1",
            display_name="审计", role="auditor", organization_id="o1")
        self.service.grant_data_scope(request_id="scope-beer", actor_id="a1",
                                      target_actor_id="op1", scope_type="segment",
                                      scope_value="beer")
        self.service.grant_data_scope(request_id="scope-baijiu", actor_id="a1",
                                      target_actor_id="op1", scope_type="segment",
                                      scope_value="baijiu")
        self.def_id = self.service.create_definition_version(
            request_id="def", actor_id="a1", fiscal_period="2026H1",
            reporting_ccy="CNY", definitions=catalog(),
            exchange_rates={"CNY": 1.0, "USD": 7.0},
            organization_scope=["o1"])["resource_id"]
        self.service.freeze_definition_version(
            request_id="def-freeze", actor_id="a1", definition_version_id=self.def_id)

    def tearDown(self):
        self.database.close()

    # ---------------------------------------------------------- 定义版本

    def test_definition_requires_full_standard_catalog(self):
        with self.assertRaises(ValidationError):
            self.service.create_definition_version(
                request_id="def2", actor_id="a1", fiscal_period="2026H2",
                reporting_ccy="CNY", definitions=catalog()[:-1],
                exchange_rates={"CNY": 1.0}, organization_scope=["o1"])

    def test_reporting_currency_rate_must_be_one(self):
        with self.assertRaises(ValidationError):
            self.service.create_definition_version(
                request_id="def3", actor_id="a1", fiscal_period="2026H2",
                reporting_ccy="USD", definitions=catalog(),
                exchange_rates={"CNY": 7.0, "USD": 2.0}, organization_scope=["o1"])

    def test_operator_cannot_create_definition(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_definition_version(
                request_id="def4", actor_id="op1", fiscal_period="2026H2",
                reporting_ccy="CNY", definitions=catalog(),
                exchange_rates={"CNY": 1.0}, organization_scope=["o1"])

    def test_facts_require_frozen_definition(self):
        def_id = self.service.create_definition_version(
            request_id="def5", actor_id="a1", fiscal_period="2025H1",
            reporting_ccy="CNY", definitions=catalog(),
            exchange_rates={"CNY": 1.0}, organization_scope=["o1"])["resource_id"]
        with self.assertRaises(ConflictError):
            self._fact(def_id, "b1", "gross_revenue", 100.0)

    # ---------------------------------------------------------- 事实与批次

    def _fact(self, def_id, batch, metric, value, *, req="f", period="2026H1",
              segment="beer", brand="Brew1", region="east", currency=None,
              unit=None, is_late=False, actor="op1"):
        kwargs: dict[str, object] = {}
        if currency:
            kwargs["currency"] = currency
        if unit:
            kwargs["unit"] = unit
        return self.service.record_fact(
            request_id=req, actor_id=actor, definition_version_id=def_id,
            batch_id=batch, source="channel", segment=segment, brand=brand,
            region=region, period=period, metric_id=metric, value=value,
            is_late=is_late, **kwargs)

    def test_volume_unit_must_match_segment_caliber(self):
        with self.assertRaises(ValidationError):
            self._fact(self.def_id, "b1", "sales_volume", 100.0, unit="tonne")

    def test_derived_metric_cannot_be_reported(self):
        with self.assertRaises(ValidationError):
            self._fact(self.def_id, "b1", "ebitda", 100.0)

    def test_same_business_key_with_changed_value_conflicts(self):
        self._fact(self.def_id, "b1", "gross_revenue", 100.0, req="f1")
        with self.assertRaises(ConflictError):
            self._fact(self.def_id, "b1", "gross_revenue", 110.0, req="f2")

    def test_late_batch_is_new_row_not_overwrite(self):
        first = self._fact(self.def_id, "b1", "gross_revenue", 100.0, req="f1")
        late = self._fact(self.def_id, "b2-late", "gross_revenue", 130.0,
                          req="f2", is_late=True)
        self.assertNotEqual(first["resource_id"], late["resource_id"])
        rows = self.service.list_facts("a1", self.def_id)["items"]
        self.assertEqual(2, len(rows))

    def test_data_scope_hides_facts(self):
        self._fact(self.def_id, "b1", "gross_revenue", 100.0, req="f1")
        self.service.grant_data_scope(request_id="scope-rv", actor_id="a1",
                                      target_actor_id="rv1", scope_type="brand",
                                      scope_value="Brew1")
        visible = self.service.list_facts("rv1", self.def_id)
        self.assertEqual(1, len(visible["items"]))
        self.assertEqual(0, visible["hidden_count"])
        # 未授予任何范围的操作者只能看到空集与隐藏计数。
        self.service.grant_data_scope(request_id="scope-au", actor_id="a1",
                                      target_actor_id="au1", scope_type="region",
                                      scope_value="nowhere")
        hidden = self.service.list_facts("au1", self.def_id)
        self.assertEqual(0, len(hidden["items"]))
        self.assertEqual(1, hidden["hidden_count"])

    # ---------------------------------------------------------- 勾稽与发布

    def _balanced_group(self, req_prefix, period, revenue, volume, premium_v):
        values = dict(sales_volume=volume, premium_volume=premium_v,
                      gross_revenue=revenue,
                      premium_revenue=revenue * 0.5, cogs=revenue * 0.6,
                      operating_expense=revenue * 0.15,
                      depreciation_amortization=revenue * 0.05)
        for i, (metric, value) in enumerate(values.items()):
            self._fact(self.def_id, "b1", metric, float(value),
                       req=f"{req_prefix}-{i}", period=period)

    def _publish(self, snapshot_id, tag):
        result = self.service.reconcile(actor_id="op1", snapshot_version_id=snapshot_id)
        for issue in result["issues"]:
            if issue["status"] == "open":
                self.service.explain_issue(
                    request_id=f"ex-{tag}-{issue['issue_id'][:6]}", actor_id="rv1",
                    issue_id=issue["issue_id"], explanation="已知结构差异，留档")
        self.service.submit_snapshot(actor_id="op1", snapshot_version_id=snapshot_id)
        self.service.approve_snapshot(request_id=f"ap-rv-{tag}", actor_id="rv1",
                                      snapshot_version_id=snapshot_id,
                                      decision="approved", comment="ok")
        self.service.approve_snapshot(request_id=f"ap-ad-{tag}", actor_id="a1",
                                      snapshot_version_id=snapshot_id,
                                      decision="approved", comment="ok")

    def test_blocking_reconciliation_prevents_submit(self):
        # 高端销量超过总销量：阻塞性勾稽失败。
        self._fact(self.def_id, "b1", "sales_volume", 100.0, req="v1")
        self._fact(self.def_id, "b1", "premium_volume", 120.0, req="v2")
        self._fact(self.def_id, "b1", "gross_revenue", 800.0, req="v3")
        snapshot_id = self.service.create_snapshot(
            request_id="snap", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1")["resource_id"]
        result = self.service.reconcile(actor_id="op1", snapshot_version_id=snapshot_id)
        codes = {i["code"] for i in result["issues"] if i["severity"] == "blocking"}
        self.assertIn("premium_exceeds_volume", codes)
        with self.assertRaises(ConflictError):
            self.service.submit_snapshot(actor_id="op1", snapshot_version_id=snapshot_id)

    def test_blocking_issue_cannot_be_explained_away(self):
        self._fact(self.def_id, "b1", "sales_volume", 100.0, req="v1")
        self._fact(self.def_id, "b1", "premium_volume", 120.0, req="v2")
        self._fact(self.def_id, "b1", "gross_revenue", 800.0, req="v3")
        snapshot_id = self.service.create_snapshot(
            request_id="snap2", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1")["resource_id"]
        result = self.service.reconcile(actor_id="op1", snapshot_version_id=snapshot_id)
        blocking = next(i for i in result["issues"] if i["severity"] == "blocking")
        with self.assertRaises(ConflictError):
            self.service.explain_issue(request_id="ex-bad", actor_id="rv1",
                                       issue_id=blocking["issue_id"],
                                       explanation="试图绕过")

    def test_publish_requires_both_approval_roles(self):
        self._balanced_group("p", "2026H1", 1000.0, 100.0, 40.0)
        snapshot_id = self.service.create_snapshot(
            request_id="snap3", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1")["resource_id"]
        result = self.service.reconcile(actor_id="op1", snapshot_version_id=snapshot_id)
        for issue in result["issues"]:
            if issue["status"] == "open":
                self.service.explain_issue(
                    request_id=f"ex-s-{issue['issue_id'][:6]}", actor_id="rv1",
                    issue_id=issue["issue_id"], explanation="留档")
        self.service.submit_snapshot(actor_id="op1", snapshot_version_id=snapshot_id)
        # 审计员不在会签角色内。
        with self.assertRaises(PermissionDenied):
            self.service.approve_snapshot(request_id="ap0", actor_id="au1",
                                          snapshot_version_id=snapshot_id,
                                          decision="approved")
        # 只有复核人批准时仍保持待会签，管理员批准后才发布。
        self.service.approve_snapshot(request_id="ap1", actor_id="rv1",
                                      snapshot_version_id=snapshot_id,
                                      decision="approved")
        self.assertEqual("submitted",
                         self.service.get_snapshot_version("a1", snapshot_id)["state"])
        self.service.approve_snapshot(request_id="ap3", actor_id="a1",
                                      snapshot_version_id=snapshot_id,
                                      decision="approved")
        self.assertEqual("published",
                         self.service.get_snapshot_version("a1", snapshot_id)["state"])

    def test_rejection_sends_snapshot_back_to_draft(self):
        self._balanced_group("r", "2026H1", 1000.0, 100.0, 40.0)
        snapshot_id = self.service.create_snapshot(
            request_id="snap4", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1")["resource_id"]
        self._publish(snapshot_id, "x")
        # 重述一个 v2 并驳回。
        self._fact(self.def_id, "b2-late", "gross_revenue", 1100.0,
                   req="late1", is_late=True)
        v2 = self.service.create_restatement(
            request_id="restate", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1", reason=domain.RESTATEMENT_LATE_DATA,
            change_summary="晚到批次")["resource_id"]
        result = self.service.reconcile(actor_id="op1", snapshot_version_id=v2)
        for issue in result["issues"]:
            if issue["status"] == "open":
                self.service.explain_issue(
                    request_id=f"ex-v2-{issue['issue_id'][:6]}", actor_id="rv1",
                    issue_id=issue["issue_id"], explanation="留档")
        self.service.submit_snapshot(actor_id="op1", snapshot_version_id=v2)
        self.service.approve_snapshot(request_id="rej", actor_id="rv1",
                                      snapshot_version_id=v2, decision="rejected",
                                      comment="数据再核")
        self.assertEqual("draft",
                         self.service.get_snapshot_version("a1", v2)["state"])

    # ---------------------------------------------------------- 不可变与重述

    def test_late_data_after_publish_only_creates_restatement(self):
        self._balanced_group("i", "2026H1", 1000.0, 100.0, 40.0)
        snapshot_id = self.service.create_snapshot(
            request_id="snap5", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1")["resource_id"]
        self._publish(snapshot_id, "imm")
        before = self.service.view_snapshot("a1", snapshot_id)
        v1_west = before["result"]["groups"][0]["metrics"]["gross_revenue"]

        self._fact(self.def_id, "b9-late", "gross_revenue", 1300.0,
                   req="late9", is_late=True)
        v2 = self.service.create_restatement(
            request_id="restate9", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1", reason=domain.RESTATEMENT_LATE_DATA,
            change_summary="晚到渠道数据上修 300")["resource_id"]
        self._publish(v2, "v9")

        after = self.service.view_snapshot("a1", snapshot_id)
        v1_meta = self.service.get_snapshot_version("a1", snapshot_id)
        v2_meta = self.service.get_snapshot_version("a1", v2)
        self.assertEqual("superseded", v1_meta["state"])
        self.assertEqual(v1_west, after["result"]["groups"][0]["metrics"]["gross_revenue"])
        self.assertEqual(snapshot_id, v2_meta["parent_version_id"])
        self.assertEqual(300.0, v2_meta["impact"]["totals"]["gross_revenue"]["delta"])
        self.assertEqual(["east"], v2_meta["impact"]["affected_regions"])

    def test_restatement_requires_published_parent(self):
        self._balanced_group("n", "2026H1", 1000.0, 100.0, 40.0)
        self.service.create_snapshot(
            request_id="snap6", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1")
        with self.assertRaises(ConflictError):
            self.service.create_restatement(
                request_id="restate-n", actor_id="op1",
                definition_version_id=self.def_id, period="2026H1",
                reason=domain.RESTATEMENT_LATE_DATA, change_summary="无已发布版本")

    def test_duplicate_snapshot_for_same_period_conflicts(self):
        self._balanced_group("d", "2026H1", 1000.0, 100.0, 40.0)
        self.service.create_snapshot(
            request_id="snap7", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1")
        with self.assertRaises(ConflictError):
            self.service.create_snapshot(
                request_id="snap8", actor_id="op1", definition_version_id=self.def_id,
                period="2026H1")

    def test_recompute_verification_matches_published(self):
        self._balanced_group("v", "2026H1", 1000.0, 100.0, 40.0)
        snapshot_id = self.service.create_snapshot(
            request_id="snap9", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1")["resource_id"]
        self._publish(snapshot_id, "ver")
        check = self.service.verify_snapshot("rv1", snapshot_id)
        self.assertTrue(check["fingerprint_match"])
        self.assertTrue(check["results_match"])

    def test_late_batch_arriving_after_reconcile_blocks_submit(self):
        # 勾稽通过、异常说明完毕之后又到晚到批次：提交时必须重新勾稽拦住。
        self._balanced_group("g", "2026H1", 1000.0, 100.0, 40.0)
        snapshot_id = self.service.create_snapshot(
            request_id="snap10", actor_id="op1", definition_version_id=self.def_id,
            period="2026H1")["resource_id"]
        result = self.service.reconcile(actor_id="op1", snapshot_version_id=snapshot_id)
        for issue in result["issues"]:
            if issue["status"] == "open":
                self.service.explain_issue(
                    request_id=f"ex-g-{issue['issue_id'][:6]}", actor_id="rv1",
                    issue_id=issue["issue_id"], explanation="留档")
        # 新晚到批次在提交前到达。
        self._fact(self.def_id, "b8-late", "gross_revenue", 1900.0,
                   req="late8", is_late=True)
        with self.assertRaises(ConflictError):
            self.service.submit_snapshot(actor_id="op1", snapshot_version_id=snapshot_id)
        # 说明新的晚到异常后即可提交，新批次数字进入本草稿（尚未发布，允许刷新）。
        result = self.service.reconcile(actor_id="op1", snapshot_version_id=snapshot_id)
        late_issue = next(i for i in result["issues"] if i["code"] == "late_channel_data")
        self.service.explain_issue(request_id="ex-late8", actor_id="rv1",
                                   issue_id=late_issue["issue_id"],
                                   explanation="晚到批次，发布前并入")
        self.service.submit_snapshot(actor_id="op1", snapshot_version_id=snapshot_id)


if __name__ == "__main__":
    unittest.main()
