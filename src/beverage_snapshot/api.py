"""组合经营快照服务的 HTTP/JSON 边界（仅依赖标准库）。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from beverage_ops_foundation.errors import DomainError

from .service import SnapshotService
from .storage import SnapshotDatabase


def route(service: SnapshotService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把 HTTP 语义请求分派到快照领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}

        if method == "POST" and parsed.path == "/data-scopes":
            return 201, service.grant_data_scope(actor_id=actor_id, **body)

        if method == "POST" and parsed.path == "/definition-versions":
            receipt = service.create_definition_version(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and parsed.path == "/definition-versions/freeze":
            receipt = service.freeze_definition_version(actor_id=actor_id, **body)
            return 200, receipt

        if method == "POST" and parsed.path == "/facts":
            receipt = service.record_fact(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "GET" and parsed.path == "/facts":
            definition_version_id = q("definition_version_id", "")
            if not definition_version_id:
                return 400, {"error": "validation_error",
                             "message": "definition_version_id 不能为空"}
            return 200, service.list_facts(
                actor_id, definition_version_id, batch_id=q("batch_id"),
                segment=q("segment"), brand=q("brand"), region=q("region"), period=q("period"))

        if method == "POST" and parsed.path == "/snapshots":
            receipt = service.create_snapshot(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and parsed.path == "/snapshots/restate":
            receipt = service.create_restatement(actor_id=actor_id, **body)
            return 200 if receipt["replayed"] else 201, receipt
        if method == "POST" and parsed.path == "/snapshots/reconcile":
            return 200, service.reconcile(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/snapshots/submit":
            return 200, service.submit_snapshot(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/snapshots/approve":
            receipt = service.approve_snapshot(actor_id=actor_id, **body)
            return 200, receipt
        if method == "POST" and parsed.path == "/issues/explain":
            receipt = service.explain_issue(actor_id=actor_id, **body)
            return 200, receipt

        if method == "GET" and parsed.path == "/snapshot-versions":
            definition_version_id = q("definition_version_id", "")
            if not definition_version_id:
                return 400, {"error": "validation_error",
                             "message": "definition_version_id 不能为空"}
            return 200, service.list_snapshot_versions(
                actor_id, definition_version_id, period=q("period"))
        if method == "GET" and parsed.path == "/snapshot-version":
            snapshot_version_id = q("id", "")
            if not snapshot_version_id:
                return 400, {"error": "validation_error", "message": "id 不能为空"}
            return 200, service.get_snapshot_version(actor_id, snapshot_version_id)
        if method == "GET" and parsed.path == "/snapshot-view":
            snapshot_version_id = q("id", "")
            if not snapshot_version_id:
                return 400, {"error": "validation_error", "message": "id 不能为空"}
            return 200, service.view_snapshot(
                actor_id, snapshot_version_id, segment=q("segment"),
                brand=q("brand"), region=q("region"))
        if method == "GET" and parsed.path == "/snapshot-verify":
            snapshot_version_id = q("id", "")
            if not snapshot_version_id:
                return 400, {"error": "validation_error", "message": "id 不能为空"}
            return 200, service.verify_snapshot(actor_id, snapshot_version_id)

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: SnapshotService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动组合经营快照 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动组合经营快照服务")
    parser.add_argument("--database", default="snapshot.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = SnapshotDatabase(args.database)
    Handler.service = SnapshotService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
