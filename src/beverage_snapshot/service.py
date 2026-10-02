"""组合经营快照领域服务。

在基础服务的组织、操作者、事务、幂等收据与哈希审计链之上，实现：

1. 指标定义/组织范围/币种/期间的版本化与冻结；
2. 接收带来源与批次号的经营事实（晚到批次保留原行，不覆盖）；
3. 快照版本从冻结事实复算高端占比、同比、单位成本与 EBITDA；
4. 发布前勾稽、异常说明与双角色授权会签；
5. 已发布快照不可变，晚到数据只能产生新的重述版本并展示影响范围；
6. 按板块/品牌/地区下钻，按操作者数据范围隐藏无权限明细。

全部工作流状态存于 SQLite，服务重启后未完成的审核与会签可以继续。
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Any, Callable

from beverage_ops_foundation.audit import append_event, canonical_json, digest, verify_chain
from beverage_ops_foundation.clock import Clock, SystemClock
from beverage_ops_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from beverage_ops_foundation.models import Actor

from . import calc, domain
from .storage import SnapshotDatabase

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
PERIOD = re.compile(r"^\d{4}(H[12]|Q[1-4])$")
CURRENCY = re.compile(r"^[A-Z]{3}$")

# 发布需要的会签角色（授权会签：复核人 + 管理员，且不能是同一人）。
REQUIRED_APPROVAL_ROLES = ("reviewer", "admin")
# 同比变动绝对值超过该阈值时给出异常提示（复算期间为半年，50%）。
YOY_OUTLIER = 0.5
# 板块销量口径：啤酒千升，白酒吨，二者不能跨板块汇总销量。
SEGMENT_VOLUME_UNIT = {domain.SEGMENT_BEER: "kL", domain.SEGMENT_BAIJIU: "tonne"}


class SnapshotService:
    """协调快照领域的权限、版本、事务、复算与审计。"""

    def __init__(self, database: SnapshotDatabase, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type=resource_type, resource_id=resource_id,
                     detail=detail, occurred_at=self._now())

    # ------------------------------------------------------------------ 数据权限

    def grant_data_scope(self, *, request_id: str, actor_id: str, target_actor_id: str,
                         scope_type: str, scope_value: str) -> dict[str, Any]:
        """为操作者授予板块/品牌/地区数据范围，仅管理员可授权。"""

        payload = {"actor_id": actor_id, "target_actor_id": target_actor_id,
                   "scope_type": scope_type, "scope_value": scope_value}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            target = self._actor(connection, target_actor_id)
            if scope_type not in ("brand", "segment", "region"):
                raise ValidationError("scope_type 必须是 brand / segment / region")
            if scope_type == "segment" and not domain.is_segment(scope_value):
                raise ValidationError("板块取值无效")
            scope_value = self._identifier(scope_value, "scope_value")
            if target.organization_id != actor.organization_id:
                raise PermissionDenied("不能为其他组织的操作者授权")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT 1 FROM actor_data_scopes WHERE actor_id=? "
                    "AND scope_type=? AND scope_value=?",
                    (target_actor_id, scope_type, scope_value),
                ).fetchone()
                if not existing:
                    connection.execute(
                        "INSERT INTO actor_data_scopes"
                        "(actor_id,scope_type,scope_value,granted_by,granted_at) "
                        "VALUES(?,?,?,?,?)",
                        (target_actor_id, scope_type, scope_value, actor_id, self._now()),
                    )
                    self._audit(connection, actor_id=actor_id, action="data_scope.granted",
                                resource_type="actor", resource_id=target_actor_id,
                                detail={"scope_type": scope_type, "scope_value": scope_value})
                return "data_scope", f"{target_actor_id}:{scope_type}:{scope_value}", \
                    {"target_actor_id": target_actor_id,
                     "scope_type": scope_type, "scope_value": scope_value}

            return self._idempotent(connection, request_id=request_id,
                                    action="grant_data_scope", payload=payload, create=create)

    @staticmethod
    def _scope_map(connection, actor_id: str) -> dict[str, set[str]]:
        scopes: dict[str, set[str]] = {}
        rows = connection.execute(
            "SELECT scope_type,scope_value FROM actor_data_scopes WHERE actor_id=?",
            (actor_id,),
        )
        for row in rows:
            scopes.setdefault(row["scope_type"], set()).add(row["scope_value"])
        return scopes

    @staticmethod
    def _in_scope(actor: Actor, scopes: dict[str, set[str]],
                  segment: str, brand: str, region: str) -> bool:
        """任一授权维度命中即可见（板块/品牌/地区构成下钻层级）。"""

        if actor.role == "admin":
            return True
        return bool(
            segment in scopes.get("segment", set())
            or brand in scopes.get("brand", set())
            or region in scopes.get("region", set())
        )

    # ------------------------------------------------------------------ 定义版本

    def create_definition_version(self, *, request_id: str, actor_id: str, fiscal_period: str,
                                  reporting_ccy: str, definitions: list[dict[str, Any]],
                                  exchange_rates: dict[str, float],
                                  organization_scope: list[str]) -> dict[str, Any]:
        """创建期间的指标定义版本（组织范围、币种与汇率随版本冻结）。"""

        payload = {"actor_id": actor_id, "fiscal_period": fiscal_period,
                   "reporting_ccy": reporting_ccy, "definitions": definitions,
                   "exchange_rates": exchange_rates, "organization_scope": organization_scope}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            fiscal_period = self._text(fiscal_period, "fiscal_period", 16)
            if not PERIOD.fullmatch(fiscal_period):
                raise ValidationError("fiscal_period 格式应为 2026H1 / 2026Q1")
            reporting_ccy = self._text(reporting_ccy, "reporting_ccy", 3).upper()
            if not CURRENCY.fullmatch(reporting_ccy):
                raise ValidationError("reporting_ccy 必须是三位 ISO 币种代码")
            if not isinstance(exchange_rates, dict) or not exchange_rates:
                raise ValidationError("exchange_rates 必须是非空币种汇率对象")
            rates: dict[str, float] = {}
            for ccy, rate in exchange_rates.items():
                if not CURRENCY.fullmatch(str(ccy)) or not isinstance(rate, (int, float)) \
                        or isinstance(rate, bool) or not math.isfinite(rate) or rate <= 0:
                    raise ValidationError("汇率必须是正数，键为三位币种代码")
                rates[str(ccy)] = float(rate)
            if rates.get(reporting_ccy) != 1.0:
                raise ValidationError("报告币种对自身汇率必须为 1")
            if not isinstance(organization_scope, list) or not organization_scope:
                raise ValidationError("organization_scope 必须是非空组织编号列表")
            scope_ids = [self._identifier(value, "organization_scope") for value in organization_scope]
            for org_id in scope_ids:
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (org_id,)).fetchone() is None:
                    raise NotFoundError(f"组织 {org_id} 不存在")
            catalog = self._validate_definitions(definitions)

            max_no = connection.execute(
                "SELECT COALESCE(MAX(version_no),0) AS m FROM definition_versions "
                "WHERE fiscal_period=?", (fiscal_period,)
            ).fetchone()["m"]
            version_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO definition_versions(definition_version_id,version_no,"
                    "organization_scope_json,reporting_ccy,fiscal_period,exchange_rate_json,"
                    "definitions_hash,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (version_id, max_no + 1, canonical_json(scope_ids), reporting_ccy,
                     fiscal_period, canonical_json(rates), digest(catalog), "active",
                     actor_id, self._now()),
                )
                for item in catalog:
                    connection.execute(
                        "INSERT INTO metric_definitions(definition_version_id,metric_id,"
                        "metric_kind,display_name,unit,description,formula,version) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (version_id, item["metric_id"], item["metric_kind"],
                         item["display_name"], item["unit"], item["description"],
                         item.get("formula", ""), 1),
                    )
                self._audit(connection, actor_id=actor_id,
                            action="definition_version.created",
                            resource_type="definition_version", resource_id=version_id,
                            detail={"fiscal_period": fiscal_period, "version_no": max_no + 1,
                                    "reporting_ccy": reporting_ccy,
                                    "definitions_hash": digest(catalog)})
                return "definition_version", version_id, \
                    {"definition_version_id": version_id, "version_no": max_no + 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_definition_version", payload=payload,
                                    create=create)

    @staticmethod
    def _validate_definitions(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(definitions, list) or not definitions:
            raise ValidationError("definitions 必须是非空指标定义列表")
        allowed = {(m, "fact") for m in domain.FACT_METRICS} | \
                  {(m, "derived") for m in domain.DERIVED_METRICS}
        seen: set[str] = set()
        catalog: list[dict[str, Any]] = []
        for item in definitions:
            if not isinstance(item, dict):
                raise ValidationError("每条指标定义必须是对象")
            metric_id = str(item.get("metric_id", "")).strip()
            kind = str(item.get("metric_kind", "")).strip()
            if (metric_id, kind) not in allowed:
                raise ValidationError(f"指标 {metric_id}({kind}) 不在标准目录内")
            if metric_id in seen:
                raise ValidationError(f"指标 {metric_id} 重复定义")
            seen.add(metric_id)
            display_name = str(item.get("display_name", "")).strip()
            unit = str(item.get("unit", "")).strip()
            description = str(item.get("description", "")).strip()
            formula = str(item.get("formula", "")).strip()
            if not display_name or not unit or not description:
                raise ValidationError("指标定义的名称、单位与口径说明不能为空")
            if kind == "derived" and not formula:
                raise ValidationError(f"派生指标 {metric_id} 必须给出复算公式")
            catalog.append({"metric_id": metric_id, "metric_kind": kind,
                            "display_name": display_name, "unit": unit,
                            "description": description, "formula": formula})
        missing = {m for m, _ in allowed} - seen
        if missing:
            raise ValidationError(f"缺少标准指标定义: {sorted(missing)}")
        return catalog

    def freeze_definition_version(self, *, request_id: str, actor_id: str,
                                  definition_version_id: str) -> dict[str, Any]:
        """冻结定义版本；冻结后事实与快照只能引用该版本。"""

        payload = {"actor_id": actor_id, "definition_version_id": definition_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            row = self._definition_row(connection, definition_version_id)
            if row["status"] == "frozen":
                raise ConflictError("定义版本已经冻结")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE definition_versions SET status='frozen',frozen_at=? "
                    "WHERE definition_version_id=?",
                    (self._now(), definition_version_id),
                )
                self._audit(connection, actor_id=actor_id,
                            action="definition_version.frozen",
                            resource_type="definition_version",
                            resource_id=definition_version_id,
                            detail={"version_no": row["version_no"]})
                return "definition_version", definition_version_id, \
                    {"definition_version_id": definition_version_id, "status": "frozen"}

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_definition_version", payload=payload,
                                    create=create)

    @staticmethod
    def _definition_row(connection, definition_version_id: str):
        row = connection.execute(
            "SELECT * FROM definition_versions WHERE definition_version_id=?",
            (definition_version_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("指标定义版本不存在")
        return row

    @staticmethod
    def _rates(row) -> dict[str, float]:
        import json
        return json.loads(row["exchange_rate_json"])

    def get_definition_version(self, definition_version_id: str) -> dict[str, Any]:
        import json
        row = self.database.connection.execute(
            "SELECT * FROM definition_versions WHERE definition_version_id=?",
            (definition_version_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("指标定义版本不存在")
        definitions = [
            {"metric_id": r["metric_id"], "metric_kind": r["metric_kind"],
             "display_name": r["display_name"], "unit": r["unit"],
             "description": r["description"], "formula": r["formula"],
             "version": r["version"]}
            for r in self.database.connection.execute(
                "SELECT * FROM metric_definitions WHERE definition_version_id=? "
                "ORDER BY metric_kind,metric_id", (definition_version_id,))
        ]
        return {"definition_version_id": row["definition_version_id"],
                "version_no": row["version_no"], "status": row["status"],
                "fiscal_period": row["fiscal_period"],
                "reporting_ccy": row["reporting_ccy"],
                "organization_scope": json.loads(row["organization_scope_json"]),
                "exchange_rates": json.loads(row["exchange_rate_json"]),
                "definitions_hash": row["definitions_hash"],
                "definitions": definitions,
                "created_by": row["created_by"], "created_at": row["created_at"],
                "frozen_at": row["frozen_at"]}

    # ------------------------------------------------------------------ 经营事实

    def record_fact(self, *, request_id: str, actor_id: str, definition_version_id: str,
                    batch_id: str, source: str, segment: str, brand: str, region: str,
                    period: str, metric_id: str, value: float, unit: str | None = None,
                    currency: str | None = None, is_late: bool = False) -> dict[str, Any]:
        """接收一条带来源与批次号的经营事实。

        晚到数据以新批次写入（is_late=True），从不覆盖已有批次行；
        已发布快照通过绑定表钉住输入，晚到事实对其没有任何影响。
        """

        payload = {"actor_id": actor_id, "definition_version_id": definition_version_id,
                   "batch_id": batch_id, "source": source, "segment": segment,
                   "brand": brand, "region": region, "period": period,
                   "metric_id": metric_id, "value": value, "unit": unit,
                   "currency": currency, "is_late": is_late}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            definition = self._definition_row(connection, definition_version_id)
            if definition["status"] != "frozen":
                raise ConflictError("只能向已冻结的指标定义版本上报事实")
            batch_id = self._identifier(batch_id, "batch_id")
            source = self._text(source, "source", 80)
            brand = self._identifier(brand, "brand")
            region = self._identifier(region, "region")
            period = self._text(period, "period", 16)
            if not PERIOD.fullmatch(period):
                raise ValidationError("period 格式应为 2026H1 / 2026Q1")
            if not domain.is_segment(segment):
                raise ValidationError("segment 必须是 beer 或 baijiu")
            metric_row = connection.execute(
                "SELECT * FROM metric_definitions WHERE definition_version_id=? AND metric_id=?",
                (definition_version_id, metric_id),
            ).fetchone()
            if metric_row is None:
                raise ValidationError("指标不在该定义版本目录中")
            if metric_row["metric_kind"] != "fact":
                raise ValidationError("派生指标不能作为事实上报，必须从事实复算")
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or not math.isfinite(value):
                raise ValidationError("value 必须是有限数字")
            value = float(value)
            rates = self._rates(definition)
            if metric_id in calc.MONEY_METRICS:
                currency = str(currency or definition["reporting_ccy"]).upper()
                if currency not in rates:
                    raise ValidationError(f"币种 {currency} 不在冻结汇率表中")
                unit = currency
            else:
                expected_unit = SEGMENT_VOLUME_UNIT[segment]
                unit = str(unit or expected_unit).strip()
                if unit != expected_unit:
                    raise ValidationError(
                        f"{segment} 销量口径单位必须是 {expected_unit}")
                currency = ""
            scopes = self._scope_map(connection, actor_id)
            if not self._in_scope(actor, scopes, segment, brand, region):
                raise PermissionDenied("不能上报本人数据范围之外的事实")
            fact_hash = digest({
                "definition_version_id": definition_version_id, "batch_id": batch_id,
                "source": source, "segment": segment, "brand": brand, "region": region,
                "period": period, "metric_id": metric_id, "value": value, "unit": unit,
                "currency": currency, "is_late": bool(is_late),
            })

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM business_facts WHERE definition_version_id=? AND batch_id=? "
                    "AND source=? AND segment=? AND brand=? AND region=? AND period=? AND metric_id=?",
                    (definition_version_id, batch_id, source, segment, brand, region,
                     period, metric_id),
                ).fetchone()
                if existing:
                    if existing["payload_hash"] != fact_hash:
                        raise ConflictError("同批次同维度事实已存在且内容不同，请使用新批次号")
                    return "business_fact", existing["fact_id"], \
                        {"fact_id": existing["fact_id"], "replayed_business_key": True}
                fact_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO business_facts(fact_id,definition_version_id,batch_id,source,"
                    "segment,brand,region,period,metric_id,value,unit,currency,is_late,"
                    "payload_hash,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fact_id, definition_version_id, batch_id, source, segment, brand, region,
                     period, metric_id, value, unit, currency, 1 if is_late else 0,
                     fact_hash, actor_id, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="fact.recorded",
                            resource_type="business_fact", resource_id=fact_id,
                            detail={"definition_version_id": definition_version_id,
                                    "batch_id": batch_id, "source": source,
                                    "segment": segment, "brand": brand, "region": region,
                                    "period": period, "metric_id": metric_id,
                                    "is_late": bool(is_late), "payload_hash": fact_hash})
                return "business_fact", fact_id, {"fact_id": fact_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_fact", payload=payload, create=create)

    def list_facts(self, actor_id: str, definition_version_id: str, *,
                   batch_id: str | None = None, segment: str | None = None,
                   brand: str | None = None, region: str | None = None,
                   period: str | None = None) -> dict[str, Any]:
        """查询事实，自动隐藏操作者数据范围之外的明细。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        sql = "SELECT * FROM business_facts WHERE definition_version_id=?"
        parameters: list[Any] = [definition_version_id]
        for column, value in (("batch_id", batch_id), ("segment", segment),
                              ("brand", brand), ("region", region), ("period", period)):
            if value is not None:
                sql += f" AND {column}=?"
                parameters.append(value)
        sql += " ORDER BY period,segment,brand,region,metric_id,recorded_at"
        scopes = self._scope_map(connection, actor_id)
        items = []
        hidden = 0
        for row in connection.execute(sql, parameters):
            if not self._in_scope(actor, scopes, row["segment"], row["brand"], row["region"]):
                hidden += 1
                continue
            items.append(self._fact_dict(row))
        return {"items": items, "hidden_count": hidden}

    @staticmethod
    def _fact_dict(row) -> dict[str, Any]:
        return {"fact_id": row["fact_id"], "definition_version_id": row["definition_version_id"],
                "batch_id": row["batch_id"], "source": row["source"],
                "segment": row["segment"], "brand": row["brand"], "region": row["region"],
                "period": row["period"], "metric_id": row["metric_id"],
                "value": row["value"], "unit": row["unit"],
                "currency": row["currency"] or None, "is_late": bool(row["is_late"]),
                "recorded_by": row["recorded_by"], "recorded_at": row["recorded_at"]}

    # ------------------------------------------------------------------ 快照工作流

    def _latest_facts(self, connection, definition_version_id: str, period: str) -> list[Any]:
        rows = connection.execute(
            "SELECT * FROM business_facts WHERE definition_version_id=? AND period IN (?,?)",
            (definition_version_id, period, calc.prior_period(period) or ""),
        ).fetchall()
        current = calc.latest_batch_rows(rows, period=period)
        prior_label = calc.prior_period(period)
        prior = calc.latest_batch_rows(rows, period=prior_label) if prior_label else []
        return current + prior

    def _bind_and_compute(self, connection, version_row) -> tuple[str, list[Any], dict[str, Any]]:
        definition = self._definition_row(connection, version_row["definition_version_id"])
        rates = self._rates(definition)
        period = version_row["fiscal_period"]
        prior_label = calc.prior_period(period)
        bound = [r for r in connection.execute(
            "SELECT f.* FROM snapshot_fact_bindings b JOIN business_facts f "
            "ON f.fact_id=b.fact_id WHERE b.snapshot_version_id=?",
            (version_row["snapshot_version_id"],)).fetchall()]
        current = [r for r in bound if r["period"] == period]
        prior = [r for r in bound if r["period"] == prior_label]
        fingerprint = digest([[r["fact_id"], r["payload_hash"]]
                              for r in sorted(bound, key=lambda x: x["fact_id"])])
        result = calc.compute_result(period=period,
                                     reporting_ccy=definition["reporting_ccy"],
                                     current_rows=current, prior_rows=prior, rates=rates)
        return fingerprint, bound, result

    def create_snapshot(self, *, request_id: str, actor_id: str,
                        definition_version_id: str, period: str) -> dict[str, Any]:
        """基于定义版本与当时最晚批次事实创建 v1 草稿并完成首轮勾稽。"""

        payload = {"actor_id": actor_id,
                   "definition_version_id": definition_version_id, "period": period}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            definition = self._definition_row(connection, definition_version_id)
            if definition["status"] != "frozen":
                raise ConflictError("只能为已冻结的定义版本创建快照")
            period = self._text(period, "period", 16)
            if not PERIOD.fullmatch(period):
                raise ValidationError("period 格式应为 2026H1 / 2026Q1")
            existing = connection.execute(
                "SELECT 1 FROM snapshot_versions WHERE definition_version_id=? AND fiscal_period=?",
                (definition_version_id, period),
            ).fetchone()
            if existing:
                raise ConflictError("该期间已存在快照版本，重述请创建新版本")
            version_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                self._materialize_snapshot(connection, version_row_kwargs={
                    "snapshot_version_id": version_id,
                    "definition_version_id": definition_version_id,
                    "fiscal_period": period, "version_no": 1,
                    "parent_version_id": None, "restatement_reason": None,
                    "change_summary": None, "created_by": actor_id,
                }, actor_id=actor_id)
                return "snapshot_version", version_id, {"snapshot_version_id": version_id, "version_no": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_snapshot", payload=payload, create=create)

    def _materialize_snapshot(self, connection, *, version_row_kwargs: dict[str, Any],
                              actor_id: str) -> str:
        """插入版本行、绑定最晚批次事实、复算并勾稽，返回版本号。"""

        period = version_row_kwargs["fiscal_period"]
        definition_version_id = version_row_kwargs["definition_version_id"]
        version_id = version_row_kwargs["snapshot_version_id"]
        chosen = self._latest_facts(connection, definition_version_id, period)
        connection.execute(
            "INSERT INTO snapshot_versions(snapshot_version_id,definition_version_id,"
            "fiscal_period,version_no,state,parent_version_id,restatement_reason,"
            "change_summary,input_fingerprint,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, definition_version_id, period, version_row_kwargs["version_no"],
             domain.STATE_DRAFT, version_row_kwargs["parent_version_id"],
             version_row_kwargs["restatement_reason"], version_row_kwargs["change_summary"],
             "", actor_id, self._now()),
        )
        for row in chosen:
            connection.execute(
                "INSERT INTO snapshot_fact_bindings(snapshot_version_id,fact_id,bound_at) "
                "VALUES(?,?,?)", (version_id, row["fact_id"], self._now()))
        self._recompute_and_persist(connection, version_id)
        self._run_reconciliation(connection, version_id)
        self._audit(connection, actor_id=actor_id, action="snapshot.created",
                    resource_type="snapshot_version", resource_id=version_id,
                    detail={"definition_version_id": definition_version_id,
                            "fiscal_period": period,
                            "version_no": version_row_kwargs["version_no"],
                            "bound_facts": len(chosen)})
        return version_id

    def _recompute_and_persist(self, connection, version_id: str) -> dict[str, Any]:
        version = self._version_row(connection, version_id)
        fingerprint, _bound, result = self._bind_and_compute(connection, version)
        connection.execute(
            "UPDATE snapshot_versions SET input_fingerprint=?,results_json=? "
            "WHERE snapshot_version_id=?",
            (fingerprint, canonical_json(result), version_id))
        return result

    def create_restatement(self, *, request_id: str, actor_id: str,
                           definition_version_id: str, period: str,
                           reason: str, change_summary: str) -> dict[str, Any]:
        """晚到渠道数据等只允许通过重述产生新版本，从不静默改动已发布快照。"""

        payload = {"actor_id": actor_id,
                   "definition_version_id": definition_version_id, "period": period,
                   "reason": reason, "change_summary": change_summary}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            definition = self._definition_row(connection, definition_version_id)
            period = self._text(period, "period", 16)
            if not PERIOD.fullmatch(period):
                raise ValidationError("period 格式应为 2026H1 / 2026Q1")
            if reason not in domain.RESTATEMENT_REASONS:
                raise ValidationError("重述原因不在允许范围内")
            change_summary = self._text(change_summary, "change_summary", 500)
            parent = connection.execute(
                "SELECT * FROM snapshot_versions WHERE definition_version_id=? "
                "AND fiscal_period=? AND state='published' "
                "ORDER BY version_no DESC LIMIT 1",
                (definition_version_id, period),
            ).fetchone()
            if parent is None:
                raise ConflictError("只有已发布快照才能发起重述")
            open_newer = connection.execute(
                "SELECT 1 FROM snapshot_versions WHERE definition_version_id=? "
                "AND fiscal_period=? AND state IN ('draft','reconciled','submitted')",
                (definition_version_id, period),
            ).fetchone()
            if open_newer:
                raise ConflictError("已有进行中的新版本，请先完成其审核与发布")
            version_no = connection.execute(
                "SELECT COALESCE(MAX(version_no),0) AS m FROM snapshot_versions "
                "WHERE definition_version_id=? AND fiscal_period=?",
                (definition_version_id, period),
            ).fetchone()["m"] + 1
            version_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                self._materialize_snapshot(connection, version_row_kwargs={
                    "snapshot_version_id": version_id,
                    "definition_version_id": definition_version_id,
                    "fiscal_period": period, "version_no": version_no,
                    "parent_version_id": parent["snapshot_version_id"],
                    "restatement_reason": reason,
                    "change_summary": change_summary,
                    "created_by": actor_id,
                }, actor_id=actor_id)
                return "snapshot_version", version_id, \
                    {"snapshot_version_id": version_id, "version_no": version_no,
                     "parent_version_id": parent["snapshot_version_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_restatement", payload=payload,
                                    create=create)

    @staticmethod
    def _version_row(connection, snapshot_version_id: str):
        row = connection.execute(
            "SELECT * FROM snapshot_versions WHERE snapshot_version_id=?",
            (snapshot_version_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("快照版本不存在")
        return row

    # ------------------------------------------------------------------ 勾稽

    def _refresh_bindings(self, connection, version) -> int:
        """把草稿快照绑定刷新为当前最晚批次事实；已发布版本绝不走此路径。"""

        chosen = self._latest_facts(connection, version["definition_version_id"],
                                    version["fiscal_period"])
        connection.execute(
            "DELETE FROM snapshot_fact_bindings WHERE snapshot_version_id=?",
            (version["snapshot_version_id"],))
        for row in chosen:
            connection.execute(
                "INSERT INTO snapshot_fact_bindings(snapshot_version_id,fact_id,bound_at) "
                "VALUES(?,?,?)", (version["snapshot_version_id"], row["fact_id"], self._now()))
        return len(chosen)

    def reconcile(self, *, actor_id: str, snapshot_version_id: str) -> dict[str, Any]:
        """刷新到最晚批次、复算并跑一遍勾稽规则，全部阻塞项消除后才可提交。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            version = self._version_row(connection, snapshot_version_id)
            if version["state"] not in (domain.STATE_DRAFT, domain.STATE_RECONCILED):
                raise ConflictError("当前状态不能重新勾稽")
            bound_count = self._refresh_bindings(connection, version)
            self._recompute_and_persist(connection, snapshot_version_id)
            issues = self._run_reconciliation(connection, snapshot_version_id)
            blocking = [i for i in issues if i["severity"] == "blocking" and i["status"] == "open"]
            state = domain.STATE_RECONCILED if not blocking else domain.STATE_DRAFT
            connection.execute(
                "UPDATE snapshot_versions SET state=? WHERE snapshot_version_id=?",
                (state, snapshot_version_id))
            self._audit(connection, actor_id=actor_id, action="snapshot.reconciled",
                        resource_type="snapshot_version", resource_id=snapshot_version_id,
                        detail={"state": state, "bound_facts": bound_count,
                                "open_issues": len(
                                    [i for i in issues if i["status"] == "open"])})
            return {"snapshot_version_id": snapshot_version_id, "state": state,
                    "bound_fact_count": bound_count, "issues": issues}

    def _run_reconciliation(self, connection, version_id: str) -> list[dict[str, Any]]:
        """根据绑定事实复算结果生成勾稽问题；每次重算刷新未说明的问题清单。"""

        connection.execute("DELETE FROM reconciliation_issues WHERE snapshot_version_id=? "
                           "AND status='open'", (version_id,))
        version = self._version_row(connection, version_id)
        _fingerprint, bound, result = self._bind_and_compute(connection, version)
        issues: list[dict[str, Any]] = []

        def add(code: str, severity: str, message: str, dimensions: dict[str, str]) -> None:
            # 已书面说明并关闭的同维度异常，在数据未再变化时不重复挂起。
            explained = connection.execute(
                "SELECT 1 FROM reconciliation_issues WHERE snapshot_version_id=? "
                "AND code=? AND dimensions_json=? AND status='resolved' LIMIT 1",
                (version_id, code, canonical_json(dimensions)),
            ).fetchone()
            if explained:
                return
            issue_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO reconciliation_issues(issue_id,snapshot_version_id,code,"
                "severity,message,dimensions_json,status) VALUES(?,?,?,?,?,?, 'open')",
                (issue_id, version_id, code, severity, message,
                 canonical_json(dimensions)),
            )

        late_groups: dict[tuple, set[str]] = {}
        raw: dict[tuple, dict[str, float]] = {}
        for row in bound:
            if row["period"] != version["fiscal_period"]:
                continue
            key = (row["segment"], row["brand"], row["region"])
            raw.setdefault(key, {})[row["metric_id"]] = row["value"]
            if row["is_late"]:
                late_groups.setdefault(key, set()).add(row["batch_id"])
        for group in result["groups"]:
            key = (group["segment"], group["brand"], group["region"])
            metrics = group["metrics"]
            dims = {"segment": group["segment"], "brand": group["brand"],
                    "region": group["region"]}
            sales_volume = metrics.get("sales_volume")
            premium_volume = metrics.get("premium_volume")
            gross = metrics.get("gross_revenue")
            premium_revenue = metrics.get("premium_revenue")
            present = raw.get(key, {})
            if sales_volume is not None and premium_volume is not None \
                    and premium_volume > sales_volume:
                add("premium_exceeds_volume", "blocking",
                    "高端销量超过板块总销量，销量口径勾稽不一致", dims)
            if gross is not None and premium_revenue is not None and premium_revenue > gross:
                add("premium_exceeds_revenue", "blocking",
                    "高端营业额超过总营业额，收入口径勾稽不一致", dims)
            if "gross_revenue" in present and (
                    "sales_volume" not in present or "cogs" not in present):
                missing = [m for m in ("sales_volume", "cogs") if m not in present]
                add("missing_core_input", "blocking",
                    f"存在营业额但缺少核心输入 {missing}，无法复算单位成本与 EBITDA", dims)
            if metrics.get("yoy_growth") is None:
                add("yoy_baseline_missing", "warning",
                    "缺少上年同期营业额，同比增长无法复算", dims)
            elif abs(metrics["yoy_growth"]) > YOY_OUTLIER:
                add("yoy_outlier", "warning",
                    f"同比增幅 {metrics['yoy_growth']:.2%} 超出阈值，需异常说明", dims)
        for (segment, brand, region), batches in sorted(late_groups.items()):
            add("late_channel_data", "warning",
                "包含晚到渠道批次，重述时需说明影响范围",
                {"segment": segment, "brand": brand, "region": region,
                 "batches": ",".join(sorted(batches))})
        return self.list_issues(connection, version_id)

    def explain_issue(self, *, request_id: str, actor_id: str, issue_id: str,
                      explanation: str) -> dict[str, Any]:
        """对勾稽异常给出书面说明；阻塞性问题不允许用说明绕过。"""

        payload = {"actor_id": actor_id, "issue_id": issue_id, "explanation": explanation}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            row = connection.execute(
                "SELECT * FROM reconciliation_issues WHERE issue_id=?", (issue_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("勾稽问题不存在")
            if row["status"] == domain.EXCEPTION_RESOLVED:
                raise ConflictError("问题已经完成说明")
            if row["severity"] == "blocking":
                raise ConflictError("阻塞性勾稽问题必须补数据后重新勾稽，不能以说明关闭")
            explanation = self._text(explanation, "explanation", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE reconciliation_issues SET status='resolved',explanation=?,"
                    "resolved_by=?,resolved_at=? WHERE issue_id=?",
                    (explanation, actor_id, self._now(), issue_id),
                )
                self._audit(connection, actor_id=actor_id, action="issue.explained",
                            resource_type="reconciliation_issue", resource_id=issue_id,
                            detail={"snapshot_version_id": row["snapshot_version_id"],
                                    "code": row["code"]})
                return "reconciliation_issue", issue_id, {"issue_id": issue_id, "status": "resolved"}

            return self._idempotent(connection, request_id=request_id,
                                    action="explain_issue", payload=payload, create=create)

    @staticmethod
    def list_issues(connection, snapshot_version_id: str) -> list[dict[str, Any]]:
        import json
        items = []
        for row in connection.execute(
            "SELECT * FROM reconciliation_issues WHERE snapshot_version_id=? "
            "ORDER BY CASE severity WHEN 'blocking' THEN 0 ELSE 1 END, issue_id",
            (snapshot_version_id,)):
            items.append({"issue_id": row["issue_id"],
                          "snapshot_version_id": row["snapshot_version_id"],
                          "code": row["code"], "severity": row["severity"],
                          "message": row["message"],
                          "dimensions": json.loads(row["dimensions_json"]),
                          "status": row["status"], "explanation": row["explanation"],
                          "resolved_by": row["resolved_by"],
                          "resolved_at": row["resolved_at"]})
        return items

    # ------------------------------------------------------------------ 会签发布

    def submit_snapshot(self, *, actor_id: str, snapshot_version_id: str) -> dict[str, Any]:
        """勾稽与异常说明全部完成后提交会签。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            version = self._version_row(connection, snapshot_version_id)
            if version["state"] not in (domain.STATE_DRAFT, domain.STATE_RECONCILED):
                raise ConflictError("只有草稿状态可以提交会签")
            # 提交前最后一次刷新到最晚批次并重跑勾稽，避免勾稽后又晚到的批次
            # 未经异常说明就进入会签。
            self._refresh_bindings(connection, version)
            self._recompute_and_persist(connection, snapshot_version_id)
            self._run_reconciliation(connection, snapshot_version_id)
            issues = self.list_issues(connection, snapshot_version_id)
            opens = [i for i in issues if i["status"] == "open"]
            blocking = [i for i in opens if i["severity"] == "blocking"]
            if blocking:
                raise ConflictError("仍有阻塞性勾稽问题，不能提交")
            if opens:
                raise ConflictError("仍有异常未完成说明，不能提交")
            connection.execute(
                "UPDATE snapshot_versions SET state='submitted',submitted_at=? "
                "WHERE snapshot_version_id=?", (self._now(), snapshot_version_id))
            self._audit(connection, actor_id=actor_id, action="snapshot.submitted",
                        resource_type="snapshot_version", resource_id=snapshot_version_id,
                        detail={"version_no": version["version_no"]})
            return {"snapshot_version_id": snapshot_version_id, "state": "submitted"}

    def approve_snapshot(self, *, request_id: str, actor_id: str,
                         snapshot_version_id: str, decision: str,
                         comment: str = "") -> dict[str, Any]:
        """授权会签：复核人与管理员均批准（且非同一人）后自动发布。"""

        payload = {"actor_id": actor_id, "snapshot_version_id": snapshot_version_id,
                   "decision": decision, "comment": comment}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *REQUIRED_APPROVAL_ROLES)
            version = self._version_row(connection, snapshot_version_id)
            if decision not in ("approved", "rejected"):
                raise ValidationError("decision 必须是 approved 或 rejected")
            comment = str(comment or "").strip()[:500]

            def create() -> tuple[str, str, dict[str, Any]]:
                if decision == "rejected":
                    if version["state"] != domain.STATE_SUBMITTED:
                        raise ConflictError("只有待会签的快照可以驳回")
                    connection.execute(
                        "DELETE FROM snapshot_approvals WHERE snapshot_version_id=?",
                        (snapshot_version_id,))
                    connection.execute(
                        "UPDATE snapshot_versions SET state='draft' WHERE snapshot_version_id=?",
                        (snapshot_version_id,))
                    self._audit(connection, actor_id=actor_id, action="snapshot.rejected",
                                resource_type="snapshot_version",
                                resource_id=snapshot_version_id,
                                detail={"role": actor.role, "comment": comment})
                    return "snapshot_approval", f"{snapshot_version_id}:{actor.role}", \
                        {"snapshot_version_id": snapshot_version_id, "state": "draft"}

                if version["state"] != domain.STATE_SUBMITTED:
                    raise ConflictError("只有待会签的快照可以批准")
                other = connection.execute(
                    "SELECT approver_id FROM snapshot_approvals "
                    "WHERE snapshot_version_id=? AND role!=? AND decision='approved'",
                    (snapshot_version_id, actor.role),
                ).fetchone()
                if other and other["approver_id"] == actor.actor_id:
                    raise PermissionDenied("两个会签角色不能由同一人承担")
                connection.execute(
                    "INSERT INTO snapshot_approvals(snapshot_version_id,role,approver_id,"
                    "decision,comment,decided_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(snapshot_version_id,role) DO UPDATE SET "
                    "approver_id=excluded.approver_id,decision='approved',"
                    "comment=excluded.comment,decided_at=excluded.decided_at",
                    (snapshot_version_id, actor.role, actor.actor_id, "approved",
                     comment, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="snapshot.approved",
                            resource_type="snapshot_version",
                            resource_id=snapshot_version_id,
                            detail={"role": actor.role})
                approved = connection.execute(
                    "SELECT COUNT(*) AS c FROM snapshot_approvals "
                    "WHERE snapshot_version_id=? AND decision='approved'",
                    (snapshot_version_id,),
                ).fetchone()["c"]
                distinct = connection.execute(
                    "SELECT COUNT(DISTINCT approver_id) AS c FROM snapshot_approvals "
                    "WHERE snapshot_version_id=? AND decision='approved'",
                    (snapshot_version_id,),
                ).fetchone()["c"]
                if approved >= len(REQUIRED_APPROVAL_ROLES) \
                        and distinct >= len(REQUIRED_APPROVAL_ROLES):
                    self._publish(connection, version, actor_id)
                    state = "published"
                else:
                    state = "submitted"
                return "snapshot_approval", f"{snapshot_version_id}:{actor.role}", \
                    {"snapshot_version_id": snapshot_version_id, "state": state}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_snapshot", payload=payload, create=create)

    def _publish(self, connection, version, actor_id: str) -> None:
        """发布快照：结果不可变；重述版本发布时旧版本置为 superseded 并记录影响。"""

        version_id = version["snapshot_version_id"]
        impact = None
        if version["parent_version_id"]:
            parent = self._version_row(connection, version["parent_version_id"])
            if parent["state"] != domain.STATE_PUBLISHED:
                raise ConflictError("重述所基于的父版本必须保持已发布状态")
            impact = self._diff_results(parent, version)
            connection.execute(
                "UPDATE snapshot_versions SET state='superseded' "
                "WHERE snapshot_version_id=?", (parent["snapshot_version_id"],))
            self._audit(connection, actor_id=actor_id, action="snapshot.superseded",
                        resource_type="snapshot_version",
                        resource_id=parent["snapshot_version_id"],
                        detail={"replaced_by": version_id})
        connection.execute(
            "UPDATE snapshot_versions SET state='published',published_at=?,"
            "published_by=?,impact_json=? WHERE snapshot_version_id=?",
            (self._now(), actor_id,
             canonical_json(impact) if impact is not None else None, version_id))
        self._audit(connection, actor_id=actor_id, action="snapshot.published",
                    resource_type="snapshot_version", resource_id=version_id,
                    detail={"version_no": version["version_no"],
                            "parent_version_id": version["parent_version_id"],
                            "restatement_reason": version["restatement_reason"],
                            "impact": impact})

    @staticmethod
    def _diff_results(parent_row, new_row) -> dict[str, Any]:
        """对比两个冻结版本的复算结果，给出变化指标与受影响板块/品牌/地区。"""

        import json
        old = json.loads(parent_row["results_json"])
        new = json.loads(new_row["results_json"])

        def diff_metrics(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for metric in sorted(set(a) | set(b)):
                old_value = a.get(metric)
                new_value = b.get(metric)
                if old_value != new_value:
                    delta = None
                    if old_value is not None and new_value is not None:
                        delta = round(new_value - old_value, calc.ROUND_DIGITS)
                    out[metric] = {"old": old_value, "new": new_value, "delta": delta}
            return out

        totals = diff_metrics(old.get("totals", {}), new.get("totals", {}))
        segments: dict[str, Any] = {}
        affected_groups: list[str] = []
        group_changes: dict[str, Any] = {}
        old_groups = {(g["segment"], g["brand"], g["region"]): g["metrics"]
                      for g in old.get("groups", [])}
        for group in new.get("groups", []):
            key = (group["segment"], group["brand"], group["region"])
            changes = diff_metrics(old_groups.get(key, {}), group["metrics"])
            if changes:
                label = "/".join(key)
                group_changes[label] = changes
                affected_groups.append(label)
        for segment in sorted(set(old.get("segments", {})) | set(new.get("segments", {}))):
            changes = diff_metrics(old.get("segments", {}).get(segment, {}),
                                   new.get("segments", {}).get(segment, {}))
            if changes:
                segments[segment] = changes
        affected_segments = sorted(segments)
        affected_regions = sorted({key.split("/")[2] for key in affected_groups})
        affected_brands = sorted({key.split("/")[1] for key in affected_groups})
        return {"reason": new_row["restatement_reason"],
                "change_summary": new_row["change_summary"],
                "totals": totals, "segments": segments, "groups": group_changes,
                "affected_segments": affected_segments,
                "affected_brands": affected_brands,
                "affected_regions": affected_regions}

    # ------------------------------------------------------------------ 查询

    def list_snapshot_versions(self, actor_id: str, definition_version_id: str,
                               period: str | None = None) -> dict[str, Any]:
        connection = self.database.connection
        self._actor(connection, actor_id)
        sql = "SELECT * FROM snapshot_versions WHERE definition_version_id=?"
        parameters: list[Any] = [definition_version_id]
        if period:
            sql += " AND fiscal_period=?"
            parameters.append(period)
        sql += " ORDER BY fiscal_period, version_no"
        return {"items": [self._version_meta(row) for row in connection.execute(sql, parameters)]}

    def _version_meta(self, row) -> dict[str, Any]:
        import json
        return {"snapshot_version_id": row["snapshot_version_id"],
                "definition_version_id": row["definition_version_id"],
                "fiscal_period": row["fiscal_period"], "version_no": row["version_no"],
                "state": row["state"], "parent_version_id": row["parent_version_id"],
                "restatement_reason": row["restatement_reason"],
                "change_summary": row["change_summary"],
                "input_fingerprint": row["input_fingerprint"],
                "created_by": row["created_by"], "created_at": row["created_at"],
                "submitted_at": row["submitted_at"], "published_at": row["published_at"],
                "published_by": row["published_by"],
                "impact": json.loads(row["impact_json"]) if row["impact_json"] else None}

    def get_snapshot_version(self, actor_id: str, snapshot_version_id: str) -> dict[str, Any]:
        """读取版本元数据、勾稽/会签状态；结果明细通过 view_snapshot 按下钻权限返回。"""

        connection = self.database.connection
        self._actor(connection, actor_id)
        row = self._version_row(connection, snapshot_version_id)
        approvals = [
            {"role": r["role"], "approver_id": r["approver_id"], "decision": r["decision"],
             "comment": r["comment"], "decided_at": r["decided_at"]}
            for r in connection.execute(
                "SELECT * FROM snapshot_approvals WHERE snapshot_version_id=? ORDER BY role",
                (snapshot_version_id,))
        ]
        meta = self._version_meta(row)
        meta["required_approval_roles"] = list(REQUIRED_APPROVAL_ROLES)
        meta["approvals"] = approvals
        meta["issues"] = self.list_issues(connection, snapshot_version_id)
        return meta

    def view_snapshot(self, actor_id: str, snapshot_version_id: str, *,
                      segment: str | None = None, brand: str | None = None,
                      region: str | None = None) -> dict[str, Any]:
        """按板块/品牌/地区下钻快照，并从冻结输入实时复算以保证可复核。

        无权限的维度行直接剔除；受限操作者看到的合计只基于其可见事实，
        并以 scope_limited=true 标明不是全口径数字。
        """

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        version = self._version_row(connection, snapshot_version_id)
        definition = self._definition_row(connection, version["definition_version_id"])
        rates = self._rates(definition)
        period = version["fiscal_period"]
        prior_label = calc.prior_period(period)
        scopes = self._scope_map(connection, actor.actor_id)
        current_rows: list[Any] = []
        prior_rows: list[Any] = []
        hidden = 0
        bound_total = 0
        for row in connection.execute(
            "SELECT f.* FROM snapshot_fact_bindings b JOIN business_facts f "
            "ON f.fact_id=b.fact_id WHERE b.snapshot_version_id=?",
            (snapshot_version_id,)):
            bound_total += 1
            if not self._in_scope(actor, scopes, row["segment"], row["brand"], row["region"]):
                hidden += 1
                continue
            if segment and row["segment"] != segment:
                continue
            if brand and row["brand"] != brand:
                continue
            if region and row["region"] != region:
                continue
            (current_rows if row["period"] == period else prior_rows).append(row)
        result = calc.compute_result(period=period,
                                     reporting_ccy=definition["reporting_ccy"],
                                     current_rows=current_rows, prior_rows=prior_rows,
                                     rates=rates)
        _, bound, stored = self._bind_and_compute(connection, version)
        stored_fingerprint = digest([[r["fact_id"], r["payload_hash"]]
                                    for r in sorted(bound, key=lambda x: x["fact_id"])])
        return {"snapshot_version_id": snapshot_version_id,
                "state": version["state"], "version_no": version["version_no"],
                "fiscal_period": period,
                "definition_version_id": version["definition_version_id"],
                "input_fingerprint": version["input_fingerprint"],
                "fingerprint_intact": stored_fingerprint == version["input_fingerprint"],
                "scope_limited": hidden > 0 or bool(segment or brand or region),
                "hidden_fact_count": hidden,
                "bound_fact_count": bound_total,
                "drill_down": {"segment": segment, "brand": brand, "region": region},
                "result": result,
                "stored_result": stored if actor.role == "admin" and not (segment or brand or region) else None}

    def verify_snapshot(self, actor_id: str, snapshot_version_id: str) -> dict[str, Any]:
        """用绑定的冻结事实重新复算，核对指纹与发布结果是否一致。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "admin", "reviewer", "auditor")
        version = self._version_row(connection, snapshot_version_id)
        fingerprint, _bound, recomputed = self._bind_and_compute(connection, version)
        import json
        stored = json.loads(version["results_json"]) if version["results_json"] else None
        return {"snapshot_version_id": snapshot_version_id,
                "state": version["state"],
                "input_fingerprint": version["input_fingerprint"],
                "fingerprint_match": fingerprint == version["input_fingerprint"],
                "results_match": stored == recomputed}

    def verify_audit(self) -> tuple[bool, int]:
        return verify_chain(self.database.connection)
