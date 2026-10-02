"""组合经营快照服务的离线端到端验收。

覆盖：定义版本冻结 → 批次事实上报 → 勾稽与异常说明 → 双角色会签发布 →
从冻结输入复算核对 → 晚到渠道数据触发重述版本（旧版保留且数字不变）→
变化原因与影响范围展示 → 按品牌/板块/地区下钻与无权限明细隐藏 →
关闭并重新打开数据库后继续尚未完成的会签。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from beverage_ops_foundation.clock import FixedClock
from beverage_ops_foundation.service import DomainService
from beverage_snapshot import domain
from beverage_snapshot.service import SnapshotService
from beverage_snapshot.storage import SnapshotDatabase

PERIOD = "2026H1"


def _catalog() -> list[dict[str, str]]:
    specs = {
        "sales_volume": ("fact", "销量", "板块单位", "啤酒千升/白酒吨", ""),
        "premium_volume": ("fact", "高端销量", "板块单位", "高端及次高端销量", ""),
        "gross_revenue": ("fact", "营业额", "报告币种", "含税前营业额", ""),
        "premium_revenue": ("fact", "高端营业额", "报告币种", "高端及次高端营业额", ""),
        "cogs": ("fact", "销货成本", "报告币种", "已售商品成本", ""),
        "operating_expense": ("fact", "期间费用", "报告币种", "销售与管理费用", ""),
        "depreciation_amortization": ("fact", "折旧摊销", "报告币种", "折旧与摊销", ""),
        "premium_volume_share": ("derived", "高端销量占比", "ratio", "高端销量/总销量",
                                 "premium_volume / sales_volume"),
        "premium_revenue_share": ("derived", "高端营业额占比", "ratio", "高端营业额/总营业额",
                                  "premium_revenue / gross_revenue"),
        "unit_cost": ("derived", "单位成本", "成本/板块单位", "销货成本/销量",
                      "cogs / sales_volume"),
        "ebitda": ("derived", "EBITDA", "报告币种", "息税折旧摊销前利润",
                   "gross_revenue - cogs - operating_expense + depreciation_amortization"),
        "ebitda_margin": ("derived", "EBITDA 率", "ratio", "EBITDA/营业额", "ebitda / gross_revenue"),
        "yoy_growth": ("derived", "同比增长", "ratio", "营业额同比",
                       "gross_revenue(本期) / gross_revenue(上年同期) - 1"),
    }
    return [{"metric_id": m, "metric_kind": kind, "display_name": name, "unit": unit,
             "description": desc, "formula": formula}
            for m, (kind, name, unit, desc, formula) in specs.items()]


# (板块, 品牌, 地区) -> 期间 -> 指标值。
GROUPS: dict[tuple[str, str, str], dict[str, dict[str, float]]] = {
    ("beer", "SnowLite", "east"): {
        "2026H1": dict(sales_volume=1200.0, premium_volume=540.0, gross_revenue=9000.0,
                       premium_revenue=4800.0, cogs=5200.0, operating_expense=1600.0,
                       depreciation_amortization=400.0),
        "2025H1": dict(sales_volume=1100.0, premium_volume=440.0, gross_revenue=8000.0,
                       premium_revenue=4000.0, cogs=4800.0, operating_expense=1500.0,
                       depreciation_amortization=380.0),
    },
    ("beer", "SnowLite", "west"): {
        "2026H1": dict(sales_volume=600.0, premium_volume=210.0, gross_revenue=4000.0,
                       premium_revenue=1700.0, cogs=2400.0, operating_expense=800.0,
                       depreciation_amortization=200.0),
        "2025H1": dict(sales_volume=580.0, premium_volume=190.0, gross_revenue=3800.0,
                       premium_revenue=1500.0, cogs=2300.0, operating_expense=780.0,
                       depreciation_amortization=190.0),
    },
    ("baijiu", "AgedCellar", "east"): {
        "2026H1": dict(sales_volume=80.0, premium_volume=52.0, gross_revenue=12000.0,
                       premium_revenue=9000.0, cogs=4200.0, operating_expense=2600.0,
                       depreciation_amortization=500.0),
        "2025H1": dict(sales_volume=90.0, premium_volume=50.0, gross_revenue=13000.0,
                       premium_revenue=9200.0, cogs=4500.0, operating_expense=2700.0,
                       depreciation_amortization=520.0),
    },
}


def _report_facts(service: SnapshotService, counter: list[int], definition_version_id: str,
                  batch: str, period_facts: dict[tuple[str, str, str],
                                                 dict[str, dict[str, float]]], *,
                  late: bool = False) -> None:
    """按 (维度,期间,指标) 逐条上报事实，request_id 全局递增保证唯一。"""

    for (segment, brand, region), periods in period_facts.items():
        for period, metrics in periods.items():
            for metric, value in metrics.items():
                counter[0] += 1
                service.record_fact(
                    request_id=f"req-fact-{counter[0]}", actor_id="op1",
                    definition_version_id=definition_version_id, batch_id=batch,
                    source="channel_mix", segment=segment, brand=brand, region=region,
                    period=period, metric_id=metric, value=value, is_late=late)


def _bootstrap(snapshot: SnapshotService) -> None:
    # 组织/操作者建档仍由基础领域服务负责，两服务共用同一数据库与审计链。
    service = DomainService(snapshot.database, snapshot.clock)
    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="o1", name="集团")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="a1",
                           display_name="管理员", role="admin", organization_id="o1")
    service.register_actor(request_id="req-reviewer", actor_id="a1", new_actor_id="rv1",
                           display_name="复核人", role="reviewer", organization_id="o1")
    service.register_actor(request_id="req-operator", actor_id="a1", new_actor_id="op1",
                           display_name="业务分析", role="operator", organization_id="o1")
    service.register_actor(request_id="req-operator2", actor_id="a1", new_actor_id="op2",
                           display_name="啤酒品牌分析", role="operator", organization_id="o1")
    # op1 负责两个板块的复盘，op2 只有 SnowLite 一个品牌的范围。
    snapshot.grant_data_scope(request_id="req-scope-op1-beer", actor_id="a1",
                              target_actor_id="op1", scope_type="segment",
                              scope_value=domain.SEGMENT_BEER)
    snapshot.grant_data_scope(request_id="req-scope-op1-baijiu", actor_id="a1",
                              target_actor_id="op1", scope_type="segment",
                              scope_value=domain.SEGMENT_BAIJIU)
    snapshot.grant_data_scope(request_id="req-scope-op2", actor_id="a1",
                              target_actor_id="op2", scope_type="brand",
                              scope_value="SnowLite")


def _prepare_definition(service: SnapshotService) -> str:
    receipt = service.create_definition_version(
        request_id="req-def", actor_id="a1", fiscal_period=PERIOD,
        reporting_ccy="CNY", definitions=_catalog(),
        exchange_rates={"CNY": 1.0}, organization_scope=["o1"])
    definition_version_id = receipt["resource_id"]
    service.freeze_definition_version(request_id="req-def-freeze", actor_id="a1",
                                      definition_version_id=definition_version_id)
    return definition_version_id


def _explain_all_warnings(service: SnapshotService, snapshot_id: str, issues, tag: str) -> None:
    for issue in issues:
        if issue["status"] == "open":
            service.explain_issue(request_id=f"req-explain-{tag}-{issue['issue_id'][:8]}",
                                  actor_id="rv1", issue_id=issue["issue_id"],
                                  explanation="半年复盘口径已核对：晚到渠道批次与结构差异已留档")


def _drive_to_published(service: SnapshotService, snapshot_id: str, tag: str) -> None:
    """勾稽 → 说明全部预警 → 提交 → 复核人与管理员双角色会签发布。"""

    result = service.reconcile(actor_id="op1", snapshot_version_id=snapshot_id)
    _explain_all_warnings(service, snapshot_id, result["issues"], tag)
    service.submit_snapshot(actor_id="op1", snapshot_version_id=snapshot_id)
    service.approve_snapshot(request_id=f"req-approve-rv-{tag}", actor_id="rv1",
                             snapshot_version_id=snapshot_id,
                             decision="approved", comment="复核通过")
    service.approve_snapshot(request_id=f"req-approve-ad-{tag}",
                             actor_id="a1", snapshot_version_id=snapshot_id,
                             decision="approved", comment="授权发布")


def _group_metric(view: dict[str, object], segment: str, brand: str,
                  region: str, metric: str):
    for group in view["result"]["groups"]:
        if (group["segment"], group["brand"], group["region"]) == (segment, brand, region):
            return group["metrics"][metric]
    return None


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "snapshot_acceptance.sqlite3"
        database = SnapshotDatabase(db_path)
        service = SnapshotService(
            database, FixedClock(datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)))

        _bootstrap(service)
        definition_version_id = _prepare_definition(service)

        counter = [0]
        _report_facts(service, counter, definition_version_id, "batch-20260920", GROUPS)

        snapshot_v1 = service.create_snapshot(
            request_id="req-snap1", actor_id="op1",
            definition_version_id=definition_version_id, period=PERIOD)["resource_id"]
        _drive_to_published(service, snapshot_v1, "v1")

        v1_meta = service.get_snapshot_version("a1", snapshot_v1)
        v1_view = service.view_snapshot("a1", snapshot_v1)
        v1_verify = service.verify_snapshot("rv1", snapshot_v1)

        # 下钻：只看白酒板块；权限：op2 只能看到 SnowLite 品牌，白酒明细被隐藏。
        baijiu_only = service.view_snapshot("a1", snapshot_v1, segment="baijiu")
        limited = service.view_snapshot("op2", snapshot_v1)
        limited_brands = {g["brand"] for g in limited["result"]["groups"]}
        baijiu_as_op2 = service.view_snapshot("op2", snapshot_v1, segment="baijiu")

        # 晚到渠道数据：西部啤酒营业额 4000 -> 4300，只允许走重述新版本。
        late_batch_2 = {
            ("beer", "SnowLite", "west"): {PERIOD: {"gross_revenue": 4300.0}},
        }
        _report_facts(service, counter, definition_version_id,
                      "batch-20260925-late", late_batch_2, late=True)
        snapshot_v2 = service.create_restatement(
            request_id="req-restate2", actor_id="op1",
            definition_version_id=definition_version_id, period=PERIOD,
            reason=domain.RESTATEMENT_LATE_DATA,
            change_summary="西部渠道晚到对账批次上修啤酒营业额 300")["resource_id"]
        _drive_to_published(service, snapshot_v2, "v2")

        v2_meta = service.get_snapshot_version("a1", snapshot_v2)
        v1_after = service.get_snapshot_version("a1", snapshot_v1)
        v1_view_after = service.view_snapshot("a1", snapshot_v1)
        v1_verify_after = service.verify_snapshot("a1", snapshot_v1)
        v2_west = service.view_snapshot("a1", snapshot_v2, segment="beer", region="west")

        # 第三个晚到批次，v3 提交后停在待会签状态，用于验证跨重启续审。
        late_batch_3 = {
            ("beer", "SnowLite", "west"): {PERIOD: {"gross_revenue": 4350.0}},
        }
        _report_facts(service, counter, definition_version_id,
                      "batch-20260928-late", late_batch_3, late=True)
        snapshot_v3 = service.create_restatement(
            request_id="req-restate3", actor_id="op1",
            definition_version_id=definition_version_id, period=PERIOD,
            reason=domain.RESTATEMENT_LATE_DATA,
            change_summary="西部渠道补报再次上修啤酒营业额 50")["resource_id"]
        reconciled = service.reconcile(actor_id="op1", snapshot_version_id=snapshot_v3)
        _explain_all_warnings(service, snapshot_v3, reconciled["issues"], "v3")
        service.submit_snapshot(actor_id="op1", snapshot_version_id=snapshot_v3)

        versions_before = service.list_snapshot_versions("a1", definition_version_id, PERIOD)
        audit_valid_before, audit_count = service.verify_audit()
        database.close()

        # ---- 模拟服务重启：重新打开同一 SQLite 文件，继续未完成的会签 ----
        database2 = SnapshotDatabase(db_path)
        service2 = SnapshotService(database2)
        versions_after = service2.list_snapshot_versions("a1", definition_version_id, PERIOD)
        v3_waiting = service2.get_snapshot_version("a1", snapshot_v3)
        service2.approve_snapshot(request_id="req-approve3-rv", actor_id="rv1",
                                  snapshot_version_id=snapshot_v3,
                                  decision="approved", comment="重启后复核通过")
        service2.approve_snapshot(request_id="req-approve3-ad", actor_id="a1",
                                  snapshot_version_id=snapshot_v3,
                                  decision="approved", comment="重启后授权发布")
        v3_published = service2.get_snapshot_version("a1", snapshot_v3)
        v2_after_restart = service2.get_snapshot_version("a1", snapshot_v2)
        v3_verify = service2.verify_snapshot("a1", snapshot_v3)
        audit_valid_after, _ = service2.verify_audit()
        database2.close()

        impact = v2_meta["impact"]
        west_v1 = _group_metric(v1_view_after, "beer", "SnowLite", "west", "gross_revenue")
        west_v2 = _group_metric(v2_west, "beer", "SnowLite", "west", "gross_revenue")
        beer_ebitda_v1 = v1_view["result"]["segments"]["beer"]["ebitda"]

        checks = {
            "v1_published": v1_meta["state"] == "published",
            "v1_fingerprint_match": v1_verify["fingerprint_match"],
            "v1_results_recompute_from_frozen": v1_verify["results_match"],
            "derived_metrics_present": all(
                v1_view["result"]["segments"]["beer"][m] is not None
                for m in ("premium_volume_share", "premium_revenue_share",
                          "unit_cost", "ebitda", "ebitda_margin", "yoy_growth")),
            "beer_ebitda_value": beer_ebitda_v1 == 9000.0 + 4000.0 - 5200.0 - 2400.0
                                 - 1600.0 - 800.0 + 400.0 + 200.0,
            "yoy_value": v1_view["result"]["segments"]["baijiu"]["yoy_growth"] is not None,
            "cross_segment_volume_not_aggregated":
                v1_view["result"]["totals"].get("sales_volume") is None
                and v1_view["result"]["totals"]["unit_cost"] is None,
            "baijiu_drill_down_only": {g["segment"] for g in baijiu_only["result"]["groups"]} == {"baijiu"},
            "limited_sees_only_authorized_brand": limited_brands == {"SnowLite"},
            "limited_hides_unauthorized_detail":
                limited["scope_limited"] and limited["hidden_fact_count"] > 0
                and not baijiu_as_op2["result"]["groups"],
            "v1_becomes_superseded": v1_after["state"] == "superseded",
            "v1_number_unchanged_after_late_data": west_v1 == 4000.0,
            "v1_still_recomputes_after_restatement":
                v1_verify_after["fingerprint_match"] and v1_verify_after["results_match"],
            "v2_number_reflects_late_batch": west_v2 == 4300.0,
            "v2_published": v2_meta["state"] == "published",
            "v2_parent_is_v1": v2_meta["parent_version_id"] == snapshot_v1,
            "impact_shows_late_data_reason": impact["reason"] == domain.RESTATEMENT_LATE_DATA,
            "impact_scopes_west_beer": impact["affected_regions"] == ["west"]
                                       and impact["affected_segments"] == ["beer"]
                                       and impact["affected_brands"] == ["SnowLite"],
            "impact_totals_delta_300": impact["totals"]["gross_revenue"]["delta"] == 300.0,
            "three_versions_persisted": len(versions_before["items"]) == 3
                                         and len(versions_after["items"]) == 3,
            "approval_resumed_after_restart": v3_waiting["state"] == "submitted",
            "v3_published_after_restart": v3_published["state"] == "published",
            "v2_superseded_after_v3": v2_after_restart["state"] == "superseded",
            "v3_recompute_ok": v3_verify["fingerprint_match"] and v3_verify["results_match"],
            "audit_chain_valid": audit_valid_before and audit_valid_after,
        }
        return {"status": "ok" if all(checks.values()) else "failed",
                "checks": checks, "audit_events": audit_count,
                "versions": [v["version_no"] for v in versions_after["items"]]}


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
