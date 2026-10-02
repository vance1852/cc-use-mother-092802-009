"""组合经营快照领域的枚举与口径常量。

啤酒高端化与白酒调整在销量单位、价位带、营业额与 EBITDA 口径上都不同，
这里集中声明系统允许的板块、事实指标、价位带以及指标的业务口径，
快照版本只引用已经在指标目录中冻结的定义。
"""

from __future__ import annotations

# 业务板块：啤酒与白酒分开核算，口径不互通。
SEGMENT_BEER = "beer"
SEGMENT_BAIJIU = "baijiu"
SEGMENTS = frozenset({SEGMENT_BEER, SEGMENT_BAIJIU})

# 高端口径由冻结指标定义中的 premium_volume / premium_revenue 表达，
# 两类业务各自的价位带阈值说明保存在指标定义版本的 description 里。

# 原始经营事实指标（来自渠道/财务上报，带来源与批次号）。
FACT_METRICS = frozenset({
    "sales_volume",        # 销量，啤酒千升(kL)，白酒吨(tonne)，单位由板块口径决定
    "gross_revenue",       # 营业额（含税前）， reporting_ccy 决定币种
    "premium_volume",      # 高端（含次高端）销量
    "premium_revenue",     # 高端营业额
    "cogs",                # 销货成本
    "operating_expense",   # 期间费用
    "depreciation_amortization",  # 折旧与摊销
})

# 派生指标：发布前必须能从冻结的事实输入复算。
DERIVED_METRICS = frozenset({
    "premium_volume_share",   # 高端产品占比 = 高端销量 / 总销量
    "premium_revenue_share",  # 高端营业额占比
    "unit_cost",              # 单位成本 = 销货成本 / 销量
    "ebitda",                 # EBITDA = 营业额 - 销货成本 - 期间费用 + 折旧摊销
    "ebitda_margin",          # EBITDA 率 = EBITDA / 营业额
    "yoy_growth",             # 同比增长 = 本期营业额 / 上年同期营业额 - 1
})

# 快照状态机。
STATE_DRAFT = "draft"
STATE_RECONCILED = "reconciled"
STATE_SUBMITTED = "submitted"
STATE_PUBLISHED = "published"
STATE_SUPERSEDED = "superseded"
SNAPSHOT_STATES = frozenset(
    {STATE_DRAFT, STATE_RECONCILED, STATE_SUBMITTED, STATE_PUBLISHED, STATE_SUPERSEDED}
)

# 异常说明状态。
EXCEPTION_OPEN = "open"
EXCEPTION_RESOLVED = "resolved"
EXCEPTION_STATES = frozenset({EXCEPTION_OPEN, EXCEPTION_RESOLVED})

# 重述触发原因。
RESTATEMENT_LATE_DATA = "late_channel_data"
RESTATEMENT_DEFINITION = "definition_change"
RESTATEMENT_SCOPE = "scope_change"
RESTATEMENT_REASONS = frozenset(
    {RESTATEMENT_LATE_DATA, RESTATEMENT_DEFINITION, RESTATEMENT_SCOPE}
)


def is_segment(value: str) -> bool:
    return value in SEGMENTS


def is_fact_metric(value: str) -> bool:
    return value in FACT_METRICS
