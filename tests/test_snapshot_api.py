import unittest

from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.api import route as foundation_route
from beverage_snapshot.api import route
from beverage_snapshot.service import SnapshotService
from beverage_snapshot.storage import SnapshotDatabase


CATALOG = [
    {"metric_id": "sales_volume", "metric_kind": "fact", "display_name": "销量",
     "unit": "u", "description": "d", "formula": ""},
    {"metric_id": "premium_volume", "metric_kind": "fact", "display_name": "高端销量",
     "unit": "u", "description": "d", "formula": ""},
    {"metric_id": "gross_revenue", "metric_kind": "fact", "display_name": "营业额",
     "unit": "CNY", "description": "d", "formula": ""},
    {"metric_id": "premium_revenue", "metric_kind": "fact", "display_name": "高端营业额",
     "unit": "CNY", "description": "d", "formula": ""},
    {"metric_id": "cogs", "metric_kind": "fact", "display_name": "销货成本",
     "unit": "CNY", "description": "d", "formula": ""},
    {"metric_id": "operating_expense", "metric_kind": "fact", "display_name": "期间费用",
     "unit": "CNY", "description": "d", "formula": ""},
    {"metric_id": "depreciation_amortization", "metric_kind": "fact",
     "display_name": "折旧摊销", "unit": "CNY", "description": "d", "formula": ""},
    {"metric_id": "premium_volume_share", "metric_kind": "derived",
     "display_name": "高端销量占比", "unit": "ratio", "description": "d",
     "formula": "premium_volume / sales_volume"},
    {"metric_id": "premium_revenue_share", "metric_kind": "derived",
     "display_name": "高端营业额占比", "unit": "ratio", "description": "d",
     "formula": "premium_revenue / gross_revenue"},
    {"metric_id": "unit_cost", "metric_kind": "derived", "display_name": "单位成本",
     "unit": "x", "description": "d", "formula": "cogs / sales_volume"},
    {"metric_id": "ebitda", "metric_kind": "derived", "display_name": "EBITDA",
     "unit": "CNY", "description": "d",
     "formula": "gross_revenue - cogs - operating_expense + depreciation_amortization"},
    {"metric_id": "ebitda_margin", "metric_kind": "derived", "display_name": "EBITDA率",
     "unit": "ratio", "description": "d", "formula": "ebitda / gross_revenue"},
    {"metric_id": "yoy_growth", "metric_kind": "derived", "display_name": "同比",
     "unit": "ratio", "description": "d", "formula": "gross_revenue / 上年同期 - 1"},
]


class SnapshotApiTest(unittest.TestCase):
    def setUp(self):
        self.database = SnapshotDatabase()
        self.service = SnapshotService(self.database)
        self.foundation = DomainService(self.database)
        foundation_route(self.foundation, "POST", "/organizations",
                         {"request_id": "org", "organization_id": "o1", "name": "集团"},
                         {"X-Actor-Id": "bootstrap"})
        for req, aid, name, role in (
                ("ad", "a1", "管理员", "admin"),
                ("rv", "rv1", "复核", "reviewer"),
                ("op", "op1", "分析", "operator")):
            status, _ = foundation_route(self.foundation, "POST", "/actors",
                                         {"request_id": req, "new_actor_id": aid,
                                          "display_name": name, "role": role,
                                          "organization_id": "o1"},
                                         {"X-Actor-Id": "bootstrap" if aid == "a1" else "a1"})
            self.assertEqual(201, status)
        route(self.service, "POST", "/data-scopes",
              {"request_id": "sc1", "target_actor_id": "op1",
               "scope_type": "segment", "scope_value": "beer"},
              {"X-Actor-Id": "a1"})
        status, payload = route(self.service, "POST", "/definition-versions",
                                {"request_id": "def", "fiscal_period": "2026H1",
                                 "reporting_ccy": "CNY", "definitions": CATALOG,
                                 "exchange_rates": {"CNY": 1.0},
                                 "organization_scope": ["o1"]},
                                {"X-Actor-Id": "a1"})
        self.assertEqual(201, status)
        self.def_id = payload["resource_id"]
        route(self.service, "POST", "/definition-versions/freeze",
              {"request_id": "fr", "definition_version_id": self.def_id},
              {"X-Actor-Id": "a1"})

    def tearDown(self):
        self.database.close()

    def test_health(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route(self):
        status, payload = route(self.service, "GET", "/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_full_publish_and_restatement_flow_over_http(self):
        values = {"sales_volume": (1000.0, None), "premium_volume": (400.0, None),
                  "gross_revenue": (8000.0, None), "premium_revenue": (4000.0, None),
                  "cogs": (4800.0, None), "operating_expense": (1200.0, None),
                  "depreciation_amortization": (300.0, None)}
        for i, (metric, (value, _)) in enumerate(values.items()):
            status, _ = route(self.service, "POST", "/facts",
                              {"request_id": f"f{i}", "definition_version_id": self.def_id,
                               "batch_id": "b1", "source": "channel", "segment": "beer",
                               "brand": "Brew1", "region": "east", "period": "2026H1",
                               "metric_id": metric, "value": value},
                              {"X-Actor-Id": "op1"})
            self.assertEqual(201, status)
        status, snap = route(self.service, "POST", "/snapshots",
                             {"request_id": "s1", "definition_version_id": self.def_id,
                              "period": "2026H1"}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        snap_id = snap["resource_id"]

        status, recon = route(self.service, "POST", "/snapshots/reconcile",
                              {"snapshot_version_id": snap_id}, {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        for issue in recon["issues"]:
            if issue["status"] == "open":
                route(self.service, "POST", "/issues/explain",
                      {"request_id": f"e-{issue['issue_id'][:6]}",
                       "issue_id": issue["issue_id"], "explanation": "留档"},
                      {"X-Actor-Id": "rv1"})
        route(self.service, "POST", "/snapshots/submit",
              {"snapshot_version_id": snap_id}, {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/snapshots/approve",
              {"request_id": "ap1", "snapshot_version_id": snap_id,
               "decision": "approved"}, {"X-Actor-Id": "rv1"})
        route(self.service, "POST", "/snapshots/approve",
              {"request_id": "ap2", "snapshot_version_id": snap_id,
               "decision": "approved"}, {"X-Actor-Id": "a1"})
        status, meta = route(self.service, "GET", f"/snapshot-version?id={snap_id}", None,
                             {"X-Actor-Id": "a1"})
        self.assertEqual(200, status)
        self.assertEqual("published", meta["state"])

        # 晚到批次 -> 重述 v2。
        route(self.service, "POST", "/facts",
              {"request_id": "late", "definition_version_id": self.def_id,
               "batch_id": "b2-late", "source": "channel", "segment": "beer",
               "brand": "Brew1", "region": "east", "period": "2026H1",
               "metric_id": "gross_revenue", "value": 8300.0, "is_late": True},
              {"X-Actor-Id": "op1"})
        status, restate = route(self.service, "POST", "/snapshots/restate",
                                {"request_id": "rs", "definition_version_id": self.def_id,
                                 "period": "2026H1", "reason": "late_channel_data",
                                 "change_summary": "晚到上修 300"},
                                {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        v2 = restate["resource_id"]
        status, recon2 = route(self.service, "POST", "/snapshots/reconcile",
                               {"snapshot_version_id": v2}, {"X-Actor-Id": "op1"})
        for issue in recon2["issues"]:
            if issue["status"] == "open":
                route(self.service, "POST", "/issues/explain",
                      {"request_id": f"e2-{issue['issue_id'][:6]}",
                       "issue_id": issue["issue_id"], "explanation": "晚到批次"},
                      {"X-Actor-Id": "rv1"})
        route(self.service, "POST", "/snapshots/submit",
              {"snapshot_version_id": v2}, {"X-Actor-Id": "op1"})
        route(self.service, "POST", "/snapshots/approve",
              {"request_id": "ap3", "snapshot_version_id": v2, "decision": "approved"},
              {"X-Actor-Id": "rv1"})
        route(self.service, "POST", "/snapshots/approve",
              {"request_id": "ap4", "snapshot_version_id": v2, "decision": "approved"},
              {"X-Actor-Id": "a1"})
        status, versions = route(
            self.service, "GET",
            f"/snapshot-versions?definition_version_id={self.def_id}&period=2026H1",
            None, {"X-Actor-Id": "a1"})
        self.assertEqual(200, status)
        self.assertEqual([1, 2], [v["version_no"] for v in versions["items"]])
        v2_meta = next(v for v in versions["items"] if v["version_no"] == 2)
        self.assertEqual(300.0, v2_meta["impact"]["totals"]["gross_revenue"]["delta"])

    def test_drill_down_query_params(self):
        status, payload = route(
            self.service, "GET",
            f"/facts?definition_version_id={self.def_id}&segment=beer",
            None, {"X-Actor-Id": "op1"})
        self.assertEqual(200, status)
        self.assertEqual([], payload["items"])

    def test_missing_actor_is_rejected(self):
        status, payload = route(
            self.service, "GET",
            f"/snapshot-versions?definition_version_id={self.def_id}", None)
        # 缺 actor 时操作者不存在 -> 404。
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
