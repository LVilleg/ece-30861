"""HTTP regressions for streamed upload limits, job links and session isolation."""

from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi import FastAPI
from PIL import Image

from system_service import api
from testing.http_server import serve


class SystemHttpTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.system = api.create_app(Path(directory.name) / "system.sqlite3")
        mounted = FastAPI()
        mounted.mount("/pod/system", self.system)
        self.port = urlsplit(self.enterContext(serve(mounted))).port
        image = BytesIO()
        Image.new("RGB", (12, 12), "navy").save(image, format="PNG")
        self.image = image.getvalue()

    def request(self, method, path, body=None, headers=None, *, chunked=False):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, "/pod/system" + path, body, headers or {}, encode_chunked=chunked)
            response = connection.getresponse()
            content = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                content = json.loads(content)
            return response.status, dict(response.getheaders()), content
        finally:
            connection.close()

    def upload(self, path, data, filename="test.png", *, fields=(), chunked=False, headers=None):
        boundary = "boundary-media-test"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n'
        ).encode() + data + b"\r\n"
        for name, value in fields:
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f'{value}\r\n').encode()
        body += f"--{boundary}--\r\n".encode()
        headers = {"Content-Type": "multipart/form-data; boundary=" + boundary, **(headers or {})}
        return self.request("POST", path, iter([body[:100], body[100:]]) if chunked else body,
                            headers, chunked=chunked)

    def test_upload_limit_checks_actual_bytes_on_both_routes(self):
        for path in ("/watermark-jobs", "/detect"):
            for chunked in (True, False):
                with self.subTest(path=path, chunked=chunked), patch.object(api, "MAX_FILE_SIZE", len(self.image)):
                    status, _, _ = self.upload(path, self.image, chunked=chunked)
                    self.assertEqual(status, 202 if path == "/watermark-jobs" else 200)
                    status, _, body = self.upload(path, self.image + b"x", chunked=chunked)
                    self.assertEqual((status, body["code"]), (413, "payload_too_large"))
                    with patch.object(api, "MULTIPART_ALLOWANCE", 4):
                        status, _, body = self.upload(path, self.image, chunked=chunked)
                        self.assertEqual((status, body["code"]), (413, "payload_too_large"))

    def test_served_schema_describes_the_mounted_contract(self):
        status, _, schema = self.request("GET", "/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(schema["components"]["schemas"]["WatermarkRequest"]["required"], ["file"])
        self.assertIn("Error", schema["components"]["responses"])
        self.assertEqual(schema["paths"]["/test/setup"]["post"]["responses"]["201"]["description"], "Ready")
        self.assertIn(schema["servers"][0]["url"], (".", "/pod/system"))
        header = schema["components"]["parameters"]["TestSessionId"]
        self.assertEqual((header["in"], header["name"]), ("header", "X-Test-Session-Id"))
        session_ref = {"$ref": "#/components/parameters/TestSessionId"}
        for path, methods in schema["paths"].items():
            if not path.startswith("/test/"):
                for operation in methods.values():
                    self.assertIn(session_ref, operation["parameters"])

    def test_invalid_media_and_payload(self):
        for path in ("/watermark-jobs", "/detect"):
            status, _, body = self.upload(path, b"unrecognized", "test.gif")
            self.assertEqual((status, body["code"]), (415, "unsupported_media_type"))
            status, _, body = self.upload(path, b"damaged", "test.png")
            self.assertEqual((status, body["code"]), (400, "bad_request"))
        for fields in ([("payload", "x" * 65)], [("payload", "a"), ("payload", "b")]):
            self.assertEqual(self.upload("/watermark-jobs", self.image, fields=fields)[0], 400)

    def test_scoped_job_links_pending_result_and_roundtrip(self):
        session = {"X-Test-Session-Id": "http-system-test"}
        request = json.dumps({"session_id": session["X-Test-Session-Id"], "setup": {}})
        headers = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/test/setup", request, headers)[0], 201)
        status, _, error = self.request("POST", "/test/setup", request, headers)
        self.assertEqual((status, error["source"]), (409, "system"))
        with patch.object(self.system.state.jobs, "wake"):
            status, headers, job = self.upload("/watermark-jobs", self.image,
                                               fields=[("payload", "team")], headers=session)
            self.assertEqual(status, 202)
            location = "/watermark-jobs/" + job["id"]
            self.assertEqual(headers["location"], "/pod/system" + location)
            self.assertEqual(self.request("GET", location + "/result", headers=session)[0], 409)
            self.assertEqual(self.request("GET", location)[0], 404)
        deadline = monotonic() + 10
        while True:
            status, _, job = self.request("GET", location, headers=session)
            self.assertEqual(status, 200)
            if job["status"] == "completed":
                break
            self.assertIn(job["status"], ("queued", "processing"))
            self.assertLess(monotonic(), deadline)
            sleep(0.03)
        self.assertEqual(job["resultUrl"], "/pod/system" + location + "/result")
        status, headers, marked = self.request("GET", location + "/result", headers=session)
        self.assertEqual((status, headers["content-type"]), (200, "image/png"))
        status, _, result = self.upload("/detect", marked, headers=session)
        self.assertEqual((status, result["detected"], result["payload"]), (200, True, "team"))
        status, _, report = self.request("GET", "/test/report?session_id=http-system-test")
        self.assertEqual(status, 200)
        self.assertIsNone(report["coverage"]["code"]["covered"])
