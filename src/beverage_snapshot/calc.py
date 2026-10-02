"""快照指标复算引擎。

所有派生指标都只能由绑定到快照版本的冻结事实重算：
- 高端占比 = 高端销量/营业额 ÷ 总量；
- 同比增长 = 本期营业额 ÷ 上年同期营业额 - 1；
- 单位成本 = 销货成本 ÷ 销量（仅在板块内有意义，啤酒与白酒销量单位不同）；
- EBITDA = 营业额 - 销货成本 - 期间费用 + 折旧摊销。

金额按指标定义版本冻结的汇率换算到报告币种；销量不做币种换算。
"""

from __future__ import annotations

import re
from typing import Any

MONEY_METRICS = ("gross_revenue", "premium_revenue", "cogs",
                 "operating_expense", "depreciation_amortization")
ROUND_DIGITS = 6

_PERIOD = re.compile(r"^(\d{4})(H[12]|Q[1-4])$")


def prior_period(period: str) -> str | None:
    """返回上年同期间标签，如 2026H1 -> 2025H1。"""

    match = _PERIOD.match(period)
    if not match:
        return None
    return f"{int(match.group(1)) - 1}{match.group(2)}"


def to_reporting_ccy(value: float, currency: str, rates: dict[str, float]) -> float:
    """按冻结汇率把金额换算为报告币种，报告币种汇率必须为 1。"""

    if currency not in rates:
        raise KeyError(currency)
    return value * rates[currency]


def _r(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, ROUND_DIGITS)


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    """安全比率，分母缺失或为零时返回 None。"""

    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def latest_batch_rows(rows: list[Any], *, period: str) -> list[Any]:
    """在同一 (来源,板块,品牌,地区,指标) 维度上取最晚批次的事实。

    晚到批次不会覆盖早批次行（审计上全部保留），快照复算只以最晚批次为准；
    选中的行随后会被快照绑定表钉住，发布之后再晚到的数据无法影响已发布版本。
    """

    chosen: dict[tuple, Any] = {}
    for row in rows:
        if row["period"] != period:
            continue
        key = (row["source"], row["segment"], row["brand"], row["region"], row["metric_id"])
        current = chosen.get(key)
        signature = (row["recorded_at"], row["batch_id"], row["fact_id"])
        if current is None or signature > (current["recorded_at"], current["batch_id"], current["fact_id"]):
            chosen[key] = row
    return list(chosen.values())


def _aggregate(rows: list[Any], rates: dict[str, float]) -> dict[tuple, dict[str, float]]:
    """按 (板块,品牌,地区) 聚合金额（换算后）与销量。"""

    groups: dict[tuple, dict[str, float]] = {}
    for row in rows:
        key = (row["segment"], row["brand"], row["region"])
        bucket = groups.setdefault(key, {})
        metric = row["metric_id"]
        amount = to_reporting_ccy(row["value"], row["currency"], rates) \
            if metric in MONEY_METRICS else row["value"]
        bucket[metric] = bucket.get(metric, 0.0) + amount
    return groups


def _derived(metrics: dict[str, float]) -> dict[str, float | None]:
    gross = metrics.get("gross_revenue")
    cogs = metrics.get("cogs")
    ebitda = None
    if gross is not None and cogs is not None:
        ebitda = gross - cogs - metrics.get("operating_expense", 0.0) \
            + metrics.get("depreciation_amortization", 0.0)
    return {
        "premium_volume_share": ratio(metrics.get("premium_volume"), metrics.get("sales_volume")),
        "premium_revenue_share": ratio(metrics.get("premium_revenue"), gross),
        "unit_cost": ratio(cogs, metrics.get("sales_volume")),
        "ebitda": ebitda,
        "ebitda_margin": ratio(ebitda, gross),
    }


def _with_yoy(current: dict[str, float], previous: dict[str, float]) -> dict[str, float | None]:
    derived = _derived(current)
    derived["yoy_growth"] = (
        round(current["gross_revenue"] / previous["gross_revenue"] - 1, ROUND_DIGITS)
        if current.get("gross_revenue") is not None and previous.get("gross_revenue")
        else None
    )
    return derived


def _output(metrics: dict[str, float], derived: dict[str, float | None]) -> dict[str, float | None]:
    return {**{k: _r(v) for k, v in metrics.items()},
            **{k: _r(v) for k, v in derived.items()}}


def compute_result(*, period: str, reporting_ccy: str, current_rows: list[Any],
                   prior_rows: list[Any] | None, rates: dict[str, float]) -> dict[str, Any]:
    """把冻结事实聚合为分组、板块与总体指标，全部可从输入复算。"""

    current_groups = _aggregate(current_rows, rates)
    prior_groups = _aggregate(prior_rows or [], rates)

    group_results: list[dict[str, Any]] = []
    segment_current: dict[str, dict[str, float]] = {}
    segment_prior: dict[str, dict[str, float]] = {}

    for key in sorted(current_groups):
        segment, brand, region = key
        metrics = current_groups[key]
        derived = _with_yoy(metrics, prior_groups.get(key, {}))
        group_results.append({
            "segment": segment, "brand": brand, "region": region,
            "metrics": _output(metrics, derived),
        })
        for metric, value in metrics.items():
            segment_current.setdefault(segment, {})[metric] = \
                segment_current.setdefault(segment, {}).get(metric, 0.0) + value
    for key, metrics in prior_groups.items():
        segment = key[0]
        for metric, value in metrics.items():
            segment_prior.setdefault(segment, {})[metric] = \
                segment_prior.setdefault(segment, {}).get(metric, 0.0) + value

    segment_results: dict[str, Any] = {}
    total_money: dict[str, float] = {}
    prior_money: dict[str, float] = {}
    for segment, metrics in sorted(segment_current.items()):
        derived = _with_yoy(metrics, segment_prior.get(segment, {}))
        segment_results[segment] = _output(metrics, derived)
        for metric, value in metrics.items():
            if metric in MONEY_METRICS:
                total_money[metric] = total_money.get(metric, 0.0) + value
    for metrics in segment_prior.values():
        for metric, value in metrics.items():
            if metric in MONEY_METRICS:
                prior_money[metric] = prior_money.get(metric, 0.0) + value

    # 跨板块销量单位不同（啤酒 kL / 白酒吨），总体不汇总销量，
    # 因此总体高端销量占比与单位成本无业务含义，显式置空而不是给出错误数字。
    total_derived = _with_yoy(total_money, prior_money)
    total_derived["unit_cost"] = None
    total_derived["premium_volume_share"] = None

    return {
        "period": period,
        "reporting_ccy": reporting_ccy,
        "totals": _output(total_money, total_derived),
        "segments": segment_results,
        "groups": group_results,
    }
