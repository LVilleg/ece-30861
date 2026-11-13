"""SQLite state shared by this deployment's Flask processes and job workers."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3


@contextmanager
def connect(database):
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(database):
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with connect(database) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS system_sessions (
                id TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS watermark_jobs (
                id TEXT PRIMARY KEY,
                session_id TEXT REFERENCES system_sessions(id),
                status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'completed', 'failed')),
                created_at TEXT NOT NULL,
                filename TEXT NOT NULL,
                image_format TEXT NOT NULL,
                source BLOB,
                survey_payload BLOB,
                result BLOB,
                error TEXT,
                claim_token TEXT,
                lease_until REAL
            );

            CREATE INDEX IF NOT EXISTS watermark_jobs_queue
                ON watermark_jobs(status, created_at);

            CREATE TABLE IF NOT EXISTS tester_sessions (
                id TEXT PRIMARY KEY,
                target_url TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ready', 'running', 'finished')),
                report TEXT
            );
        """)
