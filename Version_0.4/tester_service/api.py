"""FastAPI service for preparing, running and retrieving cross-team tests over HTTP."""

import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
import yaml

from state import connect
from .cases import CASES
from .client import (
    CALL_SECONDS, DEFAULT_TARGETS, ERROR_SOURCES, RESPONSE_LIMIT, RUN_SECONDS, SETUP_SECONDS,
    CommunicationError, TargetClient, TargetError, base_url, valid_text,
)
from .runner import initial_report, run_suite


ROOT = Path(__file__).resolve().parents[1]
LOCAL_SYSTEMS = (*DEFAULT_TARGETS, "http://127.0.0.1:8000", "http://localhost:8000")
MAX_JSON_BYTES = 16 * 1024
logger = logging.getLogger(__name__)


class TesterError(Exception):
    def __init__(self, status, message, source="tester"):
        super().__init__(message)
        self.status = status
        self.source = source


def configured_urls(name, explicit):
    if explicit is None:
        raw = os.environ.get(name)
        explicit = LOCAL_SYSTEMS if raw is None else raw.split(",")
    return {base_url(url.strip()) for url in explicit if url.strip()}


async def json_body(request: Request):
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise TesterError(400, "Supply an application/json request body.")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_JSON_BYTES:
            raise TesterError(400, "The JSON request exceeds the 16 KiB limit.")
        data.extend(chunk)
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):
        raise TesterError(400, "Supply a valid JSON object.")
    if not isinstance(value, dict):
        raise TesterError(400, "Supply a JSON object.")
    return value


def create_app(database=None, approved_targets=None, own_targets=None, cases=CASES):
    database = Path(database or os.environ.get("TESTER_STATE_DB") or ROOT / "instance/tester.sqlite3")
    database.parent.mkdir(parents=True, exist_ok=True)
    with connect(database) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS contract_tester_sessions (
            id TEXT PRIMARY KEY,
            target_url TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ready','running','finished','aborted')),
            report TEXT,
            expires_at REAL
        )""")
    approved = configured_urls("POD_SYSTEM_URLS", approved_targets)
    own = configured_urls("OWN_SYSTEM_URLS", own_targets)
    app = FastAPI(title="AI Media Watermarking Cross-Team Tester")
    with (ROOT / "tester.openapi.yaml").open(encoding="utf-8") as spec:
        specification = yaml.safe_load(spec)
    # Relative to this app's OpenAPI URL, including a mounted deployment prefix.
    specification["servers"] = [{"url": "."}]
    app.openapi = lambda: specification

    @app.exception_handler(TesterError)
    async def contract_error(_request, exc):
        return JSONResponse({"message": str(exc), "source": exc.source}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def internal_error(_request, exc):
        logger.error("Tester service failed", exc_info=exc)
        return JSONResponse({"message": "The tester could not complete the operation.", "source": "tester"},
                            status_code=500)

    @app.post("/test/setup", status_code=201)
    def setup(body: dict = Depends(json_body)):
        try:
            target = base_url(body.get("target_url"))
        except ValueError as exc:
            raise TesterError(400, str(exc)) from exc
        if target not in approved:
            raise TesterError(403, "The system base URL is outside the configured pod targets.")
        session_id = str(uuid4())
        client = TargetClient(target, session_id, SETUP_SECONDS)
        try:
            response = client.request("POST", "/test/setup", json_body={"session_id": session_id, "setup": {}},
                                      evaluate=False)
            result = response.json()
        except CommunicationError as exc:
            raise TesterError(500, str(exc), "communication") from exc
        except (ValueError, RecursionError, TargetError) as exc:
            raise TesterError(500, "The target returned an invalid setup response.", "system") from exc
        if isinstance(result, dict):
            if (400 <= response.status_code < 600 and valid_text(result.get("message"), RESPONSE_LIMIT)
                    and result.get("source") in tuple(ERROR_SOURCES)):
                return JSONResponse(result, status_code=response.status_code)
            if response.status_code == 201 and result.get("session_id") == session_id:
                with connect(database) as conn:
                    conn.execute("INSERT INTO contract_tester_sessions (id,target_url,status) VALUES (?,?,'ready')",
                                 (session_id, target))
                return {"session_id": session_id}
        raise TesterError(500, "The target returned an unexpected setup status or session ID.", "system")

    @app.post("/test/run")
    def run(body: dict = Depends(json_body)):
        session_id = body.get("session_id")
        if not valid_text(session_id):
            raise TesterError(400, "Supply a nonempty session_id of up to 200 characters.")
        # An unexpired lease distinguishes a live worker from an interrupted process.
        # The margin allows a final socket read and the last database checkpoint.
        expires = time.time() + RUN_SECONDS + CALL_SECONDS + 15
        with connect(database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,target_url FROM contract_tester_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise TesterError(404, "The tester session does not exist.")
            if row["status"] != "ready":
                raise TesterError(409, "This session has already started its suite.")
            target = row["target_url"]
            report = initial_report(session_id, target, cases, target in own)
            conn.execute("UPDATE contract_tester_sessions SET status='running',report=?,expires_at=? WHERE id=?",
                         (json.dumps(report), expires, session_id))

        def store(value, status="running"):
            with connect(database) as conn:
                saved = conn.execute(
                    """UPDATE contract_tester_sessions SET status=?,report=? WHERE id=? AND status='running'
                       AND expires_at >= ?""", (status, json.dumps(value), session_id, time.time())
                ).rowcount
            if not saved:
                raise TesterError(500, "The tester timed out. Retrieve its stored report.")

        try:
            aborted = run_suite(report, cases, store, RUN_SECONDS)
        except TesterError:
            raise
        except Exception:
            logger.exception("Tester aborted for session %s", session_id)
            aborted = True
        store(report, "aborted" if aborted else "finished")
        if aborted:
            raise TesterError(500, "The tester aborted. Retrieve its stored report with the same session ID.")
        return report

    @app.get("/test/report")
    def get_report(request: Request):
        ids = request.query_params.getlist("session_id")
        if len(ids) != 1 or not valid_text(ids[0]):
            raise TesterError(400, "Supply one nonempty session_id of up to 200 characters.")
        with connect(database) as conn:
            row = conn.execute("SELECT status,report,expires_at FROM contract_tester_sessions WHERE id=?",
                               (ids[0],)).fetchone()
        if row is None:
            raise TesterError(404, "The tester session does not exist.")
        if row["status"] == "ready" or (row["status"] == "running" and row["expires_at"] >= time.time()):
            raise TesterError(409, "The suite has not ended.")
        return JSONResponse(json.loads(row["report"]), headers={"Cache-Control": "no-store"})

    return app


app = create_app()
