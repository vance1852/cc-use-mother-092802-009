"""组合经营快照服务的 HTTP/JSON 路由。

与基础包共用同一个 SQLite 数据库和哈希审计链；路径统一以 /snapshot 开头。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from ..errors import DomainError
from .service import SnapshotService


def route_snapshot(service: SnapshotService, method: str, path: str, body: dict[str, Any],
                   headers: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
    """处理 /snapshot 路径；不属于快照服务时返回 None。"""

    parsed = urlparse(path)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or segments[0] != "snapshot":
        return None
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def single(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        if method == "POST" and segments == ["snapshot", "definitions"]:
            result = service.register_metric_definition(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "GET" and segments == ["snapshot", "definitions"]:
            version_no = single("version_no")
            return 200, service.get_definition(single("definition_id", ""),
                                               int(version_no) if version_no else None,
                                               actor_id=actor_id)

        if method == "POST" and segments == ["snapshot", "scopes"]:
            result = service.register_scope(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "GET" and segments == ["snapshot", "scopes"]:
            version_no = single("version_no")
            return 200, service.get_scope(single("scope_id", ""),
                                          int(version_no) if version_no else None,
                                          actor_id=actor_id)

        if method == "POST" and segments == ["snapshot", "grants"]:
            result = service.grant_node_access(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result

        if method == "POST" and segments == ["snapshot", "facts"]:
            result = service.record_fact(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result

        if method == "POST" and segments == ["snapshot", "versions"]:
            result = service.build_version(actor_id=actor_id, **body)
            return 200 if result.get("replayed") else 201, result
        if method == "GET" and segments == ["snapshot", "versions"]:
            return 200, service.list_versions(
                actor_id, scope_id=single("scope_id"), period_code=single("period_code"),
                batch_id=single("batch_id"), status=single("status"))

        if len(segments) == 3 and segments[:2] == ["snapshot", "versions"]:
            version_id = segments[2]
            if method == "GET":
                return 200, service.get_version(actor_id, version_id)
        if len(segments) == 4 and segments[:2] == ["snapshot", "versions"]:
            version_id, action = segments[2], segments[3]
            if method == "GET" and action == "recompute":
                return 200, service.recompute_version(actor_id, version_id)
            if method == "GET" and action == "findings":
                return 200, service.list_findings(actor_id, version_id)
            if method == "GET" and action == "data":
                return 200, service.query_snapshot(
                    actor_id, version_id, brand=single("brand"), segment=single("segment"),
                    region=single("region"), node_id=single("node_id"), drill=single("drill"))
            if method == "POST" and action == "rebuild":
                result = service.rebuild_version(actor_id=actor_id, version_id=version_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and action == "acknowledge":
                result = service.acknowledge_findings(actor_id=actor_id, version_id=version_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and action == "submit":
                result = service.submit_version(actor_id=actor_id, version_id=version_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and action == "signoff":
                result = service.sign_off(actor_id=actor_id, version_id=version_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and action == "publish":
                result = service.sanction_publication(actor_id=actor_id, version_id=version_id, **body)
                return 200 if result.get("replayed") else 201, result
            if method == "POST" and action == "cancel":
                result = service.cancel_version(actor_id=actor_id, version_id=version_id, **body)
                return 200 if result.get("replayed") else 201, result

        return 404, {"error": "route_not_found", "message": "快照接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
