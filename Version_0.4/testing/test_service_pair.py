"""Run the tester against an independently served system using real HTTP."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx

from system_service.api import create_app as create_system
from tester_service.api import create_app as create_tester
from tester_service.cases import CASES
from testing.http_server import serve


class ServicePairTests(unittest.TestCase):
    def test_setup_run_and_retrieve_across_two_origins(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with serve(create_system(root / "system.sqlite3")) as target:
                tester = create_tester(root / "tester.sqlite3", approved_targets=[target], own_targets=[])
                with serve(tester) as tester_url, httpx.Client(base_url=tester_url, timeout=60, trust_env=False) as client:
                    setup = client.post("/test/setup", json={"target_url": target})
                    self.assertEqual(setup.status_code, 201, setup.text)
                    session = setup.json()
                    self.assertEqual(client.get("/test/report", params=session).status_code, 409)
                    run = client.post("/test/run", json=session)
                    self.assertEqual(run.status_code, 200, run.text)
                    report = run.json()
                    self.assertEqual(report["target_url"], target)
                    self.assertEqual(report["session_id"], session["session_id"])
                    self.assertEqual([r["test_id"] for r in report["results"]], [name for name, _ in CASES])
                    self.assertEqual({r["outcome"] for r in report["results"]}, {"pass"}, report["results"])
                    self.assertEqual(set(report["coverage"]), {"code", "component", "system"})
                    self.assertEqual(client.get("/test/report", params=session).json(), report)
