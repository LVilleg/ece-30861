"""FastAPI entry point for the public media system contract."""

from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
import yaml

from state import connect
from . import media
from .jobs import Jobs, initialize


BASE = Path(__file__).resolve().parent
MAX_FILE_SIZE = 100_000_000
MULTIPART_ALLOWANCE = 1024 * 1024
SESSION_HEADER = "X-Test-Session-Id"
LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


class BodyTooLarge(MultiPartException):
    """The multipart parser closes its spool files on MultiPartException."""


def valid_id(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


async def bounded_stream(request, limit):
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise ApiError(400, "bad_request", "Invalid Content-Length.")
        if length > limit:
            raise BodyTooLarge("Request body is too large.")
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge("Request body is too large.")
        yield chunk


async def read_upload(request):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "multipart/form-data":
        raise ApiError(400, "bad_request", "Supply a multipart form containing one file.")
    try:
        parts = await MultiPartParser(
            request.headers, bounded_stream(request, MAX_FILE_SIZE + MULTIPART_ALLOWANCE),
            max_files=1, max_fields=16,
        ).parse()
    except BodyTooLarge:
        raise ApiError(413, "payload_too_large", "The file exceeds the 100 MB limit.")
    except (MultiPartException, MultipartParseError, ValueError) as exc:
        raise ApiError(400, "bad_request", "Invalid multipart form.") from exc
    try:
        files = parts.getlist("file")
        if len(files) != 1 or not isinstance(files[0], UploadFile) or not files[0].filename:
            raise ApiError(400, "bad_request", "Supply exactly one file in the file field.")
        upload = files[0]
        filename = upload.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if upload.size is not None and upload.size > MAX_FILE_SIZE:
            raise ApiError(413, "payload_too_large", "The file exceeds the 100 MB limit.")
        data = await upload.read(MAX_FILE_SIZE + 1)
        if len(data) > MAX_FILE_SIZE:
            raise ApiError(413, "payload_too_large", "The file exceeds the 100 MB limit.")
        payloads = parts.getlist("payload")
        if len(payloads) > 1 or (payloads and (not isinstance(payloads[0], str) or len(payloads[0]) > 64)):
            raise ApiError(400, "bad_request", "Supply at most one text payload of at most 64 characters.")
        return filename, data, payloads[0] if payloads else None
    finally:
        await parts.close()


def job_document(request, job):
    value = {"id": job["id"], "status": job["status"], "mediaKind": job["media_kind"],
             "createdAt": job["created_at"]}
    if job["status"] == "completed":
        value["resultUrl"] = job_path(request, job["id"]) + "/result"
    if job["status"] == "failed":
        value["error"] = job["error"]
    return value


def job_path(request, job_id):
    # ASGI root_path includes mounts and any reverse-proxy prefix.
    return request.scope.get("root_path", "").rstrip("/") + "/watermark-jobs/" + job_id


def create_app(database=None):
    database = Path(database or os.getenv("SYSTEM_STATE_DB") or os.getenv("WATERMARK_STATE_DB")
                    or BASE.parent / "instance" / "system.sqlite3").resolve()
    initialize(database)
    jobs = Jobs(database)

    @asynccontextmanager
    async def lifespan(_app):
        jobs.wake()
        yield

    app = FastAPI(title="AI Media Watermarking API", version="0.1.0", lifespan=lifespan)
    app.state.jobs = jobs
    with (BASE / "openapi.yaml").open(encoding="utf-8") as schema:
        contract = yaml.safe_load(schema)
    app.openapi = lambda: contract

    def error(request, status, code, message):
        body = {"code": code, "message": message}
        route = request.scope.get("route")
        if getattr(route, "path", "").startswith("/test/"):
            body["source"] = "system"
        return JSONResponse(body, status_code=status)

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return error(request, exc.status, exc.code, str(exc))

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        LOGGER.error("System operation failed", exc_info=exc)
        return error(request, 500, "internal_error", "The system operation failed.")

    def session(request: Request):
        session_id = request.headers.get(SESSION_HEADER)
        if session_id is None:
            return None
        if not valid_id(session_id):
            raise ApiError(400, "bad_request", f"Invalid {SESSION_HEADER} header.")
        with connect(database) as connection:
            known = connection.execute("SELECT 1 FROM system_sessions WHERE id = ?", (session_id,)).fetchone()
        if known is None:
            raise ApiError(404, "unknown_session", "The test session does not exist.")
        return session_id

    @app.get("/health", dependencies=[Depends(session)])
    def health():
        return {"status": "ok"}

    @app.post("/watermark-jobs", status_code=202)
    async def create_job(request: Request, session_id=Depends(session)):
        filename, data, payload = await read_upload(request)
        try:
            kind, mime = await run_in_threadpool(media.validate_media, data, filename)
        except media.UnsupportedMedia as exc:
            raise ApiError(415, "unsupported_media_type", str(exc)) from exc
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc
        job = await run_in_threadpool(jobs.submit, session_id, filename, kind, mime, data, payload)
        return JSONResponse(job_document(request, job), status_code=202,
                            headers={"Location": job_path(request, job["id"])})

    @app.get("/watermark-jobs/{jobId}")
    def job_status(jobId: str, request: Request, session_id=Depends(session)):
        job = jobs.get(jobId, session_id)
        if job is None:
            raise ApiError(404, "job_not_found", "No job exists with that ID.")
        return job_document(request, job)

    @app.get("/watermark-jobs/{jobId}/result")
    def result(jobId: str, session_id=Depends(session)):
        job = jobs.get(jobId, session_id, include_result=True)
        if job is None:
            raise ApiError(404, "job_not_found", "No job exists with that ID.")
        if job["status"] == "failed":
            raise ApiError(409, "job_failed", job["error"])
        if job["status"] != "completed":
            raise ApiError(409, "job_not_ready", "The job has not finished.")
        filename = quote(job["filename"], safe="")
        return Response(job["result"], media_type=job["mime"],
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})

    @app.post("/detect", dependencies=[Depends(session)])
    async def detect(request: Request):
        filename, data, _ = await read_upload(request)
        try:
            return await run_in_threadpool(media.detect, data, filename)
        except media.UnsupportedMedia as exc:
            raise ApiError(415, "unsupported_media_type", str(exc)) from exc
        except ValueError as exc:
            raise ApiError(400, "bad_request", str(exc)) from exc

    @app.post("/test/setup", status_code=201)
    async def setup(request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(400, "bad_request", "Supply a JSON setup object.")
        try:
            data = bytearray()
            async for chunk in bounded_stream(request, 16 * 1024):
                data.extend(chunk)
            body = json.loads(data)
        except (BodyTooLarge, ValueError, RecursionError) as exc:
            raise ApiError(400, "bad_request", "Supply a JSON setup object of at most 16 KiB.") from exc
        if not isinstance(body, dict) or not valid_id(body.get("session_id")):
            raise ApiError(400, "bad_request", "Supply a nonempty session_id of at most 200 characters.")
        if body.get("setup", {}) != {}:
            raise ApiError(400, "bad_request", "The setup object must be empty.")
        session_id = body["session_id"]
        await run_in_threadpool(register_session, session_id)
        return {"session_id": session_id}

    def register_session(session_id):
        with connect(database) as connection:
            inserted = connection.execute(
                "INSERT INTO system_sessions(id) VALUES (?) ON CONFLICT(id) DO NOTHING", (session_id,),
            ).rowcount
        if not inserted:
            raise ApiError(409, "session_conflict", "The session ID already exists.")

    @app.get("/test/report")
    def report(request: Request):
        ids = request.query_params.getlist("session_id")
        if len(ids) != 1 or not valid_id(ids[0]):
            raise ApiError(400, "bad_request", "Supply one nonempty session_id of at most 200 characters.")
        with connect(database) as connection:
            known = connection.execute("SELECT 1 FROM system_sessions WHERE id = ?", (ids[0],)).fetchone()
        if known is None:
            raise ApiError(404, "unknown_session", "The test session does not exist.")
        return {"session_id": ids[0], "coverage": {"code": {
            "covered": None, "total": None,
            "basis": "Source-line coverage instrumentation is unavailable for this system.",
        }}}

    return app


app = create_app()
