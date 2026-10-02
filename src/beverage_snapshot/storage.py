"""组合经营快照的 SQLite 表结构与连接管理。

在基础服务的 organizations / actors / sites / request_receipts / audit_events
之上增加快照领域自己的表；两库同库同事务，审计链与幂等规则直接复用。
"""

from __future__ import annotations

from beverage_ops_foundation.storage import Database

SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS definition_versions (
    definition_version_id TEXT PRIMARY KEY,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    organization_scope_json TEXT NOT NULL,
    reporting_ccy TEXT NOT NULL,
    fiscal_period TEXT NOT NULL,
    exchange_rate_json TEXT NOT NULL,
    definitions_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','frozen')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE(fiscal_period, version_no)
);
CREATE TABLE IF NOT EXISTS metric_definitions (
    definition_version_id TEXT NOT NULL REFERENCES definition_versions(definition_version_id),
    metric_id TEXT NOT NULL,
    metric_kind TEXT NOT NULL CHECK(metric_kind IN ('fact','derived')),
    display_name TEXT NOT NULL,
    unit TEXT NOT NULL,
    description TEXT NOT NULL,
    formula TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL CHECK(version >= 1),
    PRIMARY KEY(definition_version_id, metric_id)
);
CREATE TABLE IF NOT EXISTS business_facts (
    fact_id TEXT PRIMARY KEY,
    definition_version_id TEXT NOT NULL REFERENCES definition_versions(definition_version_id),
    batch_id TEXT NOT NULL,
    source TEXT NOT NULL,
    segment TEXT NOT NULL,
    brand TEXT NOT NULL,
    region TEXT NOT NULL,
    period TEXT NOT NULL,
    metric_id TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    currency TEXT NOT NULL,
    is_late INTEGER NOT NULL CHECK(is_late IN (0,1)),
    payload_hash TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(definition_version_id, batch_id, source, segment, brand, region, period, metric_id)
);
CREATE TABLE IF NOT EXISTS actor_data_scopes (
    actor_id TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('brand','segment','region')),
    scope_value TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(actor_id, scope_type, scope_value)
);
CREATE TABLE IF NOT EXISTS snapshot_versions (
    snapshot_version_id TEXT PRIMARY KEY,
    definition_version_id TEXT NOT NULL REFERENCES definition_versions(definition_version_id),
    fiscal_period TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    state TEXT NOT NULL CHECK(state IN ('draft','reconciled','submitted','published','superseded')),
    parent_version_id TEXT REFERENCES snapshot_versions(snapshot_version_id),
    restatement_reason TEXT,
    change_summary TEXT,
    impact_json TEXT,
    input_fingerprint TEXT NOT NULL DEFAULT '',
    results_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    published_at TEXT,
    published_by TEXT,
    UNIQUE(definition_version_id, version_no)
);
CREATE TABLE IF NOT EXISTS snapshot_fact_bindings (
    snapshot_version_id TEXT NOT NULL REFERENCES snapshot_versions(snapshot_version_id),
    fact_id TEXT NOT NULL REFERENCES business_facts(fact_id),
    bound_at TEXT NOT NULL,
    PRIMARY KEY(snapshot_version_id, fact_id)
);
CREATE TABLE IF NOT EXISTS reconciliation_issues (
    issue_id TEXT PRIMARY KEY,
    snapshot_version_id TEXT NOT NULL REFERENCES snapshot_versions(snapshot_version_id),
    code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('warning','blocking')),
    message TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    explanation TEXT,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshot_approvals (
    snapshot_version_id TEXT NOT NULL REFERENCES snapshot_versions(snapshot_version_id),
    role TEXT NOT NULL,
    approver_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    comment TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY(snapshot_version_id, role)
);
"""


class SnapshotDatabase(Database):
    """打开基础库并补齐快照领域表。"""

    def __init__(self, path: str = ":memory:") -> None:
        super().__init__(path)
        self.connection.executescript(SNAPSHOT_SCHEMA)
