"""Bounded HTTP calls, URL resolution and target coverage validation."""

from dataclasses import dataclass
from email.message import Message
from http.client import HTTPException
import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


ERROR_SOURCES = {"tester", "system", "communication", "unknown"}
DEFAULT_TARGETS = ("http://127.0.0.1:8000/system", "http://localhost:8000/system")
SETUP_SECONDS = 10
RUN_SECONDS = 45
CALL_SECONDS = 5
RESPONSE_LIMIT = 102 * 1024 * 1024


def valid_text(value, maximum=200):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def base_url(value):
    if not valid_text(value, 2048) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("Supply an HTTP(S) system base URL.")
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.port == 0
        or url.username is not None
        or "?" in value
        or "#" in value
        or "\\" in unquote(url.path)
        or any(part in {".", ".."} for part in unquote(url.path).split("/"))
        or url.path.rstrip("/").endswith("/test")
    ):
        raise ValueError("Supply a system base URL without credentials, a /test suffix, query, or fragment.")
    return url.geturl().rstrip("/")


class CommunicationError(Exception):
    pass


class TargetError(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class TargetResponse:
    status_code: int
    headers: Message
    body: bytes

    def json(self):
        return json.loads(self.body)


def multipart(fields, file):
    boundary = uuid4().hex
    data = bytearray()

    def add(name, contents, filename=None, mime=None):
        data.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"".encode())
        if filename is not None:
            data.extend(f"; filename=\"{filename}\"\r\nContent-Type: {mime}".encode())
        data.extend(b"\r\n\r\n")
        data.extend(contents)
        data.extend(b"\r\n")

    for name, value in fields:
        add(name, value.encode())
    if file is not None:
        filename, contents, mime = file
        add("file", contents, filename, mime)
    data.extend(f"--{boundary}--\r\n".encode())
    return bytes(data), f"multipart/form-data; boundary={boundary}"


class TargetClient:
    def __init__(self, target, session_id, duration):
        self.target = target
        self.session_id = session_id
        self.deadline = time.monotonic() + duration
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def pause(self, seconds):
        time.sleep(min(seconds, self.remaining()))

    def link_path(self, link):
        if not valid_text(link, 2048) or any(ord(char) <= 32 or ord(char) == 127 for char in link):
            raise AssertionError("The target returned a malformed job URL.")
        try:
            reference = urlsplit(link)
        except ValueError as exc:
            raise AssertionError("The target returned a malformed job URL.") from exc
        base = urlsplit(self.target)
        path = reference.path
        decoded = unquote(path)
        if (
            "?" in link or "#" in link or "\\" in decoded or decoded.startswith("//")
            or any(part in {".", ".."} for part in decoded.split("/"))
            or ((reference.scheme or reference.netloc)
                and (reference.scheme, reference.netloc.lower()) != (base.scheme, base.netloc.lower()))
        ):
            raise AssertionError("The target returned a job URL outside the configured system base.")
        if base.path and path.startswith(base.path + "/"):
            path = path[len(base.path):]
        path = "/" + path.lstrip("/")
        if not path.startswith("/watermark-jobs/"):
            raise AssertionError("The target returned a URL outside its job API.")
        return path

    def request(self, method, path, *, fields=(), file=None, json_body=None, evaluate=True):
        if not self.remaining():
            raise CommunicationError("The target call budget expired.")
        headers = {"X-Test-Session-Id": self.session_id}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        elif fields or file is not None:
            data, headers["Content-Type"] = multipart(fields, file)
        call = Request(self.target + path, data=data, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(call, timeout=min(CALL_SECONDS, self.remaining()))
            except HTTPError as exc:
                response = exc
            with response:
                body = bytearray()
                while self.remaining():
                    chunk = response.read1(min(64 * 1024, RESPONSE_LIMIT + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > RESPONSE_LIMIT:
                        raise TargetError("The target response exceeded the tester's response limit.")
                else:
                    raise CommunicationError("The target response exceeded the time budget.")
                result = TargetResponse(response.code, response.headers, bytes(body))
        except (URLError, socket.timeout, OSError, HTTPException) as exc:
            raise CommunicationError(f"No complete response for {method} {path}.") from exc
        length = result.headers.get("Content-Length")
        if length and length.isdecimal() and len(result.body) < int(length):
            raise CommunicationError(f"The target returned an incomplete response for {method} {path}.")
        if evaluate and result.status_code >= 500:
            raise TargetError(f"The target returned HTTP {result.status_code} for {method} {path}.")
        return result


def unavailable(basis):
    return {"covered": None, "total": None, "basis": basis}


def code_coverage(client):
    path = "/test/report?" + urlencode({"session_id": client.session_id})
    try:
        response = client.request("GET", path)
        if response.status_code != 200:
            return unavailable(f"Target coverage report returned HTTP {response.status_code}.")
        report = response.json()
        code = report["coverage"]["code"]
        covered, total, basis = code["covered"], code["total"], code["basis"]
        valid_counts = (covered is None and total is None) or (
            type(covered) is int and type(total) is int and 0 <= covered <= total
        )
        if report["session_id"] == client.session_id and valid_counts and valid_text(basis, 4096):
            return {"covered": covered, "total": total, "basis": basis}
    except (CommunicationError, TargetError) as exc:
        return unavailable(str(exc))
    except (KeyError, TypeError, ValueError):
        pass
    return unavailable("The target returned an invalid code coverage report.")
