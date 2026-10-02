"""组合经营快照服务。

在基础包的 SQLite、操作者角色、幂等回执与哈希审计链之上，提供：

- 指标定义、组织范围、币种汇率、期间的版本化登记；
- 带 source 与 batch_id 的经营事实接收（修订留痕，不就地抹除）；
- 发布前从冻结输入复算高端产品占比、同比、单位成本与盈利指标；
- 勾稽异常、异常说明（warning 逐条说明，blocking 必须消除）；
- 按定义要求的角色会签与管理员授权发布；
- 迟到数据只能生成新的重述版本，已发布快照永不变更；
- 版本间变化原因与影响范围（差异报告）；
- 按品牌、业务板块、地区下钻，并对无权限节点整段隐藏；
- 全部状态落库，服务重启后审核与重述继续。
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from typing import Any

from ..audit import append_event, canonical_json, digest
from ..clock import Clock, SystemClock
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..models import Actor
from .metrics import KNOWN_MEASURES, MetricDefinitionError, evaluate, run_checks, validate_definition
from .schema import SNAPSHOT_SCHEMA

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
PERIOD = re.compile(r"^(\d{4})(H[12]|Q[1-4]|-(0[1-9]|1[0-2]))?$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
NODE_TYPES = frozenset({"group", "segment", "brand", "region", "operating_unit"})
DRAFT_STATUSES = frozenset({"draft"})

# 单个冻结组织范围允许的节点规模上限。
MAX_NODES = 2000


def previous_period(period_code: str) -> str | None:
    """返回同比期间代码：年度、半年、季度与月度均取上年同期。"""

    match = PERIOD.fullmatch(period_code)
    if not match:
        return None
    year = int(match.group(1))
    suffix = match.group(2)
    return f"{year - 1}{suffix or ''}"


class SnapshotService:
    """实现快照版本从建档到发布、重述的完整状态机。"""

    def __init__(self, database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        database.connection.executescript(SNAPSHOT_SCHEMA)

    # ----- 基础工具 -----

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _period(self, value: str) -> str:
        value = self._identifier(value, "period_code")
        if not PERIOD.fullmatch(value):
            raise ValidationError("period_code 应形如 2026、2026H1、2026Q2 或 2026-06")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create) -> dict[str, Any]:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"replayed": True, "resource_type": row["resource_type"], "resource_id": row["resource_id"]}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"replayed": False, "resource_type": resource_type, "resource_id": resource_id, **response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    # ----- 指标定义版本 -----

    def register_metric_definition(self, *, request_id: str, actor_id: str, definition_id: str,
                                   body: dict[str, Any], change_reason: str | None = None) -> dict[str, Any]:
        definition_id = self._identifier(definition_id, "definition_id")
        payload = {"actor_id": actor_id, "definition_id": definition_id,
                   "body": body, "change_reason": change_reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            try:
                validate_definition(body)
            except MetricDefinitionError as exc:
                raise ValidationError(str(exc)) from exc
            body_hash = digest(body)

            def create():
                previous = connection.execute(
                    "SELECT * FROM snap_definitions WHERE definition_id=? AND status='active' "
                    "ORDER BY version_no DESC LIMIT 1", (definition_id,)).fetchone()
                version_no = (previous["version_no"] + 1) if previous else 1
                if previous and previous["body_hash"] == body_hash:
                    raise ConflictError("指标定义内容与当前生效版本完全相同，无需生成新版本")
                if version_no > 1:
                    self._text(change_reason, "change_reason", 500)
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO snap_definitions(version_id,definition_id,version_no,status,body_json,"
                    "body_hash,created_by,created_at,change_reason) VALUES(?,?,?, 'active',?,?,?,?,?)",
                    (version_id, definition_id, version_no, canonical_json(body), body_hash,
                     actor_id, self._now(), change_reason),
                )
                if previous:
                    connection.execute(
                        "UPDATE snap_definitions SET status='superseded', superseded_by=? "
                        "WHERE version_id=?", (version_id, previous["version_id"]),
                    )
                self._audit(connection, actor_id=actor_id, action="snapshot.definition.registered",
                            resource_type="metric_definition", resource_id=version_id,
                            detail={"definition_id": definition_id, "version_no": version_no,
                                    "body_hash": body_hash, "change_reason": change_reason})
                return ("metric_definition", version_id,
                        {"version_id": version_id, "definition_id": definition_id, "version_no": version_no})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.register_definition:{definition_id}",
                                    payload=payload, create=create)

    def get_definition(self, definition_id: str, version_no: int | None = None,
                       actor_id: str | None = None) -> dict[str, Any]:
        connection = self.database.connection
        if actor_id is not None:
            self._actor(connection, actor_id)
        if version_no is None:
            row = connection.execute(
                "SELECT * FROM snap_definitions WHERE definition_id=? AND status='active' "
                "ORDER BY version_no DESC LIMIT 1", (definition_id,)).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM snap_definitions WHERE definition_id=? AND version_no=?",
                (definition_id, version_no)).fetchone()
        if row is None:
            raise NotFoundError("指标定义版本不存在")
        return {"version_id": row["version_id"], "definition_id": row["definition_id"],
                "version_no": row["version_no"], "status": row["status"],
                "body": json.loads(row["body_json"]), "body_hash": row["body_hash"],
                "change_reason": row["change_reason"], "created_by": row["created_by"],
                "created_at": row["created_at"]}

    # ----- 组织范围与币种版本 -----

    def register_scope(self, *, request_id: str, actor_id: str, scope_id: str,
                       nodes: list[dict[str, Any]], currencies: dict[str, Any],
                       change_reason: str | None = None) -> dict[str, Any]:
        scope_id = self._identifier(scope_id, "scope_id")
        payload = {"actor_id": actor_id, "scope_id": scope_id, "nodes": nodes,
                   "currencies": currencies, "change_reason": change_reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            clean_nodes = self._clean_nodes(nodes)
            clean_currencies = self._clean_currencies(currencies)
            body = {"nodes": [
                {k: node[k] for k in ("node_id", "parent_id", "node_type", "name",
                                      "brand_id", "segment", "region") if node.get(k) is not None}
                for node in clean_nodes], "currencies": clean_currencies}
            body_hash = digest(body)

            def create():
                previous = connection.execute(
                    "SELECT * FROM snap_scopes WHERE scope_id=? AND status='active' "
                    "ORDER BY version_no DESC LIMIT 1", (scope_id,)).fetchone()
                version_no = (previous["version_no"] + 1) if previous else 1
                if previous and previous["body_hash"] == body_hash:
                    raise ConflictError("组织范围内容与当前生效版本完全相同，无需生成新版本")
                if version_no > 1:
                    self._text(change_reason, "change_reason", 500)
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO snap_scopes(version_id,scope_id,version_no,status,currency_json,"
                    "body_json,body_hash,created_by,created_at,change_reason) "
                    "VALUES(?,?,?, 'active',?,?,?,?,?,?)",
                    (version_id, scope_id, version_no, canonical_json(clean_currencies),
                     canonical_json(body), body_hash, actor_id, self._now(), change_reason),
                )
                for ordinal, node in enumerate(clean_nodes):
                    connection.execute(
                        "INSERT INTO snap_scope_nodes(scope_version_id,node_id,parent_id,node_type,name,"
                        "brand_id,segment,region,ordinal) VALUES(?,?,?,?,?,?,?,?,?)",
                        (version_id, node["node_id"], node.get("parent_id"), node["node_type"],
                         node["name"], node.get("brand_id"), node.get("segment"),
                         node.get("region"), ordinal),
                    )
                if previous:
                    connection.execute(
                        "UPDATE snap_scopes SET status='superseded', superseded_by=? "
                        "WHERE version_id=?", (version_id, previous["version_id"]),
                    )
                self._audit(connection, actor_id=actor_id, action="snapshot.scope.registered",
                            resource_type="scope", resource_id=version_id,
                            detail={"scope_id": scope_id, "version_no": version_no,
                                    "node_count": len(clean_nodes), "change_reason": change_reason})
                return ("scope", version_id, {"version_id": version_id, "scope_id": scope_id,
                                              "version_no": version_no, "node_count": len(clean_nodes)})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.register_scope:{scope_id}",
                                    payload=payload, create=create)

    def _clean_nodes(self, nodes: Any) -> list[dict[str, Any]]:
        if not isinstance(nodes, list) or not nodes:
            raise ValidationError("nodes 必须是非空数组")
        if len(nodes) > MAX_NODES:
            raise ValidationError(f"节点数量不能超过 {MAX_NODES}")
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in nodes:
            if not isinstance(raw, dict):
                raise ValidationError("节点必须是对象")
            node_id = self._identifier(raw.get("node_id"), "node_id")
            if node_id in seen:
                raise ValidationError(f"节点编号重复: {node_id}")
            seen.add(node_id)
            node_type = raw.get("node_type")
            if node_type not in NODE_TYPES:
                raise ValidationError(f"{node_id} 的 node_type 不受支持")
            parent_id = raw.get("parent_id")
            if parent_id is not None:
                parent_id = self._identifier(parent_id, "parent_id")
                if parent_id == node_id:
                    raise ValidationError(f"{node_id} 不能把自己设为上级")
            cleaned.append({
                "node_id": node_id,
                "parent_id": parent_id,
                "node_type": node_type,
                "name": self._text(raw.get("name"), "name", 100),
                "brand_id": self._optional_identifier(raw.get("brand_id"), "brand_id"),
                "segment": self._optional_text(raw.get("segment"), "segment"),
                "region": self._optional_text(raw.get("region"), "region"),
            })
        ids = {node["node_id"] for node in cleaned}
        by_id = {node["node_id"]: node for node in cleaned}
        for node in cleaned:
            if node["parent_id"] is not None and node["parent_id"] not in ids:
                raise ValidationError(f"节点 {node['node_id']} 的上级 {node['parent_id']} 不存在")
        for node in cleaned:  # 无环检查
            seen_chain: set[str] = set()
            current = node["node_id"]
            while current is not None:
                if current in seen_chain:
                    raise ValidationError("组织范围树不能存在环")
                seen_chain.add(current)
                parent = by_id[current].get("parent_id")
                current = parent if parent in ids else None
        return cleaned

    def _optional_identifier(self, value: Any, field: str) -> str | None:
        if value in (None, ""):
            return None
        return self._identifier(value, field)

    def _optional_text(self, value: Any, field: str) -> str | None:
        if value in (None, ""):
            return None
        return self._text(value, field, 100)

    def _clean_currencies(self, currencies: Any) -> dict[str, Any]:
        if not isinstance(currencies, dict) or not isinstance(currencies.get("rates"), dict):
            raise ValidationError("currencies.rates 必须是币种到汇率的对象")
        rates: dict[str, str] = {}
        for code, rate in currencies["rates"].items():
            if not CURRENCY.fullmatch(str(code)):
                raise ValidationError(f"币种代码无效: {code}")
            try:
                value = Decimal(str(rate))
            except Exception as exc:
                raise ValidationError(f"币种 {code} 的汇率无效") from exc
            if value <= 0:
                raise ValidationError(f"币种 {code} 的汇率必须为正数")
            rates[code] = str(value)
        as_of = currencies.get("as_of_date")
        if as_of is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(as_of)):
            raise ValidationError("as_of_date 应形如 2026-06-30")
        return {"rates": rates, "as_of_date": as_of}

    def get_scope(self, scope_id: str, version_no: int | None = None,
                  actor_id: str | None = None) -> dict[str, Any]:
        connection = self.database.connection
        if actor_id is not None:
            self._actor(connection, actor_id)
        if version_no is None:
            row = connection.execute(
                "SELECT * FROM snap_scopes WHERE scope_id=? AND status='active' "
                "ORDER BY version_no DESC LIMIT 1", (scope_id,)).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM snap_scopes WHERE scope_id=? AND version_no=?",
                (scope_id, version_no)).fetchone()
        if row is None:
            raise NotFoundError("组织范围版本不存在")
        return {"version_id": row["version_id"], "scope_id": row["scope_id"],
                "version_no": row["version_no"], "status": row["status"],
                "body": json.loads(row["body_json"]), "body_hash": row["body_hash"],
                "currencies": json.loads(row["currency_json"]),
                "change_reason": row["change_reason"], "created_by": row["created_by"],
                "created_at": row["created_at"]}

    # ----- 节点授权 -----

    def grant_node_access(self, *, request_id: str, actor_id: str, target_actor_id: str,
                          node_ids: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "target_actor_id": target_actor_id, "node_ids": list(node_ids or [])}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            target = self._actor(connection, target_actor_id)
            if not isinstance(node_ids, list) or not node_ids:
                raise ValidationError("node_ids 必须是非空数组")
            clean_ids = [self._identifier(node_id, "node_id") for node_id in node_ids]

            def create():
                granted = []
                for node_id in clean_ids:
                    exists = connection.execute(
                        "SELECT 1 FROM snap_scope_nodes WHERE node_id=? LIMIT 1", (node_id,)).fetchone()
                    if exists is None:
                        raise NotFoundError(f"节点 {node_id} 未在任何组织范围版本中登记")
                    connection.execute(
                        "INSERT INTO snap_node_grants(actor_id,node_id,granted_by,granted_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(actor_id,node_id) DO UPDATE SET "
                        "granted_by=excluded.granted_by, granted_at=excluded.granted_at",
                        (target.actor_id, node_id, actor.actor_id, self._now()),
                    )
                    granted.append(node_id)
                self._audit(connection, actor_id=actor_id, action="snapshot.access.granted",
                            resource_type="actor", resource_id=target.actor_id,
                            detail={"node_ids": granted})
                return ("node_grant", f"{target.actor_id}:{','.join(granted)}",
                        {"target_actor_id": target.actor_id, "node_ids": granted})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.grant:{target_actor_id}:{digest(payload)}",
                                    payload=payload, create=create)

    # ----- 经营事实 -----

    def record_fact(self, *, request_id: str, actor_id: str, fact_id: str, node_id: str,
                    batch_id: str, source: str, period_code: str, currency_code: str,
                    measures: dict[str, float], observed_at: str | None = None,
                    note: str | None = None) -> dict[str, Any]:
        fact_id = self._identifier(fact_id, "fact_id")
        node_id = self._identifier(node_id, "node_id")
        batch_id = self._identifier(batch_id, "batch_id")
        period_code = self._period(period_code)
        payload = {"actor_id": actor_id, "fact_id": fact_id, "node_id": node_id,
                   "batch_id": batch_id, "source": source, "period_code": period_code,
                   "currency_code": currency_code, "measures": measures, "observed_at": observed_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            source = self._text(source, "source", 100)
            if not CURRENCY.fullmatch(str(currency_code or "")):
                raise ValidationError("currency_code 必须是三位大写币种代码")
            clean_measures = self._clean_measures(measures)
            if observed_at is not None:
                observed_at = self._text(observed_at, "observed_at", 40)
            if connection.execute("SELECT 1 FROM snap_scope_nodes WHERE node_id=? LIMIT 1",
                                  (node_id,)).fetchone() is None:
                raise NotFoundError(f"节点 {node_id} 未在任何组织范围版本中登记")

            def create():
                measures_json = canonical_json(clean_measures)
                measures_hash = digest(clean_measures)
                existing = connection.execute(
                    "SELECT * FROM snap_facts WHERE fact_id=?", (fact_id,)).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO snap_facts(fact_id,node_id,batch_id,source,period_code,"
                        "currency_code,measures_json,measures_hash,observed_at,recorded_by,recorded_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (fact_id, node_id, batch_id, source, period_code, currency_code,
                         measures_json, measures_hash, observed_at, actor_id, self._now()),
                    )
                    self._audit(connection, actor_id=actor_id, action="snapshot.fact.recorded",
                                resource_type="fact", resource_id=fact_id,
                                detail={"node_id": node_id, "batch_id": batch_id,
                                        "period_code": period_code, "source": source,
                                        "measures_hash": measures_hash})
                    return ("fact", fact_id, {"fact_id": fact_id, "revised": False})
                if (existing["measures_hash"] == measures_hash
                        and existing["node_id"] == node_id
                        and existing["period_code"] == period_code):
                    return ("fact", fact_id, {"fact_id": fact_id, "revised": False, "duplicate": True})
                # 同一业务事实的迟到修正：写入修订记录，绝不静默覆盖。
                revision_id = uuid.uuid4().hex
                connection.execute(
                    "UPDATE snap_facts SET node_id=?,batch_id=?,source=?,period_code=?,currency_code=?,"
                    "measures_json=?,measures_hash=?,observed_at=?,recorded_by=?,recorded_at=? "
                    "WHERE fact_id=?",
                    (node_id, batch_id, source, period_code, currency_code, measures_json,
                     measures_hash, observed_at, actor_id, self._now(), fact_id),
                )
                connection.execute(
                    "INSERT INTO snap_fact_revisions(revision_id,fact_id,batch_id,measures_json,"
                    "measures_hash,change_type,note,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (revision_id, fact_id, batch_id, measures_json, measures_hash, "revision",
                     note, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="snapshot.fact.revised",
                            resource_type="fact", resource_id=fact_id,
                            detail={"revision_id": revision_id, "batch_id": batch_id,
                                    "previous_hash": existing["measures_hash"],
                                    "measures_hash": measures_hash, "note": note})
                return ("fact", fact_id, {"fact_id": fact_id, "revised": True,
                                          "revision_id": revision_id})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.record_fact:{fact_id}",
                                    payload=payload, create=create)

    def _clean_measures(self, measures: Any) -> dict[str, float]:
        if not isinstance(measures, dict) or not measures:
            raise ValidationError("measures 必须是非空对象")
        cleaned: dict[str, float] = {}
        for name, raw in measures.items():
            if name not in KNOWN_MEASURES:
                raise ValidationError(f"未知度量: {name}")
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise ValidationError(f"度量 {name} 必须是数字")
            value = Decimal(str(raw))
            if not value.is_finite():
                raise ValidationError(f"度量 {name} 必须是有限数字")
            cleaned[name] = float(value)
        return cleaned

    # ----- 快照版本：冻结、勾稽、会签、发布 -----

    def build_version(self, *, request_id: str, actor_id: str, scope_id: str, definition_id: str,
                      period_code: str, kind: str = "regular", restates_version_id: str | None = None,
                      change_reason: str | None = None,
                      comparison_version_id: str | None = None) -> dict[str, Any]:
        scope_id = self._identifier(scope_id, "scope_id")
        definition_id = self._identifier(definition_id, "definition_id")
        period_code = self._period(period_code)
        if kind not in ("regular", "restatement"):
            raise ValidationError("kind 只能是 regular 或 restatement")
        payload = {"actor_id": actor_id, "scope_id": scope_id, "definition_id": definition_id,
                   "period_code": period_code, "kind": kind, "restates_version_id": restates_version_id,
                   "comparison_version_id": comparison_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            scope_row = connection.execute(
                "SELECT * FROM snap_scopes WHERE scope_id=? AND status='active' ORDER BY version_no DESC LIMIT 1",
                (scope_id,)).fetchone()
            if scope_row is None:
                raise NotFoundError("生效组织范围不存在")
            definition_row = connection.execute(
                "SELECT * FROM snap_definitions WHERE definition_id=? AND status='active' "
                "ORDER BY version_no DESC LIMIT 1", (definition_id,)).fetchone()
            if definition_row is None:
                raise NotFoundError("生效指标定义不存在")
            scope = {"row": scope_row, "body": json.loads(scope_row["body_json"]),
                     "currencies": json.loads(scope_row["currency_json"])}
            definition = {"row": definition_row, "body": json.loads(definition_row["body_json"])}

            restates_row = None
            if kind == "restatement":
                if not restates_version_id:
                    raise ValidationError("重述版本必须指定 restates_version_id")
                self._text(change_reason, "change_reason", 500)
                restates_row = self._version_row(connection, restates_version_id)
                if restates_row["status"] not in ("published", "superseded"):
                    raise ConflictError("被重述版本必须已经发布")
            elif change_reason is not None:
                self._text(change_reason, "change_reason", 500)

            def create():
                open_row = connection.execute(
                    "SELECT version_id FROM snap_versions WHERE scope_id=? AND definition_id=? "
                    "AND period_code=? AND status IN ('draft','pending')",
                    (scope_id, definition_id, period_code)).fetchone()
                if open_row is not None:
                    raise ConflictError("该口径与期间已有未完成版本，请先推进或撤销它")
                serial = connection.execute(
                    "SELECT COALESCE(MAX(serial),0)+1 AS next_serial FROM snap_versions "
                    "WHERE scope_id=? AND definition_id=? AND period_code=?",
                    (scope_id, definition_id, period_code)).fetchone()["next_serial"]
                version_id = uuid.uuid4().hex
                frozen = self._freeze_inputs(connection, scope=scope, definition=definition,
                                             period_code=period_code,
                                             comparison_version_id=comparison_version_id)
                results, findings = self._compute(definition=definition, scope=scope, frozen=frozen)
                diff = None
                if kind == "restatement":
                    diff = self._diff_versions(json.loads(restates_row["results_json"]), results,
                                               old_frozen=json.loads(restates_row["frozen_inputs_json"]),
                                               new_frozen=frozen)
                connection.execute(
                    "INSERT INTO snap_versions(version_id,scope_id,definition_id,period_code,serial,kind,"
                    "status,change_reason,restates_version_id,input_fingerprint,frozen_inputs_json,"
                    "results_json,results_hash,findings_json,acknowledged_json,diff_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (version_id, scope_id, definition_id, period_code, serial, kind, "draft",
                     change_reason, restates_version_id if kind == "restatement" else None,
                     frozen["fingerprint"], canonical_json(frozen), canonical_json(results),
                     digest(results), canonical_json(findings), canonical_json({}),
                     canonical_json(diff) if diff else None, actor_id, self._now()),
                )
                for fact in frozen["facts"]:
                    connection.execute(
                        "INSERT INTO snap_version_facts(version_id,fact_id,included_revision_id,measures_hash) "
                        "VALUES(?,?,?,?)",
                        (version_id, fact["fact_id"], fact.get("revision_id"), fact["measures_hash"]),
                    )
                self._audit(connection, actor_id=actor_id, action="snapshot.version.built",
                            resource_type="snapshot_version", resource_id=version_id,
                            detail={"scope_id": scope_id, "definition_id": definition_id,
                                    "period_code": period_code, "serial": serial, "kind": kind,
                                    "fact_count": len(frozen["facts"]),
                                    "batch_count": len(frozen["batches"]),
                                    "blocking_findings": sum(1 for f in findings if f["severity"] == "blocking"),
                                    "restates_version_id": restates_version_id})
                return ("snapshot_version", version_id,
                        {"version_id": version_id, "serial": serial, "status": "draft",
                         "fact_count": len(frozen["facts"]),
                         "blocking_findings": sum(1 for f in findings if f["severity"] == "blocking"),
                         "warning_findings": sum(1 for f in findings if f["severity"] == "warning")})

            return self._idempotent(connection, request_id=request_id,
                                    action=(f"snapshot.build_version:{scope_id}:{definition_id}:"
                                            f"{period_code}:{kind}:{restates_version_id or '-'}"),
                                    payload=payload, create=create)

    def _freeze_inputs(self, connection, *, scope: dict[str, Any], definition: dict[str, Any],
                       period_code: str, comparison_version_id: str | None) -> dict[str, Any]:
        node_ids = [node["node_id"] for node in scope["body"]["nodes"]]
        facts: list[dict[str, Any]] = []
        batches: set[str] = set()
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            rows = connection.execute(
                f"SELECT * FROM snap_facts WHERE period_code=? AND node_id IN ({placeholders})",
                (period_code, *node_ids)).fetchall()
            for row in sorted(rows, key=lambda item: item["fact_id"]):
                revision = connection.execute(
                    "SELECT revision_id FROM snap_fact_revisions WHERE fact_id=? "
                    "ORDER BY recorded_at DESC, rowid DESC LIMIT 1", (row["fact_id"],)).fetchone()
                batches.add(row["batch_id"])
                facts.append({
                    "fact_id": row["fact_id"],
                    "node_id": row["node_id"],
                    "batch_id": row["batch_id"],
                    "source": row["source"],
                    "currency_code": row["currency_code"],
                    "measures": json.loads(row["measures_json"]),
                    "measures_hash": row["measures_hash"],
                    "revision_id": revision["revision_id"] if revision else None,
                    "observed_at": row["observed_at"],
                })
        # 同比基准：显式指定优先，否则取同口径上年（上月）同期的最近已发布版本。
        comparison_ref: dict[str, Any] | None = None
        candidate_id = comparison_version_id
        if candidate_id is None:
            prior_period = previous_period(period_code)
            if prior_period:
                prior = connection.execute(
                    "SELECT * FROM snap_versions WHERE scope_id=? AND definition_id=? AND period_code=? "
                    "AND status IN ('published','superseded') ORDER BY serial DESC LIMIT 1",
                    (scope["row"]["scope_id"], definition["row"]["definition_id"], prior_period)).fetchone()
                candidate_id = prior["version_id"] if prior else None
        if candidate_id:
            candidate = self._version_row(connection, candidate_id)
            if candidate["status"] not in ("published", "superseded"):
                raise ConflictError("对比版本必须已经发布")
            prior_results = json.loads(candidate["results_json"])
            comparison_ref = {
                "version_id": candidate["version_id"],
                "period_code": candidate["period_code"],
                "serial": candidate["serial"],
                "results_hash": candidate["results_hash"],
                "nodes": {node_id: {"metrics": payload["metrics"], "measures": payload["measures"]}
                          for node_id, payload in prior_results["nodes"].items()},
            }
        frozen = {
            "scope": {"version_id": scope["row"]["version_id"], "scope_id": scope["row"]["scope_id"],
                      "version_no": scope["row"]["version_no"], "body_hash": scope["row"]["body_hash"],
                      "nodes": scope["body"]["nodes"], "currencies": scope["currencies"]},
            "definition": {"version_id": definition["row"]["version_id"],
                           "definition_id": definition["row"]["definition_id"],
                           "version_no": definition["row"]["version_no"],
                           "body_hash": definition["row"]["body_hash"], "body": definition["body"]},
            "period_code": period_code,
            "facts": facts,
            "batches": sorted(batches),
            "comparison": comparison_ref,
            "frozen_at": self._now(),
        }
        frozen["fingerprint"] = digest({
            "scope_version": {"id": scope["row"]["scope_id"], "no": scope["row"]["version_no"],
                              "hash": scope["row"]["body_hash"]},
            "definition_version": {"id": definition["row"]["definition_id"],
                                   "no": definition["row"]["version_no"],
                                   "hash": definition["row"]["body_hash"]},
            "period_code": period_code,
            "facts": [{"fact_id": f["fact_id"], "revision_id": f["revision_id"],
                       "measures_hash": f["measures_hash"]} for f in facts],
            "comparison_version_id": comparison_ref["version_id"] if comparison_ref else None,
        })
        return frozen

    def _compute(self, *, definition: dict[str, Any], scope: dict[str, Any],
                 frozen: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        nodes = {node["node_id"]: node for node in frozen["scope"]["nodes"]}
        fx = {code: Decimal(rate) for code, rate in frozen["scope"]["currencies"]["rates"].items()}
        results = evaluate(definition=definition["body"], nodes=nodes, fx=fx,
                           facts=frozen["facts"], comparison=frozen.get("comparison"))
        findings = run_checks(definition=definition["body"], nodes=nodes, results=results,
                              facts=frozen["facts"], fx=fx)
        for index, finding in enumerate(findings):
            finding["finding_key"] = (
                f"{index:03d}:{finding['check_type']}:{finding['node_id'] or '-'}:"
                f"{finding['metric_id'] or '-'}")
        return results, findings

    def rebuild_version(self, *, request_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        """用最新到达的事实重算草稿；已发布版本永不重建。"""

        version_id = self._identifier(version_id, "version_id")
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create():
                row = self._version_row(connection, version_id)
                if row["status"] not in DRAFT_STATUSES:
                    raise ConflictError("只有草稿版本可以重新计算")
                frozen = json.loads(row["frozen_inputs_json"])
                scope_row = connection.execute(
                    "SELECT * FROM snap_scopes WHERE version_id=?",
                    (frozen["scope"]["version_id"],)).fetchone()
                definition_row = connection.execute(
                    "SELECT * FROM snap_definitions WHERE version_id=?",
                    (frozen["definition"]["version_id"],)).fetchone()
                scope = {"row": scope_row, "body": json.loads(scope_row["body_json"]),
                         "currencies": json.loads(scope_row["currency_json"])}
                definition = {"row": definition_row, "body": json.loads(definition_row["body_json"])}
                new_frozen = self._freeze_inputs(
                    connection, scope=scope, definition=definition, period_code=row["period_code"],
                    comparison_version_id=(frozen.get("comparison") or {}).get("version_id"))
                results, findings = self._compute(definition=definition, scope=scope, frozen=new_frozen)
                connection.execute("DELETE FROM snap_version_facts WHERE version_id=?", (version_id,))
                for fact in new_frozen["facts"]:
                    connection.execute(
                        "INSERT INTO snap_version_facts(version_id,fact_id,included_revision_id,measures_hash) "
                        "VALUES(?,?,?,?)",
                        (version_id, fact["fact_id"], fact.get("revision_id"), fact["measures_hash"]))
                connection.execute(
                    "UPDATE snap_versions SET frozen_inputs_json=?,input_fingerprint=?,results_json=?,"
                    "results_hash=?,findings_json=?,acknowledged_json=? WHERE version_id=?",
                    (canonical_json(new_frozen), new_frozen["fingerprint"], canonical_json(results),
                     digest(results), canonical_json(findings), canonical_json({}), version_id),
                )
                self._audit(connection, actor_id=actor_id, action="snapshot.version.rebuilt",
                            resource_type="snapshot_version", resource_id=version_id,
                            detail={"fact_count": len(new_frozen["facts"]),
                                    "batch_count": len(new_frozen["batches"]),
                                    "new_fingerprint": new_frozen["fingerprint"]})
                return ("snapshot_version", version_id,
                        {"version_id": version_id, "status": "draft",
                         "fact_count": len(new_frozen["facts"]),
                         "blocking_findings": sum(1 for f in findings if f["severity"] == "blocking"),
                         "warning_findings": sum(1 for f in findings if f["severity"] == "warning")})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.rebuild_version:{version_id}",
                                    payload=payload, create=create)

    def _version_row(self, connection, version_id: str):
        row = connection.execute("SELECT * FROM snap_versions WHERE version_id=?",
                                 (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("快照版本不存在")
        return row

    def list_findings(self, actor_id: str, version_id: str) -> dict[str, Any]:
        version_id = self._identifier(version_id, "version_id")
        connection = self.database.connection
        self._actor(connection, actor_id)
        row = self._version_row(connection, version_id)
        findings = json.loads(row["findings_json"])
        acknowledged = json.loads(row["acknowledged_json"])
        return {"version_id": version_id, "status": row["status"],
                "items": [{**finding, "acknowledgment": acknowledged.get(finding["finding_key"])}
                          for finding in findings]}

    def acknowledge_findings(self, *, request_id: str, actor_id: str, version_id: str,
                             finding_keys: list[str] | str, note: str) -> dict[str, Any]:
        version_id = self._identifier(version_id, "version_id")
        note = self._text(note, "note", 500)
        if isinstance(finding_keys, str):
            if finding_keys != "*":
                raise ValidationError("finding_keys 必须是数组或 *")
        elif not isinstance(finding_keys, list) or not finding_keys:
            raise ValidationError("finding_keys 必须是非空数组或 *")
        payload = {"actor_id": actor_id, "version_id": version_id,
                   "finding_keys": finding_keys, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create():
                row = self._version_row(connection, version_id)
                if row["status"] not in DRAFT_STATUSES:
                    raise ConflictError("只有草稿版本可以补充异常说明")
                findings = json.loads(row["findings_json"])
                by_key = {item["finding_key"]: item for item in findings}
                wanted = list(by_key) if finding_keys == "*" else finding_keys
                acknowledged = json.loads(row["acknowledged_json"])
                applied: list[str] = []
                for key in wanted:
                    finding = by_key.get(key)
                    if finding is None:
                        raise NotFoundError(f"异常编号不存在: {key}")
                    if finding["severity"] == "blocking":
                        raise ConflictError(f"阻断性异常必须通过修订数据消除，不能仅作说明: {key}")
                    acknowledged[key] = {"by": actor_id, "at": self._now(), "note": note}
                    applied.append(key)
                connection.execute(
                    "UPDATE snap_versions SET acknowledged_json=? WHERE version_id=?",
                    (canonical_json(acknowledged), version_id))
                self._audit(connection, actor_id=actor_id, action="snapshot.finding.acknowledged",
                            resource_type="snapshot_version", resource_id=version_id,
                            detail={"finding_keys": applied})
                return ("snapshot_version", version_id,
                        {"version_id": version_id, "acknowledged": applied})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.acknowledge:{version_id}:{digest(payload)}",
                                    payload=payload, create=create)

    def submit_version(self, *, request_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        version_id = self._identifier(version_id, "version_id")
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create():
                row = self._version_row(connection, version_id)
                if row["status"] != "draft":
                    raise ConflictError("只有草稿版本可以提交会签")
                findings = json.loads(row["findings_json"])
                blocking = [f for f in findings if f["severity"] == "blocking"]
                if blocking:
                    raise ConflictError(f"仍有 {len(blocking)} 项阻断性勾稽异常，不能提交会签")
                acknowledged = json.loads(row["acknowledged_json"])
                missing = [f["finding_key"] for f in findings
                           if f["severity"] == "warning" and f["finding_key"] not in acknowledged]
                if missing:
                    raise ConflictError(f"仍有 {len(missing)} 项警告性异常缺少说明")
                required_roles = json.loads(row["frozen_inputs_json"])["definition"]["body"]["required_signoff_roles"]
                connection.execute("UPDATE snap_versions SET status='pending' WHERE version_id=?",
                                   (version_id,))
                self._audit(connection, actor_id=actor_id, action="snapshot.version.submitted",
                            resource_type="snapshot_version", resource_id=version_id,
                            detail={"required_roles": required_roles})
                return ("snapshot_version", version_id, {"version_id": version_id, "status": "pending"})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.submit:{version_id}",
                                    payload=payload, create=create)

    def sign_off(self, *, request_id: str, actor_id: str, version_id: str,
                 decision: str, comment: str | None = None) -> dict[str, Any]:
        version_id = self._identifier(version_id, "version_id")
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision 只能是 approved 或 rejected")
        payload = {"actor_id": actor_id, "version_id": version_id, "decision": decision,
                   "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")

            def create():
                row = self._version_row(connection, version_id)
                if row["status"] != "pending":
                    raise ConflictError("只有待会签版本可以签署")
                frozen = json.loads(row["frozen_inputs_json"])
                required_roles = set(frozen["definition"]["body"]["required_signoff_roles"])
                if actor.role not in required_roles:
                    raise PermissionDenied("当前定义版本未要求该角色会签")
                role = actor.role
                if connection.execute(
                        "SELECT 1 FROM snap_signoffs WHERE version_id=? AND role=?",
                        (version_id, role)).fetchone():
                    raise ConflictError(f"{role} 角色已经完成会签")
                connection.execute(
                    "INSERT INTO snap_signoffs(version_id,role,actor_id,decision,comment,signed_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (version_id, role, actor_id, decision, comment, self._now()),
                )
                new_status = "pending"
                if decision == "rejected":
                    new_status = "rejected"
                    connection.execute("UPDATE snap_versions SET status='rejected' WHERE version_id=?",
                                       (version_id,))
                self._audit(connection, actor_id=actor_id,
                            action=f"snapshot.version.signoff.{decision}",
                            resource_type="snapshot_version", resource_id=version_id,
                            detail={"role": role, "comment": comment})
                return ("snapshot_version", version_id,
                        {"version_id": version_id, "role": role, "decision": decision,
                         "status": new_status})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.signoff:{version_id}:{decision}",
                                    payload=payload, create=create)

    def sanction_publication(self, *, request_id: str, actor_id: str, version_id: str,
                             comment: str | None = None) -> dict[str, Any]:
        """授权发布：定义要求的角色会签齐备后由管理员发布，发布后内容冻结。"""

        version_id = self._identifier(version_id, "version_id")
        payload = {"actor_id": actor_id, "version_id": version_id, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")

            def create():
                row = self._version_row(connection, version_id)
                if row["status"] != "pending":
                    raise ConflictError("只有待会签版本可以授权发布")
                frozen = json.loads(row["frozen_inputs_json"])
                required_roles = set(frozen["definition"]["body"]["required_signoff_roles"])
                decisions = {sign["role"]: sign["decision"] for sign in
                             self._signoffs(connection, version_id)}
                missing = required_roles - set(decisions)
                if missing:
                    raise ConflictError(f"会签角色尚未齐备: {sorted(missing)}")
                if any(decisions[role] != "approved" for role in required_roles):
                    raise ConflictError("存在拒绝会签，不能发布")
                # 发布前用冻结输入再复算一次，确认与保存结果逐字节一致。
                scope_row = connection.execute(
                    "SELECT * FROM snap_scopes WHERE version_id=?",
                    (frozen["scope"]["version_id"],)).fetchone()
                definition_row = connection.execute(
                    "SELECT * FROM snap_definitions WHERE version_id=?",
                    (frozen["definition"]["version_id"],)).fetchone()
                scope = {"row": scope_row, "body": json.loads(scope_row["body_json"]),
                         "currencies": json.loads(scope_row["currency_json"])}
                definition = {"row": definition_row, "body": json.loads(definition_row["body_json"])}
                results, _findings = self._compute(definition=definition, scope=scope, frozen=frozen)
                if digest(results) != row["results_hash"] or results != json.loads(row["results_json"]):
                    raise ConflictError("冻结输入复算结果与存储结果不一致，已拒绝发布")
                now = self._now()
                connection.execute(
                    "UPDATE snap_versions SET status='published',published_at=?,published_by=? "
                    "WHERE version_id=?", (now, actor_id, version_id))
                connection.execute(
                    "INSERT INTO snap_sanction(version_id,actor_id,comment,sanctioned_at) "
                    "VALUES(?,?,?,?)", (version_id, actor_id, comment, now))
                # 同口径同期间的上一任已发布版本（常规或重述）改为 superseded，
                # 保证同一口径与期间至多只有一个 published 快照。
                prior = connection.execute(
                    "SELECT version_id,kind FROM snap_versions WHERE scope_id=? AND definition_id=? "
                    "AND period_code=? AND status='published' AND version_id<>? "
                    "ORDER BY serial DESC LIMIT 1",
                    (row["scope_id"], row["definition_id"], row["period_code"], version_id)).fetchone()
                superseded_prior = None
                if prior:
                    connection.execute(
                        "UPDATE snap_versions SET status='superseded',restated_by_version_id=? "
                        "WHERE version_id=?", (version_id, prior["version_id"]))
                    superseded_prior = prior["version_id"]
                # 重述目标若仍为 published（理论上已被上一步覆盖），再兜底置为 superseded。
                if row["kind"] == "restatement" and row["restates_version_id"]:
                    target = connection.execute(
                        "SELECT status FROM snap_versions WHERE version_id=?",
                        (row["restates_version_id"],)).fetchone()
                    if target and target["status"] == "published":
                        connection.execute(
                            "UPDATE snap_versions SET status='superseded',restated_by_version_id=? "
                            "WHERE version_id=?", (version_id, row["restates_version_id"]))
                        superseded_prior = superseded_prior or row["restates_version_id"]
                self._audit(connection, actor_id=actor_id, action="snapshot.version.published",
                            resource_type="snapshot_version", resource_id=version_id,
                            detail={"published_at": now, "comment": comment,
                                    "restates_version_id": row["restates_version_id"],
                                    "superseded_prior": superseded_prior})
                return ("snapshot_version", version_id,
                        {"version_id": version_id, "status": "published", "published_at": now})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.publish:{version_id}",
                                    payload=payload, create=create)

    def cancel_version(self, *, request_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        version_id = self._identifier(version_id, "version_id")
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")

            def create():
                row = self._version_row(connection, version_id)
                if row["status"] not in ("draft", "rejected"):
                    raise ConflictError("只有草稿或被拒绝版本可以撤销")
                connection.execute("UPDATE snap_versions SET status='cancelled' WHERE version_id=?",
                                   (version_id,))
                self._audit(connection, actor_id=actor_id, action="snapshot.version.cancelled",
                            resource_type="snapshot_version", resource_id=version_id, detail={})
                return ("snapshot_version", version_id,
                        {"version_id": version_id, "status": "cancelled"})

            return self._idempotent(connection, request_id=request_id,
                                    action=f"snapshot.cancel:{version_id}",
                                    payload=payload, create=create)

    # ----- 读取、复算、下钻 -----

    def _signoffs(self, connection, version_id: str) -> list[dict[str, Any]]:
        return [{"role": row["role"], "actor_id": row["actor_id"], "decision": row["decision"],
                 "comment": row["comment"], "signed_at": row["signed_at"]}
                for row in connection.execute(
                    "SELECT * FROM snap_signoffs WHERE version_id=? ORDER BY role", (version_id,))]

    def get_version(self, actor_id: str, version_id: str) -> dict[str, Any]:
        version_id = self._identifier(version_id, "version_id")
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        row = self._version_row(connection, version_id)
        frozen = json.loads(row["frozen_inputs_json"])
        findings = json.loads(row["findings_json"])
        acknowledged = json.loads(row["acknowledged_json"])
        sanction = connection.execute("SELECT * FROM snap_sanction WHERE version_id=?",
                                      (version_id,)).fetchone()
        # 晚到渠道数据提示：期间与范围相同、但未进入本版本冻结批次集合的新批次。
        late_batches: list[str] = []
        node_ids = [node["node_id"] for node in frozen["scope"]["nodes"]]
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            current = {r["batch_id"] for r in connection.execute(
                f"SELECT DISTINCT batch_id FROM snap_facts WHERE period_code=? AND node_id IN ({placeholders})",
                (row["period_code"], *node_ids)).fetchall()}
            late_batches = sorted(current - set(frozen["batches"]))
        return {
            "version_id": version_id,
            "scope_id": row["scope_id"], "definition_id": row["definition_id"],
            "period_code": row["period_code"], "serial": row["serial"], "kind": row["kind"],
            "status": row["status"], "change_reason": row["change_reason"],
            "restates_version_id": row["restates_version_id"],
            "restated_by_version_id": row["restated_by_version_id"],
            "scope_version_id": frozen["scope"]["version_id"],
            "scope_version_no": frozen["scope"]["version_no"],
            "definition_version_id": frozen["definition"]["version_id"],
            "definition_version_no": frozen["definition"]["version_no"],
            "input_fingerprint": row["input_fingerprint"],
            "results_hash": row["results_hash"],
            "facts": {"count": len(frozen["facts"]), "batches": frozen["batches"],
                      "late_batches": late_batches},
            "comparison_version_id": (frozen.get("comparison") or {}).get("version_id"),
            "findings_summary": {
                "blocking": sum(1 for f in findings if f["severity"] == "blocking"),
                "warning": sum(1 for f in findings if f["severity"] == "warning"),
                "acknowledged": len(acknowledged),
            },
            "signoffs": self._signoffs(connection, version_id),
            "sanction": ({"actor_id": sanction["actor_id"], "comment": sanction["comment"],
                          "sanctioned_at": sanction["sanctioned_at"]} if sanction else None),
            "diff": json.loads(row["diff_json"]) if row["diff_json"] else None,
            "created_by": row["created_by"], "created_at": row["created_at"],
            "published_at": row["published_at"], "published_by": row["published_by"],
            "viewer": {"actor_id": actor.actor_id, "role": actor.role},
        }

    def recompute_version(self, actor_id: str, version_id: str) -> dict[str, Any]:
        """完全从冻结输入复算，并与保存的结果摘要逐字节比对。"""

        version_id = self._identifier(version_id, "version_id")
        connection = self.database.connection
        self._actor(connection, actor_id)
        row = self._version_row(connection, version_id)
        frozen = json.loads(row["frozen_inputs_json"])
        nodes = {node["node_id"]: node for node in frozen["scope"]["nodes"]}
        fx = {code: Decimal(rate) for code, rate in frozen["scope"]["currencies"]["rates"].items()}
        results = evaluate(definition=frozen["definition"]["body"], nodes=nodes, fx=fx,
                           facts=frozen["facts"], comparison=frozen.get("comparison"))
        findings = run_checks(definition=frozen["definition"]["body"], nodes=nodes, results=results,
                              facts=frozen["facts"], fx=fx)
        stored = json.loads(row["results_json"])
        return {
            "version_id": version_id,
            "status": row["status"],
            "input_fingerprint": row["input_fingerprint"],
            "recomputed_hash": digest(results),
            "stored_hash": row["results_hash"],
            "matches": digest(results) == row["results_hash"] and results == stored,
            "finding_count": len(findings),
        }

    def _visible_nodes(self, connection, actor: Actor, nodes: dict[str, dict[str, Any]]) -> set[str]:
        """admin 见全部；其他角色只能见被授权节点及其整棵子树。"""

        if actor.role == "admin":
            return set(nodes)
        granted = {row["node_id"] for row in connection.execute(
            "SELECT node_id FROM snap_node_grants WHERE actor_id=?", (actor.actor_id,)).fetchall()}
        children: dict[str, list[str]] = {}
        for node_id, node in nodes.items():
            children.setdefault(node.get("parent_id"), []).append(node_id)
        visible: set[str] = set()
        stack = [node_id for node_id in granted if node_id in nodes]
        while stack:
            current = stack.pop()
            if current in visible:
                continue
            visible.add(current)
            stack.extend(children.get(current, ()))
        return visible

    def _subtree(self, nodes: dict[str, dict[str, Any]], root: str) -> set[str]:
        result = {root}
        stack = [root]
        while stack:
            current = stack.pop()
            for node_id, node in nodes.items():
                if node.get("parent_id") == current and node_id not in result:
                    result.add(node_id)
                    stack.append(node_id)
        return result

    def query_snapshot(self, actor_id: str, version_id: str, *, brand: str | None = None,
                       segment: str | None = None, region: str | None = None,
                       node_id: str | None = None, drill: str | None = None) -> dict[str, Any]:
        """按品牌、业务板块、地区或节点下钻；无权限明细整段隐藏，不泄露编号与名称。"""

        version_id = self._identifier(version_id, "version_id")
        if drill is not None and drill not in ("brand", "segment", "region"):
            raise ValidationError("drill 只能是 brand、segment 或 region")
        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        row = self._version_row(connection, version_id)
        results = json.loads(row["results_json"])
        frozen = json.loads(row["frozen_inputs_json"])
        nodes = {node["node_id"]: node for node in frozen["scope"]["nodes"]}
        visible = self._visible_nodes(connection, actor, nodes)
        node_results = results["nodes"]

        # 1) 计算“过滤域”：节点下钻取该节点子树；维度过滤取所有匹配节点的子树并集；否则全量。
        if node_id:
            if node_id not in nodes:
                raise NotFoundError("节点不存在")
            if node_id not in visible:
                raise PermissionDenied("无权查看该节点")
            domain = self._subtree(nodes, node_id)
        elif brand or segment or region:
            domain: set[str] = set()
            for nid, node in nodes.items():
                if ((brand is None or node.get("brand_id") == brand)
                        and (segment is None or node.get("segment") == segment)
                        and (region is None or node.get("region") == region)):
                    domain |= self._subtree(nodes, nid)
        else:
            domain = set(nodes)
        scoped = domain & visible
        hidden_count = len(domain - scoped)
        # 不相交最高节点用于合计，避免父子重复计数。
        frontier = sorted(nid for nid in scoped if nodes[nid].get("parent_id") not in scoped)

        def node_payload(nid: str) -> dict[str, Any]:
            payload = node_results[nid]
            return {"node_id": nid, "parent_id": nodes[nid].get("parent_id"),
                    "node_type": nodes[nid]["node_type"], "name": nodes[nid]["name"],
                    "brand_id": nodes[nid].get("brand_id"), "segment": nodes[nid].get("segment"),
                    "region": nodes[nid].get("region"), "measures": payload["measures"],
                    "metrics": payload["metrics"], "fact_count": payload["fact_count"],
                    "batches": payload["batches"]}

        response: dict[str, Any] = {
            "version_id": version_id,
            "status": row["status"],
            "period_code": row["period_code"],
            "presentation_currency": results["presentation_currency"],
            "filters": {"brand": brand, "segment": segment, "region": region,
                        "node_id": node_id, "drill": drill},
            "totals": self._aggregate(node_results, frontier, frozen["definition"]["body"]),
            "partial": hidden_count > 0,
            "hidden_nodes_count": hidden_count,
            "items": [node_payload(nid) for nid in sorted(scoped)],
        }
        if drill:
            response["drill"] = self._drill(node_results, nodes, scoped, drill,
                                            frozen["definition"]["body"])
        return response

    def _aggregate(self, node_results, frontier: list[str], definition_body) -> dict[str, Any]:
        from .metrics import _round, to_number
        measures: dict[str, Decimal] = {}
        for nid in frontier:
            for name, value in node_results[nid]["measures"].items():
                if value is not None:
                    measures[name] = measures.get(name, Decimal("0")) + Decimal(str(value))
        rounded = {name: _round(name, value) for name, value in measures.items()}
        measures_out = {name: to_number(value) for name, value in sorted(rounded.items())}
        metrics_out: dict[str, Any] = {}
        for metric in definition_body["metrics"]:
            metric_id, kind = metric["metric_id"], metric["kind"]
            if kind == "sum":
                metrics_out[metric_id] = measures_out.get(metric["measure"])
            elif kind == "ratio":
                num = rounded.get(metric["numerator_measure"])
                den = rounded.get(metric["denominator_measure"])
                metrics_out[metric_id] = (to_number(_round(metric_id, num / den))
                                          if num is not None and den is not None and den != 0 else None)
            else:
                metrics_out[metric_id] = None  # 同比只在冻结树节点上有定义，跨过滤合计不重算
        return {"node_count": len(frontier), "measures": measures_out, "metrics": metrics_out}

    def _drill(self, node_results, nodes, scoped_nodes: set[str], dimension: str,
               definition_body) -> list[dict[str, Any]]:
        attr = {"brand": "brand_id", "segment": "segment", "region": "region"}[dimension]
        # 带维度标记的节点，若其可见祖先也带标记则被吸收，避免重复计数。
        chosen: list[str] = []
        for nid in sorted(scoped_nodes):
            if not nodes[nid].get(attr):
                continue
            shadowed = False
            ancestor = nodes[nid].get("parent_id")
            while ancestor and ancestor in scoped_nodes:
                if nodes[ancestor].get(attr):
                    shadowed = True
                    break
                ancestor = nodes[ancestor].get("parent_id")
            if not shadowed:
                chosen.append(nid)
        groups: dict[str, list[str]] = {}
        for nid in chosen:
            groups.setdefault(nodes[nid][attr], []).append(nid)
        return [{dimension: key, **self._aggregate(node_results, sorted(group), definition_body)}
                for key, group in sorted(groups.items())]

    def list_versions(self, actor_id: str, *, scope_id: str | None = None,
                      period_code: str | None = None, batch_id: str | None = None,
                      status: str | None = None) -> dict[str, Any]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        query = "SELECT v.* FROM snap_versions v"
        parameters: list[Any] = []
        clauses = []
        if batch_id:
            # 冻结输入采用 canonical JSON（紧凑分隔符），批次既出现在事实行也出现在批次清单。
            safe_like = batch_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append('v.frozen_inputs_json LIKE ? ESCAPE "\\"')
            parameters.append(f'%"batch_id":"{safe_like}"%')
        if scope_id:
            clauses.append("v.scope_id=?")
            parameters.append(scope_id)
        if period_code:
            clauses.append("v.period_code=?")
            parameters.append(period_code)
        if status:
            clauses.append("v.status=?")
            parameters.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY v.created_at DESC, v.serial DESC"
        items = [{"version_id": row["version_id"], "scope_id": row["scope_id"],
                  "definition_id": row["definition_id"], "period_code": row["period_code"],
                  "serial": row["serial"], "kind": row["kind"], "status": row["status"],
                  "change_reason": row["change_reason"],
                  "restates_version_id": row["restates_version_id"],
                  "restated_by_version_id": row["restated_by_version_id"],
                  "published_at": row["published_at"]}
                 for row in connection.execute(query, parameters).fetchall()]
        return {"items": items}

    # ----- 差异报告 -----

    def _diff_versions(self, old_results: dict[str, Any], new_results: dict[str, Any], *,
                       old_frozen: dict[str, Any], new_frozen: dict[str, Any]) -> dict[str, Any]:
        node_changes: list[dict[str, Any]] = []
        impact_nodes: set[str] = set()
        for node_id in sorted(set(old_results["nodes"]) | set(new_results["nodes"])):
            old_node = old_results["nodes"].get(node_id, {"measures": {}, "metrics": {}})
            new_node = new_results["nodes"].get(node_id, {"measures": {}, "metrics": {}})
            changes = []
            for section in ("measures", "metrics"):
                keys = set(old_node.get(section, {})) | set(new_node.get(section, {}))
                for key in sorted(keys):
                    old_value = old_node.get(section, {}).get(key)
                    new_value = new_node.get(section, {}).get(key)
                    if old_value != new_value:
                        changes.append({"kind": section, "key": key, "old": old_value,
                                        "new": new_value,
                                        "delta": (None if old_value is None or new_value is None
                                                  else round(new_value - old_value, 6))})
            if changes:
                impact_nodes.add(node_id)
                node_changes.append({"node_id": node_id, "changes": changes})
        old_hashes = {f["fact_id"]: f["measures_hash"] for f in old_frozen["facts"]}
        new_hashes = {f["fact_id"]: f["measures_hash"] for f in new_frozen["facts"]}
        added = sorted(fid for fid in new_hashes if old_hashes.get(fid) is None)
        removed = sorted(fid for fid in old_hashes if new_hashes.get(fid) is None)
        revised = sorted(fid for fid in new_hashes
                         if fid in old_hashes and old_hashes[fid] != new_hashes[fid])
        old_batches, new_batches = set(old_frozen["batches"]), set(new_frozen["batches"])
        return {
            "old_version_hash": digest(old_results),
            "new_version_hash": digest(new_results),
            "facts_added": added,
            "facts_removed": removed,
            "facts_revised": revised,
            "batches_added": sorted(new_batches - old_batches),
            "batches_removed": sorted(old_batches - new_batches),
            "impacted_node_ids": sorted(impact_nodes),
            "impacted_node_count": len(impact_nodes),
            "node_changes": node_changes,
        }
