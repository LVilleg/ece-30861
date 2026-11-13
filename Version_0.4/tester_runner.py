"""Run tester.py in an isolated background process for the website.

Temporary job records are shared by workers on one server instance. The
supervisor owns the timeout and completion record, independently of Flask.
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
import hashlib


PROJECT_ROOT = Path(__file__).resolve().parent
RUN_TIMEOUT = 180
_ROOT_ID = hashlib.sha256(str(PROJECT_ROOT).encode()).hexdigest()[:12]
JOB_ROOT = Path(tempfile.gettempdir()) / ("ece30861-tester-" + _ROOT_ID)


@contextmanager
def _instance_lock():
    """Serialize job ownership changes across workers on this instance."""
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    with (JOB_ROOT / "guard").open("a+b") as guard:
        if guard.seek(0, os.SEEK_END) == 0:
            guard.write(b"\0")
            guard.flush()
        guard.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(guard.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            guard.seek(0)
            if os.name == "nt":
                msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


def _job_directory(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Invalid tester run ID.")
    return JOB_ROOT / run_id


def _write_status(directory, record):
    temporary = directory / ("status-" + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
    temporary.replace(directory / "status.json")


def _release_active_unlocked(run_id):
    lock = JOB_ROOT / "active"
    try:
        if lock.read_text(encoding="ascii").strip() == run_id:
            lock.unlink()
    except FileNotFoundError:
        pass


def _release_lock(run_id):
    with _instance_lock():
        _release_active_unlocked(run_id)


def get_tester_run(run_id):
    """Read a job summary and the tail of its test output."""
    directory = _job_directory(run_id)
    record = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    if record["status"] == "running" and time.time() - record["started_at"] > RUN_TIMEOUT + 30:
        record.update(status="error", message="The tester was interrupted or exceeded its time limit. Run it again.")
    try:
        with (directory / "output.log").open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 32000))
            record["output"] = log.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        record["output"] = ""
    return record


def _process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def start_tester_run(mode="full"):
    """Start a run, or return the current instance's already-running job ID."""
    if mode not in {"full", "quick"}:
        raise ValueError("Choose full or quick tester mode.")
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    lock = JOB_ROOT / "active"
    with _instance_lock():
        for _ in range(3):
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    active = lock.read_text(encoding="ascii").strip()
                    if get_tester_run(active)["status"] == "running":
                        return active
                    _release_active_unlocked(active)
                except (FileNotFoundError, ValueError):
                    # The supervisor may have been interrupted before setup.
                    if lock.exists() and time.time() - lock.stat().st_mtime < 30:
                        raise RuntimeError("Another tester run is starting. Try again shortly.")
                    lock.unlink(missing_ok=True)
            else:
                with os.fdopen(descriptor, "w", encoding="ascii") as owner:
                    owner.write(run_id)
                break
        else:
            raise RuntimeError("The tester is busy. Try again shortly.")

    directory = _job_directory(run_id)
    try:
        directory.mkdir()
        _write_status(directory, {
            "id": run_id, "mode": mode, "status": "running",
            "message": "The automatic tester is running.",
            "started_at": time.time(), "report": None,
        })
        subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--job", run_id],
            cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **_process_options(),
        )
    except Exception:
        _release_lock(run_id)
        raise
    return run_id


def _stop_test_process(process):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def _run_job(run_id):
    directory = _job_directory(run_id)
    record = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    command = [sys.executable, "-B", str(PROJECT_ROOT / "tester.py"),
               "--report", str(directory / "report.json")]
    if record["mode"] == "quick":
        command.append("--quick")
    try:
        with (directory / "output.log").open("wb") as log:
            process = subprocess.Popen(
                command, cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, **_process_options(),
            )
            try:
                exit_code = process.wait(timeout=RUN_TIMEOUT)
            except subprocess.TimeoutExpired:
                _stop_test_process(process)
                raise RuntimeError("The tester exceeded the 180-second time limit.")
        record["exit_code"] = exit_code
        report_path = directory / "report.json"
        if not report_path.exists():
            raise RuntimeError("The tester could not finish. See the test output for details.")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        record["report"] = report
        successful = exit_code == 0 and report["successful"]
        record.update(
            status="passed" if successful else "failed",
            message="All automatic checks passed." if successful else "Some automatic checks failed. Review the test output.",
        )
    except Exception as exc:
        record.update(status="error", message=str(exc))
    finally:
        record["completed_at"] = time.time()
        try:
            _write_status(directory, record)
        finally:
            _release_lock(run_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help=argparse.SUPPRESS)
    _run_job(parser.parse_args().job)
