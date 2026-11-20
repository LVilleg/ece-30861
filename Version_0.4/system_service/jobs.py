"""Durable jobs for the optional-text media API."""

from datetime import datetime, timezone
import logging
from pathlib import Path
from threading import Condition, Thread
from time import time
from uuid import uuid4

from state import connect
from . import media


LOGGER = logging.getLogger(__name__)
LEASE_SECONDS = 300


def initialize(database):
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with connect(database) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        # This table can coexist with the earlier survey-image job format.
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS system_sessions (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS media_jobs (
                id TEXT PRIMARY KEY,
                session_id TEXT REFERENCES system_sessions(id),
                created_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'processing', 'completed', 'failed')),
                filename TEXT NOT NULL,
                media_kind TEXT NOT NULL,
                mime TEXT NOT NULL,
                source BLOB,
                payload TEXT,
                result BLOB,
                error TEXT,
                claim_token TEXT,
                lease_until REAL
            );
            CREATE INDEX IF NOT EXISTS media_job_queue ON media_jobs(status, created_at);
        """)


class Jobs:
    def __init__(self, database):
        self.database = database
        self.condition = Condition()
        self.running = False

    def submit(self, session_id, filename, kind, mime, source, payload):
        job = {
            "id": str(uuid4()), "status": "queued", "media_kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with connect(self.database) as connection:
            connection.execute(
                """INSERT INTO media_jobs
                   (id, status, media_kind, created_at, session_id, filename, mime, source, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job["id"], job["status"], kind, job["created_at"], session_id,
                 filename, mime, source, payload),
            )
        self.wake()
        return job

    def get(self, job_id, session_id, *, include_result=False):
        fields = "id, status, created_at, media_kind, filename, mime, error"
        if include_result:
            fields += ", result"
        with connect(self.database) as connection:
            job = connection.execute(
                f"SELECT {fields} FROM media_jobs WHERE id = ? AND session_id IS ?",
                (job_id, session_id),
            ).fetchone()
        if job and job["status"] in ("queued", "processing"):
            self.wake()
        return job

    def wake(self):
        with self.condition:
            if self.running:
                self.condition.notify()
            else:
                self.running = True
                Thread(target=self._drain, name="media-watermark", daemon=True).start()

    def _claim(self):
        now = time()
        token = str(uuid4())
        with connect(self.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                """SELECT id, filename, source, payload FROM media_jobs
                   WHERE status = 'queued' OR (status = 'processing' AND lease_until <= ?)
                   ORDER BY created_at, id LIMIT 1""", (now,),
            ).fetchone()
            if job is None:
                next_lease = connection.execute(
                    "SELECT MIN(lease_until) FROM media_jobs WHERE status = 'processing'"
                ).fetchone()[0]
                return None, None, next_lease
            connection.execute(
                """UPDATE media_jobs SET status = 'processing', claim_token = ?, lease_until = ?
                   WHERE id = ?""", (token, now + LEASE_SECONDS, job["id"]),
            )
        return job, token, None

    def _process(self, job, token):
        try:
            result = media.embed(job["source"], job["filename"], job["payload"])
            error = None
        except Exception:
            LOGGER.exception("Could not watermark job %s", job["id"])
            result = None
            error = "The media could not be watermarked."
        with connect(self.database) as connection:
            connection.execute(
                """UPDATE media_jobs SET status = ?, result = ?, error = ?, source = NULL,
                   payload = NULL, claim_token = NULL, lease_until = NULL
                   WHERE id = ? AND claim_token = ?""",
                ("failed" if error else "completed", result, error, job["id"], token),
            )

    def _drain(self):
        try:
            while True:
                with self.condition:
                    job, token, next_lease = self._claim()
                    if job is None:
                        if next_lease is None:
                            self.running = False
                            return
                        # An interrupted process's job becomes claimable without another HTTP call.
                        self.condition.wait(timeout=max(0, next_lease - time()))
                        continue
                self._process(job, token)
        except Exception:
            LOGGER.exception("Could not drain the media job queue")
            with self.condition:
                self.running = False
