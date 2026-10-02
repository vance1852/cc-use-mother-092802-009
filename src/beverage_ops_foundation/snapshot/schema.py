"""组合经营快照服务的 SQLite 表结构。

所有长事务状态（冻结输入、勾稽异常、会签、发布、重述）都落库，
服务重启后尚未完成的审核与重述可以继续推进。
"""

from __future__ import annotations

SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS snap_definitions (
    version_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL,
    body_json TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_by TEXT,
    change_reason TEXT,
    UNIQUE(definition_id, version_no)
);
CREATE TABLE IF NOT EXISTS snap_scopes (
    version_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL,
    currency_json TEXT NOT NULL,
    body_json TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    superseded_by TEXT,
    change_reason TEXT,
    UNIQUE(scope_id, version_no)
);
CREATE TABLE IF NOT EXISTS snap_scope_nodes (
    scope_version_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    parent_id TEXT,
    node_type TEXT NOT NULL,
    name TEXT NOT NULL,
    brand_id TEXT,
    segment TEXT,
    region TEXT,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(scope_version_id, node_id)
);
CREATE TABLE IF NOT EXISTS snap_facts (
    fact_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    source TEXT NOT NULL,
    period_code TEXT NOT NULL,
    currency_code TEXT NOT NULL,
    measures_json TEXT NOT NULL,
    measures_hash TEXT NOT NULL,
    observed_at TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_facts_node_period ON snap_facts(node_id, period_code);
CREATE INDEX IF NOT EXISTS idx_snap_facts_batch ON snap_facts(batch_id);
CREATE TABLE IF NOT EXISTS snap_fact_revisions (
    revision_id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    measures_json TEXT NOT NULL,
    measures_hash TEXT NOT NULL,
    change_type TEXT NOT NULL,
    note TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_fact_revisions_fact ON snap_fact_revisions(fact_id);
CREATE TABLE IF NOT EXISTS snap_versions (
    version_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    period_code TEXT NOT NULL,
    serial INTEGER NOT NULL CHECK(serial >= 1),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    change_reason TEXT,
    restates_version_id TEXT,
    restated_by_version_id TEXT,
    input_fingerprint TEXT NOT NULL,
    frozen_inputs_json TEXT NOT NULL,
    results_json TEXT NOT NULL,
    results_hash TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    acknowledged_json TEXT NOT NULL,
    diff_json TEXT,
    published_at TEXT,
    published_by TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scope_id, definition_id, period_code, serial)
);
CREATE TABLE IF NOT EXISTS snap_signoffs (
    version_id TEXT NOT NULL,
    role TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT,
    signed_at TEXT NOT NULL,
    PRIMARY KEY(version_id, role)
);
CREATE TABLE IF NOT EXISTS snap_version_facts (
    version_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    included_revision_id TEXT,
    measures_hash TEXT NOT NULL,
    PRIMARY KEY(version_id, fact_id)
);
CREATE TABLE IF NOT EXISTS snap_sanction (
    version_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    comment TEXT,
    sanctioned_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snap_node_grants (
    actor_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(actor_id, node_id)
);
"""
