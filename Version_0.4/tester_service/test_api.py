"""Regression coverage for the JSON tester lifecycle, ownership and crash checkpoints."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from state import connect
from .api import create_app
from .client import CommunicationError, TargetClient, TargetError, TargetResponse


SELF = "http://localhost:8000"
PEER = "https://peer.example/pod/system"


def reply(status, payload):
    return TargetResponse(status, {}, json.dumps(payload).encode())


def success(_client):
    return "The project call matched."


class TesterLifecycle(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "tester.sqlite3"
        self.calls = []
        self.setup_error = None

        def request(client, method, path, **kwargs):
            self.calls.append((client.target, method, path, kwargs))
            if path == "/test/setup":
                self.assertEqual(kwargs["json_body"], {"session_id": client.session_id, "setup": {}})
                if self.setup_error is not None:
                    return self.setup_error
                return reply(201, {"session_id": client.session_id})
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/test/report?session_id=" + client.session_id)
            return reply(200, {"session_id": client.session_id, "coverage": {"code": {
                "covered": 5, "total": 8, "basis": "Source lines in the mock target."}}})

        patcher = patch.object(TargetClient, "request", request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, cases=(("first", success),)):
        return TestClient(create_app(self.database, [SELF, PEER], [SELF], cases))

    def setup_id(self, client, target=PEER):
        response = client.post("/test/setup", json={"target_url": target})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["session_id"]

    def report(self, client, session):
        return client.get("/test/report", params={"session_id": session})

    def test_pass_fail_system_and_transport_errors_do_not_abort(self):
        def mismatch(_):
            raise AssertionError("Unexpected payload.")

        def target_error(_):
            raise TargetError("System returned 500.")

        def network_error(_):
            raise CommunicationError("No response.")

        cases = (("matches", success), ("mismatch", mismatch), ("target", target_error), ("network", network_error))
        client = self.client(cases)
        for target in (SELF, PEER):
            with self.subTest(target=target):
                session = self.setup_id(client, target)
                self.assertEqual(self.report(client, session).status_code, 409)
                response = client.post("/test/run", json={"session_id": session})
                self.assertEqual(response.status_code, 200, response.text)
                report = response.json()
                self.assertEqual(report["target_url"], target)
                self.assertEqual([r["test_id"] for r in report["results"]], [case[0] for case in cases])
                self.assertEqual([r["outcome"] for r in report["results"]], ["pass", "fail", "error", "error"])
                self.assertEqual([r["source"] for r in report["results"][2:]], ["system", "communication"])
                self.assertEqual(report["coverage"]["code"]["covered"], 5)
                self.assertEqual("model" in report["coverage"], target == SELF)
                self.assertEqual(self.report(client, session).json(), report)
                self.assertEqual(client.post("/test/run", json={"session_id": session}).status_code, 409)
                self.assertEqual(self.calls[-1][0], target)

    def test_forwarded_setup_failure_and_mismatched_id(self):
        client = self.client()
        error = {"source": "system", "message": "System is busy.", "code": "busy"}
        for remote, status, expected in [(reply(409, error), 409, error), (reply(201, {"session_id": "wrong"}), 500, None)]:
            self.setup_error = remote
            response = client.post("/test/setup", json={"target_url": PEER})
            self.assertEqual(response.status_code, status)
            if expected:
                self.assertEqual(response.json(), expected)
            session = self.calls[-1][3]["json_body"]["session_id"]
            self.assertEqual(client.post("/test/run", json={"session_id": session}).status_code, 404)

    def test_abort_and_concurrent_requests(self):
        entered, release = Event(), Event()

        def fail_tester(_):
            entered.set()
            release.wait(5)
            raise RuntimeError("Simulated tester abort.")

        client = self.client((("first", success), ("broken", fail_tester), ("last", success)))
        session = self.setup_id(client)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, "/test/run", json={"session_id": session})
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(client.post("/test/run", json={"session_id": session}).status_code, 409)
                self.assertEqual(self.report(client, session).status_code, 409)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).status_code, 500)
        results = self.report(self.client(), session).json()["results"]
        self.assertEqual([item["outcome"] for item in results], ["pass", "error", "skip"])
        self.assertEqual(results[1]["source"], "tester")

    def test_interrupted_process_report_available_after_lease_expiration(self):
        def crash(_):
            raise SystemExit("Simulated worker exit.")

        client = self.client((("first", success), ("interrupted", crash), ("last", success)))
        session = self.setup_id(client)
        # A BaseException at process exit bypasses the endpoint's exception handler.
        endpoint = next(r.endpoint for r in client.app.routes if r.path == "/test/run")
        with self.assertRaises(SystemExit):
            endpoint({"session_id": session})
        with connect(self.database) as conn:
            conn.execute("UPDATE contract_tester_sessions SET expires_at=0 WHERE id=?", (session,))
        recovered = self.report(self.client(), session)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual([r["outcome"] for r in recovered.json()["results"]], ["pass", "error", "skip"])
        self.assertEqual(self.report(client, session).json(), recovered.json())
        self.assertEqual(client.post("/test/run", json={"session_id": session}).status_code, 409)

    def test_served_openapi_includes_lifecycle_contract(self):
        spec = self.client().get("/openapi.json").json()
        self.assertEqual(spec["openapi"], "3.1.1")
        self.assertEqual(set(spec["paths"]), {"/test/setup", "/test/run", "/test/report"})
        self.assertEqual(spec["components"]["schemas"]["Error"]["required"], ["message", "source"])
        self.assertIn("model", spec["components"]["schemas"]["Report"]["properties"]["coverage"]["properties"])
        for path, method in [("/test/setup", "post"), ("/test/run", "post"), ("/test/report", "get")]:
            operation = spec["paths"][path][method]
            self.assertIn("default", operation["responses"])
            if method == "post":
                self.assertIn("application/json", operation["requestBody"]["content"])
            else:
                self.assertEqual(operation["parameters"][0]["name"], "session_id")

    def test_bad_input_does_not_contact_target(self):
        client = self.client()
        for payload in (None, [], {"target_url": PEER + "/test"}, {"target_url": PEER + "?bad=1"}):
            self.assertEqual(client.post("/test/setup", json=payload).status_code, 400)
        self.assertEqual(client.post("/test/setup", json={"target_url": "https://elsewhere.example"}).status_code, 403)
        self.assertEqual(client.post("/test/setup", content=b"{" * 65536,
                                     headers={"Content-Type": "application/json"}).status_code, 400)
        self.assertEqual(client.get("/test/report?session_id=a&session_id=b").status_code, 400)
        self.assertEqual(self.calls, [])
