"""组合经营快照服务的离线端到端验收。

覆盖：指标定义/范围/币种版本化、事实接收、冻结复算、勾稽阻断与警告说明、
修订留痕、服务重启后继续会签、授权发布、晚到批次触发重述、差异报告、
冻结复算一致性与无权限明细隐藏。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..audit import verify_chain
from ..clock import FixedClock
from ..errors import PermissionDenied
from ..service import DomainService
from ..storage import Database
from .schema import SNAPSHOT_SCHEMA
from .service import SnapshotService

DEFINITION = {
    "presentation_currency": "CNY",
    "metrics": [
        {"metric_id": "revenue", "kind": "sum", "measure": "revenue", "display_name": "营业额"},
        {"metric_id": "volume", "kind": "sum", "measure": "volume_hl", "display_name": "销量(百升)"},
        {"metric_id": "premium_revenue", "kind": "sum", "measure": "premium_revenue",
         "display_name": "高端营业额"},
        {"metric_id": "premium_share", "kind": "ratio",
         "numerator_measure": "premium_revenue", "denominator_measure": "revenue",
         "display_name": "高端产品占比"},
        {"metric_id": "unit_cost", "kind": "ratio",
         "numerator_measure": "cogs", "denominator_measure": "volume_hl",
         "display_name": "单位成本"},
        {"metric_id": "ebitda", "kind": "sum", "measure": "ebitda", "display_name": "EBITDA"},
        {"metric_id": "ebitda_margin", "kind": "ratio",
         "numerator_measure": "ebitda", "denominator_measure": "revenue",
         "display_name": "EBITDA 利润率"},
        {"metric_id": "revenue_yoy", "kind": "yoy", "base_metric": "revenue",
         "display_name": "营业额同比"},
    ],
    "checks": [
        {"type": "required_measures", "measures": ["revenue", "volume_hl"], "severity": "blocking"},
        {"type": "required_measures", "measures": ["premium_revenue"], "severity": "warning"},
        {"type": "measure_order", "rules": ["premium_revenue <= revenue"], "severity": "blocking"},
    ],
    "required_signoff_roles": ["admin", "reviewer"],
}

NODES = [
    {"node_id": "g1", "node_type": "group", "name": "集团"},
    {"node_id": "beer", "parent_id": "g1", "node_type": "segment", "name": "啤酒板块",
     "segment": "beer"},
    {"node_id": "snow", "parent_id": "beer", "node_type": "brand", "name": "雪花品牌",
     "brand_id": "snow", "region": "east"},
    {"node_id": "bud", "parent_id": "beer", "node_type": "brand", "name": "百威品牌",
     "brand_id": "bud", "region": "east"},
    {"node_id": "baijiu", "parent_id": "g1", "node_type": "segment", "name": "白酒板块",
     "segment": "baijiu"},
    {"node_id": "laojiao", "parent_id": "baijiu", "node_type": "brand", "name": "老窖品牌",
     "brand_id": "laojiao", "region": "west"},
]

CURRENCIES = {"rates": {"CNY": "1"}, "as_of_date": "2026-06-30"}

FACTS_2025 = {
    "f-2025-snow": ("snow", {"revenue": 1000, "premium_revenue": 300, "volume_hl": 100,
                             "cogs": 600, "ebitda": 200}),
    "f-2025-bud": ("bud", {"revenue": 800, "premium_revenue": 200, "volume_hl": 80,
                           "cogs": 500, "ebitda": 120}),
    "f-2025-laojiao": ("laojiao", {"revenue": 900, "volume_hl": 50, "cogs": 400, "ebitda": 300}),
}

FACTS_2026_BAD = {"revenue": 1100, "premium_revenue": 1400, "volume_hl": 110,
                  "cogs": 640, "ebitda": 260}  # 高端 > 总营业额，必须被勾稽拦截
FACTS_2026_SNOW = {"revenue": 1100, "premium_revenue": 440, "volume_hl": 110,
                   "cogs": 640, "ebitda": 260}
FACTS_2026_BUD = {"revenue": 820, "premium_revenue": 246, "volume_hl": 82,
                  "cogs": 510, "ebitda": 130}
FACTS_2026_LAOJIAO = {"revenue": 810, "volume_hl": 45, "cogs": 390, "ebitda": 210}
FACT_LATE = {"revenue": 60, "premium_revenue": 30, "volume_hl": 6, "cogs": 30, "ebitda": 15}


def _bootstrap(database: Database, clock: FixedClock) -> tuple[DomainService, SnapshotService]:
    domain = DomainService(database, clock)
    snap = SnapshotService(database, clock)
    domain.register_organization(request_id="org", actor_id="bootstrap",
                                 organization_id="o1", name="示范酒业集团")
    domain.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                          display_name="管理员", role="admin", organization_id="o1")
    domain.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                          display_name="业务操作员", role="operator", organization_id="o1")
    domain.register_actor(request_id="rv", actor_id="a1", new_actor_id="rv1",
                          display_name="复盘审核人", role="reviewer", organization_id="o1")
    domain.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                          display_name="啤酒板块经办", role="operator", organization_id="o1")
    return domain, snap


def _publish(snap: SnapshotService, version_id: str, *, req: str, note: str = "验收口径说明") -> None:
    findings = snap.list_findings("op1", version_id)
    warning_keys = [item["finding_key"] for item in findings["items"]
                    if item["severity"] == "warning"]
    if warning_keys:
        snap.acknowledge_findings(request_id=f"{req}-ack", actor_id="op1",
                                  version_id=version_id, finding_keys=warning_keys, note=note)
    snap.submit_version(request_id=f"{req}-submit", actor_id="op1", version_id=version_id)
    snap.sign_off(request_id=f"{req}-rv", actor_id="rv1", version_id=version_id,
                  decision="approved", comment="审核通过")
    snap.sign_off(request_id=f"{req}-adm", actor_id="a1", version_id=version_id,
                  decision="approved", comment="管理会签通过")
    snap.sanction_publication(request_id=f"{req}-pub", actor_id="a1", version_id=version_id,
                              comment="授权发布")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "snapshot_acceptance.sqlite3"
        clock = FixedClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        database = Database(path, extra_schema=SNAPSHOT_SCHEMA)
        _domain, snap = _bootstrap(database, clock)

        snap.register_metric_definition(request_id="def", actor_id="a1",
                                        definition_id="review-def", body=DEFINITION)
        snap.register_scope(request_id="scope", actor_id="a1", scope_id="group-scope",
                            nodes=NODES, currencies=CURRENCIES)
        snap.grant_node_access(request_id="grant-beer", actor_id="a1", target_actor_id="op2",
                               node_ids=["beer"])

        # 上年同期：发布 2025H1 快照，作为同比基准。
        for fact_id, (node_id, measures) in FACTS_2025.items():
            snap.record_fact(request_id=f"rec-{fact_id}", actor_id="op1", fact_id=fact_id,
                             node_id=node_id, batch_id="b-2025-final", source="erp",
                             period_code="2025H1", currency_code="CNY", measures=measures)
        prior = snap.build_version(request_id="build-2025", actor_id="op1",
                                   scope_id="group-scope", definition_id="review-def",
                                   period_code="2025H1")
        _publish(snap, prior["version_id"], req="v2025")

        # 本期：初版事实含勾稽错误。
        snap.record_fact(request_id="rec-2026-snow", actor_id="op1", fact_id="f-2026-snow",
                         node_id="snow", batch_id="b-2026-1", source="distributor-feed",
                         period_code="2026H1", currency_code="CNY", measures=FACTS_2026_BAD)
        snap.record_fact(request_id="rec-2026-bud", actor_id="op1", fact_id="f-2026-bud",
                         node_id="bud", batch_id="b-2026-1", source="erp",
                         period_code="2026H1", currency_code="CNY", measures=FACTS_2026_BUD)
        snap.record_fact(request_id="rec-2026-lj", actor_id="op1", fact_id="f-2026-laojiao",
                         node_id="laojiao", batch_id="b-2026-1", source="erp",
                         period_code="2026H1", currency_code="CNY", measures=FACTS_2026_LAOJIAO)
        draft = snap.build_version(request_id="build-2026", actor_id="op1",
                                   scope_id="group-scope", definition_id="review-def",
                                   period_code="2026H1")
        assert draft["blocking_findings"] == 1, draft
        blocked_submit = False
        try:
            snap.submit_version(request_id="blocked-submit", actor_id="op1",
                                version_id=draft["version_id"])
        except Exception:
            blocked_submit = True
        assert blocked_submit

        # 渠道修正以同一 fact_id 写回：留痕修订而非静默覆盖，然后重算草稿。
        revised = snap.record_fact(request_id="rec-2026-snow-fix", actor_id="op1",
                                   fact_id="f-2026-snow", node_id="snow", batch_id="b-2026-1r",
                                   source="distributor-feed", period_code="2026H1",
                                   currency_code="CNY", measures=FACTS_2026_SNOW,
                                   note="渠道回传高端金额串列，按更正批次重报")
        assert revised["revised"] is True
        rebuilt = snap.rebuild_version(request_id="rebuild-2026", actor_id="op1",
                                       version_id=draft["version_id"])
        assert rebuilt["blocking_findings"] == 0 and rebuilt["warning_findings"] == 1, rebuilt

        # 白酒节点没有高端口径：警告必须逐条说明，阻断性异常不能用说明替代。
        findings = snap.list_findings("op1", draft["version_id"])
        warning_keys = [item["finding_key"] for item in findings["items"]
                        if item["severity"] == "warning"]
        snap.acknowledge_findings(request_id="ack-2026", actor_id="op1",
                                  version_id=draft["version_id"], finding_keys=warning_keys,
                                  note="白酒白酒调整期高端产品线暂不填报，口径上不适用")
        snap.submit_version(request_id="submit-2026", actor_id="op1",
                            version_id=draft["version_id"])

        # 服务重启：关闭并重新打开同一个 SQLite 文件，待会签流程继续推进。
        database.close()
        database = Database(path, extra_schema=SNAPSHOT_SCHEMA)
        snap = SnapshotService(database, clock)
        reopened_meta = snap.get_version("a1", draft["version_id"])
        assert reopened_meta["status"] == "pending"
        snap.sign_off(request_id="sign-rv-2026", actor_id="rv1", version_id=draft["version_id"],
                      decision="approved", comment="勾稽与说明齐备")
        snap.sign_off(request_id="sign-adm-2026", actor_id="a1", version_id=draft["version_id"],
                      decision="approved", comment="管理会签通过")
        snap.sanction_publication(request_id="publish-2026", actor_id="a1",
                                  version_id=draft["version_id"], comment="半年复盘发布")
        published = snap.get_version("a1", draft["version_id"])
        assert published["status"] == "published"
        frozen_hash_before_late = published["results_hash"]

        # 发布后复算必须一致；集团高端占比 = (440+246)/(1100+820+810) ≈ 0.251282（白酒无高端口径）。
        verify = snap.recompute_version("a1", draft["version_id"])
        assert verify["matches"] is True
        group_data = snap.query_snapshot("a1", draft["version_id"], node_id="g1")
        metrics_by_node = {item["node_id"]: item["metrics"] for item in group_data["items"]}
        group_metrics = metrics_by_node["g1"]
        premium_share = group_metrics["premium_share"]
        assert abs(premium_share - (440 + 246) / 2730) < 1e-6, premium_share
        beer_share = metrics_by_node["beer"]["premium_share"]
        assert abs(beer_share - (440 + 246) / (1100 + 820)) < 1e-6, beer_share
        # 单位成本与 EBITDA 利润率来自冻结事实的确定性复算。
        unit_cost_snow = metrics_by_node["snow"]["unit_cost"]
        assert abs(unit_cost_snow - 640 / 110) < 1e-6, unit_cost_snow
        assert abs(metrics_by_node["g1"]["ebitda_margin"] - (260 + 130 + 210) / 2730) < 1e-6
        revenue_yoy = group_metrics["revenue_yoy"]
        assert abs(revenue_yoy - (2730 - 2700) / 2700) < 1e-6, revenue_yoy

        # 晚到渠道批次：已发布版本不能重建，只能生成重述版本。
        snap.record_fact(request_id="rec-late", actor_id="op1", fact_id="f-2026-late-bud",
                         node_id="bud", batch_id="b-late-east", source="east-channel",
                         period_code="2026H1", currency_code="CNY", measures=FACT_LATE,
                         observed_at="2026-07-05T10:00:00Z")
        old_meta = snap.get_version("a1", draft["version_id"])
        assert old_meta["facts"]["late_batches"] == ["b-late-east"]
        rebuild_denied = False
        try:
            snap.rebuild_version(request_id="rebuild-published", actor_id="op1",
                                 version_id=draft["version_id"])
        except Exception:
            rebuild_denied = True
        assert rebuild_denied

        restatement = snap.build_version(
            request_id="build-restate", actor_id="op1", scope_id="group-scope",
            definition_id="review-def", period_code="2026H1", kind="restatement",
            restates_version_id=draft["version_id"],
            change_reason="华东经销渠道晚到批次 b-late-east，补录百威端午档期销售")
        diff = snap.get_version("a1", restatement["version_id"])["diff"]
        assert diff["facts_added"] == ["f-2026-late-bud"], diff
        assert "bud" in diff["impacted_node_ids"]
        _publish(snap, restatement["version_id"], req="restate")
        old_now = snap.get_version("a1", draft["version_id"])
        assert old_now["status"] == "superseded"
        assert old_now["restated_by_version_id"] == restatement["version_id"]
        assert snap.recompute_version("a1", draft["version_id"])["matches"] is True
        assert old_now["results_hash"] == frozen_hash_before_late  # 已发布数字永不改变

        # 权限下钻：op2 仅被授权啤酒板块，看不到集团根与白酒明细。
        denied = False
        try:
            snap.query_snapshot("op2", draft["version_id"], node_id="g1")
        except PermissionDenied:
            denied = True
        assert denied
        visible = snap.query_snapshot("op2", restatement["version_id"], drill="segment")
        visible_ids = {item["node_id"] for item in visible["items"]}
        assert visible_ids == {"beer", "snow", "bud"}, visible_ids
        assert visible["partial"] is True and visible["hidden_nodes_count"] >= 2
        assert {row["segment"] for row in visible["drill"]} == {"beer"}
        by_brand = snap.query_snapshot("op2", restatement["version_id"], drill="brand")
        assert {row["brand"] for row in by_brand["drill"]} == {"snow", "bud"}

        valid, event_count = verify_chain(database.connection)
        database.close()
        return {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "published_serial": published["serial"],
            "premium_share_2026h1": round(premium_share, 6),
            "revenue_yoy_2026h1": round(revenue_yoy, 6),
            "restatement_serial": restatement["serial"],
            "restatement_impacted_nodes": diff["impacted_node_ids"],
            "old_version_immutable": old_now["results_hash"] == frozen_hash_before_late,
            "resumed_after_restart": reopened_meta["status"] == "pending",
            "hidden_nodes_count": visible["hidden_nodes_count"],
        }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
