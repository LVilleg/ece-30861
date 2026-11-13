"""Integration regressions for job isolation and the course HTTP lifecycle."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import Flask, jsonify, request
from werkzeug.serving import WSGIRequestHandler, make_server

import cross_team
from cross_team import TargetClient, create_tester_blueprint
from cross_team_cases import SURVEY_VECTORS, image_file
from state import initialize
import system_api
from system_api import create_system_blueprint


class QuietHandler(WSGIRequestHandler):
    def log(self, log_type, message, *args):
        pass


class CrossTeamTests(unittest.TestCase):
    def serve(self, app):
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "state.sqlite3"
        initialize(database)

        self.app = Flask(__name__)
        self.app.register_blueprint(create_system_blueprint(database), url_prefix="/pod/system")
        self.app.register_blueprint(create_tester_blueprint(database), url_prefix="/tester")
        self.client = self.app.test_client()
        self.target = self.serve(self.app) + "/pod/system"
        self.app.config["TESTER_APPROVED_TARGETS"] = {self.target}

    def setup_tester(self, target=None):
        response = self.client.post("/tester/test/setup", json={"target_url": target or self.target})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["session_id"]

    def run_tester(self, session_id):
        return self.client.post("/tester/test/run", json={"session_id": session_id})

    def report(self, session_id):
        return self.client.get("/tester/test/report", query_string={"session_id": session_id})

    def test_real_http_round_trip_and_report(self):
        session = self.setup_tester(self.target + "/")
        self.assertNotEqual(session, self.setup_tester())
        self.assertEqual(self.report(session).status_code, 409)
        self.assertEqual(self.run_tester("unknown").status_code, 404)
        self.assertEqual(self.run_tester("").status_code, 400)

        response = self.run_tester(session)
        self.assertEqual(response.status_code, 200)
        report = response.get_json()
        self.assertEqual(report["session_id"], session)
        self.assertEqual(report["target_url"], self.target)
        self.assertEqual([item["test_id"] for item in report["results"]],
                         [test_id for test_id, _ in cross_team.CASES])
        self.assertTrue(all(item["outcome"] == "pass" for item in report["results"]), report["results"])
        self.assertIsNone(report["coverage"]["code"]["covered"])
        self.assertEqual(self.report(session).get_json(), report)
        self.assertEqual(self.run_tester(session).status_code, 409)
        self.assertEqual(self.report(session).get_json(), report)

    def test_rejected_targets_and_forwarded_setup_failure(self):
        for url in ("file:///local", self.target + "/test", self.target + "?x=1"):
            response = self.client.post("/tester/test/setup", json={"target_url": url})
            self.assertEqual(response.status_code, 400)
        response = self.client.post("/tester/test/setup", json={"target_url": "http://localhost:1/system"})
        self.assertEqual(response.status_code, 403)

        mock = Flask("setup-failure")
        ids = []
        failure = {"source": "system", "message": "The target is busy.", "code": "busy"}

        @mock.post("/test/setup")
        def fail_setup():
            body = request.get_json()
            ids.append(body["session_id"])
            self.assertEqual(body["setup"], {})
            return jsonify(failure), 409

        target = self.serve(mock)
        self.app.config["TESTER_APPROVED_TARGETS"].add(target)
        response = self.client.post("/tester/test/setup", json={"target_url": target})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json(), failure)
        self.assertEqual(self.run_tester(ids[0]).status_code, 404)

    def test_concurrent_run_and_abort_report(self):
        session = self.setup_tester()
        entered, release = Event(), Event()

        def pass_case(_client):
            return "First case passed."

        def abort_case(_client):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test harness did not release the case.")
            raise RuntimeError("Simulated tester failure.")

        cases = (("first", pass_case), ("second", abort_case), ("third", pass_case))
        with patch.object(cross_team, "CASES", cases), ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(self.run_tester, session)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.run_tester(session).status_code, 409)
                self.assertEqual(self.report(session).status_code, 409)
            finally:
                release.set()
            self.assertEqual(result.result(timeout=10).status_code, 500)

        report = self.report(session).get_json()
        self.assertEqual([item["outcome"] for item in report["results"]], ["pass", "error", "skip"])
        self.assertEqual(report["results"][1]["source"], "tester")
        self.assertEqual(self.run_tester(session).status_code, 409)

    def test_target_failures_and_coverage_remain_in_completed_report(self):
        mock = Flask("target-errors")
        calls = []

        @mock.post("/pod/test/setup")
        def setup():
            return jsonify(session_id=request.get_json()["session_id"]), 201

        @mock.get("/pod/health")
        def health():
            calls.append("health")
            return jsonify(code="internal_error", message="Simulated target failure."), 500

        @mock.get("/pod/test/report")
        def coverage():
            calls.append("coverage")
            return jsonify(session_id=request.args["session_id"], coverage={"code": {
                "covered": 2, "total": 3, "basis": "Project source lines."
            }})

        target = self.serve(mock) + "/pod"
        self.app.config["TESTER_APPROVED_TARGETS"].add(target)
        session = self.setup_tester(target)

        def simulated_timeout(_client):
            raise cross_team.CommunicationError("Simulated timeout.")

        cases = (("system-failure", cross_team.CASES[0][1]), ("timeout", simulated_timeout))
        with patch.object(cross_team, "CASES", cases):
            response = self.run_tester(session)
        self.assertEqual(response.status_code, 200)
        report = response.get_json()
        self.assertEqual(calls, ["health", "coverage"])
        self.assertEqual([item["source"] for item in report["results"]], ["system", "communication"])
        self.assertTrue(all(item["outcome"] == "error" for item in report["results"]))
        self.assertEqual(report["coverage"]["code"]["covered"], 2)
        self.assertEqual(self.report(session).get_json(), report)

    def test_job_pending_result_and_session_isolation(self):
        session = self.setup_tester()
        other = self.setup_tester()
        client = TargetClient(self.target, session, 10)
        other_client = TargetClient(self.target, other, 10)
        image_format, status, tools, vector = SURVEY_VECTORS[0]
        fields = [("ai_status", status), *(("ai_tools", tool) for tool in tools)]
        with patch.object(system_api._WorkQueue, "kick"):
            response = client.request("POST", "/watermark-jobs", fields=fields, file=image_file(image_format))
            self.assertEqual(response.status_code, 202)
            path = client.link_path(response.headers["Location"]) + "/result"
            self.assertEqual(client.request("GET", path).status_code, 409)
            self.assertEqual(other_client.request("GET", path).status_code, 404)

        deadline = time.monotonic() + 5
        while True:
            result = client.request("GET", path)
            if result.status_code == 200:
                break
            self.assertEqual(result.status_code, 409)
            self.assertLess(time.monotonic(), deadline)
            client.pause(0.05)
        detected = client.request("POST", "/detect", file=("result.png", result.body, "image/png"))
        self.assertEqual(detected.json()["payload"], vector)

    def test_scaled_project_file_limit_on_both_upload_routes(self):
        _, image, _ = image_file("PNG")
        for route in ("/pod/system/watermark-jobs", "/pod/system/detect"):
            with self.subTest(route=route), patch.object(system_api, "MAX_FILE_SIZE", len(image) - 1):
                result = self.client.post(route, data={"file": (BytesIO(image), "over.png")})
                self.assertEqual(result.status_code, 413)
                self.assertEqual(result.get_json()["code"], "payload_too_large")
        with patch.object(system_api, "MAX_FILE_SIZE", len(image)):
            result = self.client.post("/pod/system/detect", data={"file": (BytesIO(image), "edge.png")})
            self.assertEqual(result.status_code, 200)

    def test_system_session_errors_and_job_link_resolution(self):
        session = str(uuid4())
        setup = {"session_id": session, "setup": {}}
        path = "/pod/system/test/setup"
        self.assertEqual(self.client.post(path, json=setup).status_code, 201)
        self.assertEqual(self.client.post(path, json=setup).status_code, 409)
        report = self.client.get("/pod/system/test/report", query_string={"session_id": session})
        self.assertEqual(report.get_json()["session_id"], session)
        client = TargetClient(self.target, session, 10)
        self.assertEqual(client.link_path("/watermark-jobs/a"), "/watermark-jobs/a")
        self.assertEqual(client.link_path(self.target + "/watermark-jobs/a"), "/watermark-jobs/a")
        for link in ("//other.example/watermark-jobs/a", "/watermark-jobs/%2e%2e/test/setup"):
            with self.subTest(link=link), self.assertRaises(AssertionError):
                client.link_path(link)


if __name__ == "__main__":
    unittest.main()
