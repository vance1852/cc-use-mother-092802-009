"""组合经营指标目录与确定性复算引擎。

啤酒与白酒的销量、价位带、营业额与 EBITDA 口径不同，本模块只依据
*冻结的*指标定义、币种汇率、组织范围与经营事实进行计算。
同样的冻结输入必然得到同样的结构化结果与摘要，供发布前勾稽和发布后复算。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

MONEY_SCALE = Decimal("0.01")
VOLUME_SCALE = Decimal("0.001")
RATIO_SCALE = Decimal("0.000001")
ZERO = Decimal("0")

MONETARY_MEASURES = frozenset({"revenue", "premium_revenue", "cogs", "ebitda"})
VOLUME_MEASURES = frozenset({"volume_hl", "premium_volume_hl"})
KNOWN_MEASURES = MONETARY_MEASURES | VOLUME_MEASURES

METRIC_KINDS = frozenset({"sum", "ratio", "yoy"})
CHECK_TYPES = frozenset({
    "required_measures",
    "measure_order",
    "rollup_consistency",
})


class MetricDefinitionError(ValueError):
    """指标定义文档无法被引擎接受。"""


def _round(measure_or_ratio: str, value: Decimal) -> Decimal:
    if measure_or_ratio in MONETARY_MEASURES:
        return value.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
    if measure_or_ratio in VOLUME_MEASURES:
        return value.quantize(VOLUME_SCALE, rounding=ROUND_HALF_UP)
    return value.quantize(RATIO_SCALE, rounding=ROUND_HALF_UP)


def to_number(value: Decimal | None) -> float | None:
    """把定点 Decimal 转成可序列化数字；None 表示该节点没有口径数据。"""

    if value is None:
        return None
    return float(value)


def validate_definition(body: dict[str, Any]) -> None:
    """校验一份指标定义文档的结构。"""

    metrics = body.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise MetricDefinitionError("metrics 必须是非空数组")
    seen: set[str] = set()
    for metric in metrics:
        if not isinstance(metric, dict):
            raise MetricDefinitionError("metric 必须是对象")
        metric_id = metric.get("metric_id")
        if not isinstance(metric_id, str) or not metric_id:
            raise MetricDefinitionError("metric_id 不能为空")
        if metric_id in seen:
            raise MetricDefinitionError(f"metric_id 重复: {metric_id}")
        seen.add(metric_id)
        kind = metric.get("kind")
        if kind not in METRIC_KINDS:
            raise MetricDefinitionError(f"{metric_id} 的 kind 不受支持: {kind}")
        if kind == "sum":
            if metric.get("measure") not in KNOWN_MEASURES:
                raise MetricDefinitionError(f"{metric_id} 的 measure 不受支持")
        elif kind == "ratio":
            for field in ("numerator_measure", "denominator_measure"):
                if metric.get(field) not in KNOWN_MEASURES:
                    raise MetricDefinitionError(f"{metric_id} 的 {field} 不受支持")
        elif kind == "yoy":
            if not isinstance(metric.get("base_metric"), str):
                raise MetricDefinitionError(f"{metric_id} 的 yoy 必须声明 base_metric")
    for metric in metrics:
        if metric["kind"] == "yoy" and metric["base_metric"] not in seen:
            raise MetricDefinitionError(f"{metric['metric_id']} 的 base_metric 未在 metrics 中声明")
    checks = body.get("checks", [])
    if not isinstance(checks, list):
        raise MetricDefinitionError("checks 必须是数组")
    for check in checks:
        if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES:
            raise MetricDefinitionError("check.type 不受支持")
        if check.get("severity", "blocking") not in ("blocking", "warning"):
            raise MetricDefinitionError("check.severity 只能是 blocking 或 warning")
    roles = body.get("required_signoff_roles", [])
    if not isinstance(roles, list) or not roles or any(r not in ("admin", "reviewer") for r in roles):
        raise MetricDefinitionError("required_signoff_roles 必须包含 admin/reviewer 中的角色")
    if not isinstance(body.get("presentation_currency"), str) or len(body["presentation_currency"]) != 3:
        raise MetricDefinitionError("presentation_currency 必须是三位币种代码")


def _rate(fx: dict[str, Decimal], currency: str, presentation: str) -> Decimal | None:
    if currency == presentation:
        return Decimal("1")
    return fx.get(currency)


def evaluate(
    *,
    definition: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    fx: dict[str, Decimal],
    facts: list[dict[str, Any]],
    comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从冻结输入计算节点度量、衍生指标与勾稽线索。

    facts 的每项至少包含 fact_id、node_id、batch_id、currency_code、measures。
    函数不依赖数据库连接，便于发布后从冻结输入独立复算。
    """

    presentation = definition["presentation_currency"]
    fx_findings: list[dict[str, Any]] = []

    # 1) 按事实归属节点汇总“自有”度量（汇率换算为发布币种）。
    own_totals: dict[str, dict[str, Decimal]] = {node_id: {} for node_id in nodes}
    own_present: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    fact_count: dict[str, int] = {node_id: 0 for node_id in nodes}
    batches: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    out_of_scope: set[str] = set()

    for fact in sorted(facts, key=lambda item: item["fact_id"]):
        node_id = fact["node_id"]
        if node_id not in nodes:
            out_of_scope.add(fact["fact_id"])
            continue
        rate = _rate(fx, fact["currency_code"], presentation)
        if rate is None:
            fx_findings.append({
                "check_type": "fx_rate_missing",
                "severity": "blocking",
                "node_id": node_id,
                "metric_id": fact["currency_code"],
                "message": f"币种 {fact['currency_code']} 缺少到 {presentation} 的冻结汇率",
            })
            continue
        measures = fact.get("measures") or {}
        for measure in sorted(measures):
            value = Decimal(str(measures[measure])) * (rate if measure in MONETARY_MEASURES else Decimal("1"))
            own_totals[node_id][measure] = own_totals[node_id].get(measure, ZERO) + value
            own_present[node_id].add(measure)
        fact_count[node_id] += 1
        batches[node_id].add(fact["batch_id"])

    # 2) 按组织范围子树自底向上合并，避免集团口径与品牌口径重复计数。
    children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        parent = node.get("parent_id")
        if parent:
            children[parent].append(node_id)

    subtree_totals: dict[str, dict[str, Decimal]] = {}
    subtree_present: dict[str, set[str]] = {}

    def walk(node_id: str) -> None:
        totals = dict(own_totals[node_id])
        present = set(own_present[node_id])
        for child in sorted(children.get(node_id, ())):
            if child not in subtree_totals:
                walk(child)
            for measure, value in subtree_totals[child].items():
                totals[measure] = totals.get(measure, ZERO) + value
            present.update(subtree_present[child])
        subtree_totals[node_id] = totals
        subtree_present[node_id] = present

    for node_id in sorted(nodes):
        if node_id not in subtree_totals:
            walk(node_id)

    # 3) 计算指标：汇总、占比/单位成本类比率、同比。
    comparison_nodes = (comparison or {}).get("nodes", {})
    result_nodes: dict[str, Any] = {}
    for node_id in sorted(nodes):
        node = nodes[node_id]
        totals = subtree_totals[node_id]
        present = subtree_present[node_id]
        measures_out = {measure: to_number(_round(measure, totals[measure])) for measure in sorted(present)}
        metrics_out: dict[str, float | None] = {}
        # 第一遍：汇总与比率指标。
        for metric in definition["metrics"]:
            metric_id = metric["metric_id"]
            kind = metric["kind"]
            value: Decimal | None = None
            if kind == "sum":
                measure = metric["measure"]
                if measure in present:
                    value = _round(measure, totals[measure])
            elif kind == "ratio":
                numerator_m = metric["numerator_measure"]
                denominator_m = metric["denominator_measure"]
                if numerator_m in present and denominator_m in present and totals[denominator_m] != 0:
                    value = _round(metric_id, totals[numerator_m] / totals[denominator_m])
            metrics_out[metric_id] = to_number(value)
        # 第二遍：同比依赖第一遍的基础指标，与其声明位置无关。
        for metric in definition["metrics"]:
            if metric["kind"] != "yoy":
                continue
            metric_id = metric["metric_id"]
            base = metric["base_metric"]
            current = metrics_out.get(base)
            previous = comparison_nodes.get(node_id, {}).get("metrics", {}).get(base)
            if current is not None and previous not in (None, 0):
                metrics_out[metric_id] = to_number(
                    _round(metric_id, (Decimal(str(current)) - Decimal(str(previous))) / Decimal(str(previous))))
        result_nodes[node_id] = {
            "node": {
                "node_id": node_id,
                "parent_id": node.get("parent_id"),
                "node_type": node.get("node_type"),
                "name": node.get("name"),
                "brand_id": node.get("brand_id"),
                "segment": node.get("segment"),
                "region": node.get("region"),
            },
            "has_own_facts": fact_count[node_id] > 0,
            "fact_count": fact_count[node_id],
            "batches": sorted(batches[node_id]),
            "measures": measures_out,
            "metrics": metrics_out,
        }

    roots = sorted(nid for nid, node in nodes.items() if not node.get("parent_id"))
    return {
        "presentation_currency": presentation,
        "roots": roots,
        "nodes": result_nodes,
        "out_of_scope_fact_ids": sorted(out_of_scope),
        "fx_findings": fx_findings,
    }


def run_checks(
    *,
    definition: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    results: dict[str, Any],
    facts: list[dict[str, Any]],
    fx: dict[str, Decimal] | None = None,
) -> list[dict[str, Any]]:
    """在复算结果上执行定义中声明的勾稽规则，返回异常清单。"""

    fx = fx or {}
    findings = list(results["fx_findings"])
    for fact_id in results["out_of_scope_fact_ids"]:
        findings.append({
            "check_type": "scope_membership",
            "severity": "blocking",
            "node_id": None,
            "metric_id": fact_id,
            "message": f"事实 {fact_id} 归属的节点不在本版本组织范围内",
        })

    for check in definition.get("checks", []):
        check_type = check["type"]
        severity = check.get("severity", "blocking")
        if check_type == "required_measures":
            required = check.get("measures", [])
            for node_id in sorted(nodes):
                node_result = results["nodes"][node_id]
                if not node_result["has_own_facts"]:
                    continue
                for measure in sorted(required):
                    if measure not in node_result["measures"]:
                        findings.append({
                            "check_type": "required_measures",
                            "severity": severity,
                            "node_id": node_id,
                            "metric_id": measure,
                            "message": f"节点 {node_id} 缺少必需度量 {measure}",
                        })
        elif check_type == "measure_order":
            for rule in check.get("rules", []):
                left_m, _, right_m = rule.partition("<=")
                left_m, right_m = left_m.strip(), right_m.strip()
                if not left_m or not right_m:
                    continue
                for node_id in sorted(nodes):
                    measures = results["nodes"][node_id]["measures"]
                    if left_m in measures and right_m in measures and measures[left_m] > measures[right_m]:
                        findings.append({
                            "check_type": "measure_order",
                            "severity": severity,
                            "node_id": node_id,
                            "metric_id": rule,
                            "message": f"节点 {node_id} 的 {left_m} 大于 {right_m}，口径关系不成立",
                        })
        elif check_type == "rollup_consistency":
            metric_id = check.get("metric_id")
            tolerance = Decimal(str(check.get("tolerance", 0.01)))
            metric = next((m for m in definition["metrics"] if m["metric_id"] == metric_id), None)
            if metric is None or metric["kind"] != "sum":
                continue
            measure = metric["measure"]

            def converted(fact: dict[str, Any]) -> Decimal:
                value = Decimal(str(fact["measures"][measure]))
                if measure in MONETARY_MEASURES:
                    rate = fx.get(fact["currency_code"])
                    if rate is not None:
                        value = value * rate
                return value

            children_map: dict[str, list[str]] = {}
            for nid, node in nodes.items():
                children_map.setdefault(node.get("parent_id"), []).append(nid)

            def descendants(root: str) -> set[str]:
                result: set[str] = set()
                stack = [root]
                while stack:
                    current = stack.pop()
                    for child in children_map.get(current, ()):
                        if child not in result:
                            result.add(child)
                            stack.append(child)
                return result

            facts_by_node: dict[str, list[dict[str, Any]]] = {}
            for fact in facts:
                facts_by_node.setdefault(fact["node_id"], []).append(fact)
            for node_id in sorted(nodes):
                direct = [f for f in facts_by_node.get(node_id, []) if measure in (f.get("measures") or {})]
                descendant_facts = [f for descendant in descendants(node_id)
                                    for f in facts_by_node.get(descendant, [])
                                    if measure in (f.get("measures") or {})]
                if not direct or not descendant_facts:
                    continue
                direct_sum = sum((converted(f) for f in direct), ZERO)
                descendant_sum = sum((converted(f) for f in descendant_facts), ZERO)
                if descendant_sum == 0:
                    continue
                gap = abs(direct_sum - descendant_sum) / descendant_sum
                if gap > tolerance:
                    findings.append({
                        "check_type": "rollup_consistency",
                        "severity": severity,
                        "node_id": node_id,
                        "metric_id": metric_id,
                        "message": (f"节点 {node_id} 的 {metric_id} 直报值与下级汇总偏差 "
                                    f"{to_number(_round('ratio', gap))}，超过容限 {check.get('tolerance', 0.01)}"),
                    })
    findings.sort(key=lambda item: (item["check_type"], item["node_id"] or "", item["metric_id"] or ""))
    return findings
