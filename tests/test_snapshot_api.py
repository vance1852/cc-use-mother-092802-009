import unittest

from beverage_ops_foundation.api import route
from beverage_ops_foundation.service import DomainService
from beverage_ops_foundation.snapshot.service import SnapshotService
from beverage_ops_foundation.storage import Database

DEFINITION = {
    "presentation_currency": "CNY",
    "metrics": [
        {"metric_id": "revenue", "kind": "sum", "measure": "revenue"},
        {"metric_id": "premium_share", "kind": "ratio",
         "numerator_measure": "premium_revenue", "denominator_measure": "revenue"},
    ],
    "checks": [],
    "required_signoff_roles": ["admin"],
}
NODES = [
    {"node_id": "g1", "node_type": "group", "name": "集团"},
    {"node_id": "beer", "parent_id": "g1", "node_type": "segment", "name": "啤酒",
     "segment": "beer", "brand_id": "snow", "region": "east"},
]
CURRENCIES = {"rates": {"CNY": "1"}}


class SnapshotApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.snapshot = SnapshotService(self.database)
        self.headers_admin = {"X-Actor-Id": "a1"}
        self.headers_op = {"X-Actor-Id": "op1"}
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="集团")
        self.service.register_actor(request_id="adm", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                    display_name="办", role="operator", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, headers=None):
        return route(self.service, method, path, body or {}, headers or {}, self.snapshot)

    def test_full_flow_over_http(self):
        status, payload = self.call("POST", "/snapshot/definitions", {
            "request_id": "def", "definition_id": "d1", "body": DEFINITION},
            self.headers_admin)
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/snapshot/scopes", {
            "request_id": "sc", "scope_id": "s1", "nodes": NODES,
            "currencies": CURRENCIES}, self.headers_admin)
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/snapshot/facts", {
            "request_id": "f1", "fact_id": "fact-1", "node_id": "beer",
            "batch_id": "batch-1", "source": "erp", "period_code": "2026H1",
            "currency_code": "CNY",
            "measures": {"revenue": 1000, "premium_revenue": 400}}, self.headers_op)
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/snapshot/versions", {
            "request_id": "build", "scope_id": "s1",
            "definition_id": "d1", "period_code": "2026H1"}, self.headers_op)
        self.assertEqual(201, status)
        version_id = payload["version_id"]

        status, payload = self.call("POST", f"/snapshot/versions/{version_id}/submit",
                                    {"request_id": "sub"}, self.headers_op)
        self.assertEqual(201, status)

        status, payload = self.call("POST", f"/snapshot/versions/{version_id}/signoff",
                                    {"request_id": "sig", "decision": "approved"},
                                    self.headers_admin)
        self.assertEqual(201, status)

        status, payload = self.call("POST", f"/snapshot/versions/{version_id}/publish",
                                    {"request_id": "pub"}, self.headers_admin)
        self.assertEqual(201, status)
        self.assertEqual("published", payload["status"])

        status, payload = self.call("GET", f"/snapshot/versions/{version_id}/recompute",
                                    None, self.headers_admin)
        self.assertEqual(200, status)
        self.assertTrue(payload["matches"])

        status, payload = self.call("GET", f"/snapshot/versions/{version_id}/data?drill=brand",
                                    None, self.headers_admin)
        self.assertEqual(200, status)
        self.assertEqual("snow", payload["drill"][0]["brand"])

        status, payload = self.call("GET", "/snapshot/versions?period_code=2026H1",
                                    None, self.headers_admin)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))

    def test_snapshot_route_without_actor_is_denied(self):
        status, payload = self.call("POST", "/snapshot/definitions", {
            "request_id": "d", "definition_id": "d1", "body": DEFINITION})
        self.assertEqual(404, status)  # 操作者不存在

    def test_snapshot_validation_error(self):
        self.call("POST", "/snapshot/definitions", {
            "request_id": "def", "definition_id": "d1", "body": DEFINITION},
            self.headers_admin)
        status, payload = self.call("POST", "/snapshot/versions", {
            "request_id": "build", "scope_id": "missing",
            "definition_id": "d1", "period_code": "2026H1"}, self.headers_admin)
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_unknown_snapshot_route(self):
        status, payload = self.call("GET", "/snapshot/missing", None, self.headers_admin)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
