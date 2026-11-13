"""Job-based image watermarking and system test endpoints."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
import logging
from pathlib import PurePath
from threading import Lock
from time import time
from uuid import uuid4

from flask import Blueprint, current_app, g, jsonify, request, send_file, url_for
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from state import connect
from survey_watermark import (
    UnsupportedImageError,
    WatermarkInputError,
    detect_image,
    encode_payload,
    validate_image,
    watermark_image,
)


MAX_FILE_SIZE = 100_000_000
MULTIPART_ALLOWANCE = 1024 * 1024
SESSION_HEADER = "X-Test-Session-Id"
WORKER_LEASE_SECONDS = 300
IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
CONTENT_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg"}
LOGGER = logging.getLogger(__name__)


def _project_error(status, code, message):
    return jsonify(code=code, message=message), status


def _session_error(status, message):
    return jsonify(message=message, source="system"), status


def _valid_session_id(session_id):
    return isinstance(session_id, str) and bool(session_id.strip()) and len(session_id) <= 200


class _WorkQueue:
    """Drain the shared SQLite queue with at most one thread per app process."""

    def __init__(self, database):
        self.database = database
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watermark")
        self.lock = Lock()
        self.running = False

    def kick(self):
        with self.lock:
            if not self.running:
                self.executor.submit(self._drain)
                self.running = True

    def _claim(self):
        token = str(uuid4())
        now = time()
        with connect(self.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                """
                SELECT id FROM watermark_jobs
                WHERE status = 'queued' OR (status = 'processing' AND lease_until <= ?)
                ORDER BY created_at, id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if job is None:
                return None
            connection.execute(
                """
                UPDATE watermark_jobs SET status = 'processing', claim_token = ?, lease_until = ?
                WHERE id = ?
                """,
                (token, now + WORKER_LEASE_SECONDS, job["id"]),
            )
        return job["id"], token

    def _finish(self, job_id, token, status, result=None, error=None):
        with connect(self.database) as connection:
            connection.execute(
                """
                UPDATE watermark_jobs
                SET status = ?, result = ?, error = ?, source = NULL, survey_payload = NULL,
                    claim_token = NULL, lease_until = NULL
                WHERE id = ? AND claim_token = ?
                """,
                (status, result, error, job_id, token),
            )

    def _process(self, job_id, token):
        with connect(self.database) as connection:
            job = connection.execute(
                "SELECT source, survey_payload FROM watermark_jobs WHERE id = ? AND claim_token = ?",
                (job_id, token),
            ).fetchone()
        if job is None:
            return
        try:
            result = watermark_image(job["source"], job["survey_payload"])
        except WatermarkInputError as error:
            self._finish(job_id, token, "failed", error=str(error) or "The image could not be watermarked.")
        except Exception:
            LOGGER.exception("Watermark worker failed for job %s", job_id)
            self._finish(job_id, token, "failed", error="The image could not be watermarked.")
        else:
            self._finish(job_id, token, "completed", result=result)

    def _drain(self):
        try:
            while True:
                # Holding the lock through the empty check prevents a submitter
                # from missing its wakeup as the worker becomes idle.
                with self.lock:
                    claimed = self._claim()
                    if claimed is None:
                        self.running = False
                        return
                self._process(*claimed)
        except Exception:
            LOGGER.exception("Could not process the watermark queue")
            with self.lock:
                self.running = False


def _job_json(job):
    document = {
        "id": job["id"],
        "status": job["status"],
        "mediaKind": "image",
        "createdAt": job["created_at"],
    }
    if job["status"] == "completed":
        document["resultUrl"] = url_for("system_api.watermark_result", job_id=job["id"])
    elif job["status"] == "failed":
        document["error"] = job["error"]
    return document


def _read_image():
    files = request.files.getlist("file")
    if len(files) != 1 or not files[0].filename:
        raise WatermarkInputError("Supply exactly one file in the multipart file field.")
    upload = files[0]
    filename = PurePath(upload.filename.replace("\\", "/")).name
    expected_format = IMAGE_FORMATS.get(PurePath(filename).suffix.lower())
    if expected_format is None:
        raise UnsupportedImageError("Only PNG and JPEG images are supported.")
    data = upload.stream.read(MAX_FILE_SIZE + 1)
    if len(data) > MAX_FILE_SIZE:
        raise RequestEntityTooLarge()
    image_format = validate_image(data)
    if image_format != expected_format:
        raise WatermarkInputError("The filename extension does not match the decoded image format.")
    return filename, image_format, data


def create_system_blueprint(database):
    blueprint = Blueprint("system_api", __name__)
    queue = _WorkQueue(database)
    test_endpoints = {"system_api.test_setup", "system_api.test_report"}

    @blueprint.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error):
        if request.endpoint in test_endpoints:
            return _session_error(400, "The test request body is too large.")
        return _project_error(413, "payload_too_large", "The file exceeds the 100 MB limit.")

    @blueprint.errorhandler(WatermarkInputError)
    def invalid_upload(error):
        if isinstance(error, UnsupportedImageError):
            return _project_error(415, "unsupported_media_type", str(error))
        return _project_error(400, "bad_request", str(error))

    @blueprint.errorhandler(Exception)
    def system_failure(error):
        if isinstance(error, HTTPException):
            if request.endpoint in test_endpoints:
                return _session_error(error.code, error.description)
            return _project_error(error.code, error.name.lower().replace(" ", "_"), error.description)
        current_app.logger.exception("System API operation failed")
        if request.endpoint in test_endpoints:
            return _session_error(500, "The system test operation failed.")
        return _project_error(500, "internal_error", "The project operation failed.")

    @blueprint.before_request
    def prepare_request():
        if request.endpoint in test_endpoints:
            request.max_content_length = 16 * 1024
            return None
        if request.endpoint in {"system_api.create_job", "system_api.detect"}:
            request.max_content_length = MAX_FILE_SIZE + MULTIPART_ALLOWANCE
        session_id = request.headers.get(SESSION_HEADER)
        g.system_session_id = session_id
        if session_id is None:
            return None
        if not _valid_session_id(session_id):
            return _project_error(400, "bad_request", f"Invalid {SESSION_HEADER} header.")
        with connect(database) as connection:
            session = connection.execute(
                "SELECT id FROM system_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if session is None:
            return _project_error(404, "unknown_session", "The test session does not exist.")
        return None

    @blueprint.get("/health")
    def health():
        return jsonify(status="ok")

    @blueprint.post("/watermark-jobs")
    def create_job():
        filename, image_format, source = _read_image()
        statuses = request.form.getlist("ai_status")
        if len(statuses) != 1:
            raise WatermarkInputError("Supply exactly one ai_status.")
        payload = encode_payload(statuses[0], request.form.getlist("ai_tools"))
        job = {
            "id": str(uuid4()),
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with connect(database) as connection:
            connection.execute(
                """
                INSERT INTO watermark_jobs
                    (id, session_id, status, created_at, filename, image_format, source, survey_payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["id"], g.system_session_id, job["status"], job["created_at"],
                    filename, image_format, source, payload,
                ),
            )
        queue.kick()
        location = url_for("system_api.job_status", job_id=job["id"])
        return jsonify(_job_json(job)), 202, {"Location": location}

    @blueprint.get("/watermark-jobs/<job_id>")
    def job_status(job_id):
        with connect(database) as connection:
            job = connection.execute(
                """
                SELECT id, status, created_at, error FROM watermark_jobs
                WHERE id = ? AND session_id IS ?
                """,
                (job_id, g.system_session_id),
            ).fetchone()
        if job is None:
            return _project_error(404, "job_not_found", "No job exists with that ID.")
        if job["status"] in {"queued", "processing"}:
            queue.kick()
        return jsonify(_job_json(job))

    @blueprint.get("/watermark-jobs/<job_id>/result")
    def watermark_result(job_id):
        with connect(database) as connection:
            job = connection.execute(
                """
                SELECT status, filename, image_format, result, error FROM watermark_jobs
                WHERE id = ? AND session_id IS ?
                """,
                (job_id, g.system_session_id),
            ).fetchone()
        if job is None:
            return _project_error(404, "job_not_found", "No job exists with that ID.")
        if job["status"] in {"queued", "processing"}:
            queue.kick()
            return _project_error(409, "job_not_ready", "The job has not finished.")
        if job["status"] == "failed":
            return _project_error(409, "job_failed", job["error"])
        return send_file(
            BytesIO(job["result"]),
            mimetype=CONTENT_TYPES[job["image_format"]],
            download_name=job["filename"],
            as_attachment=True,
        )

    @blueprint.post("/detect")
    def detect():
        _, _, data = _read_image()
        return jsonify(detect_image(data))

    @blueprint.post("/test/setup")
    def test_setup():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not _valid_session_id(body.get("session_id")):
            return _session_error(400, "Supply a nonempty session_id of at most 200 characters.")
        if body.get("setup", {}) != {}:
            return _session_error(400, "The setup object must be empty.")
        session_id = body["session_id"]
        # Setup has no resources to prepare for this pod. Insertion publishes
        # the ready session in one transaction.
        with connect(database) as connection:
            inserted = connection.execute(
                "INSERT INTO system_sessions (id) VALUES (?) ON CONFLICT(id) DO NOTHING",
                (session_id,),
            ).rowcount
        if not inserted:
            return _session_error(409, "The session ID already exists.")
        return jsonify(session_id=session_id), 201

    @blueprint.get("/test/report")
    def test_report():
        session_ids = request.args.getlist("session_id")
        if len(session_ids) != 1 or not _valid_session_id(session_ids[0]):
            return _session_error(400, "Supply one nonempty session_id of at most 200 characters.")
        session_id = session_ids[0]
        with connect(database) as connection:
            session = connection.execute(
                "SELECT id FROM system_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if session is None:
            return _session_error(404, "The test session does not exist.")
        return jsonify(
            session_id=session_id,
            coverage={
                "code": {
                    "covered": None,
                    "total": None,
                    "basis": "Source-line coverage instrumentation is unavailable for this system.",
                }
            },
        )

    return blueprint
