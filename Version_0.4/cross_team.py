"""Legacy Flask tester for the survey-based project API."""

import json
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request

from cross_team_cases import CASES, TargetJobError
from state import connect
from tester_service.client import (
    CommunicationError, DEFAULT_TARGETS, ERROR_SOURCES, RESPONSE_LIMIT,
    RUN_SECONDS, SETUP_SECONDS, TargetClient, TargetError,
    base_url, code_coverage, unavailable, valid_text,
)


def error(status, message, source="tester"):
    return jsonify(message=message, source=source), status


def run_suite(session_id, target):
    client = TargetClient(target, session_id, RUN_SECONDS)
    results = []
    coverage = {
        "code": unavailable("Target coverage could not be collected."),
        "component": unavailable("Pod component inventory and counting rules are pending."),
        "system": unavailable("Pod acceptance criteria and counting rules are pending."),
    }
    aborted = False
    try:
        for test_id, run_case in CASES:
            result = {"test_id": test_id}
            try:
                result.update(outcome="pass", message=run_case(client))
            except AssertionError as exc:
                result.update(outcome="fail", message=str(exc) or "The response differed from expectations.")
            except (TargetError, TargetJobError) as exc:
                result.update(outcome="error", source="system", message=str(exc) or "The target failed.")
            except CommunicationError as exc:
                result.update(outcome="error", source="communication", message=str(exc))
            except Exception:
                current_app.logger.exception("Cross-team tester aborted while running %s", test_id)
                results.append(dict(result, outcome="error", source="tester", message="The tester could not evaluate this case."))
                aborted = True
                break
            results.append(result)
        for test_id, _ in CASES[len(results):]:
            results.append({"test_id": test_id, "outcome": "skip", "message": "The tester aborted before this case ran."})
        coverage["code"] = code_coverage(client)
    except Exception:
        current_app.logger.exception("Cross-team tester aborted while collecting coverage")
        aborted = True
    return {"session_id": session_id, "target_url": target, "results": results, "coverage": coverage}, aborted


def create_tester_blueprint(database):
    blueprint = Blueprint("cross_team", __name__)

    @blueprint.errorhandler(413)
    def request_too_large(_exc):
        return error(400, "The JSON request is too large.")

    @blueprint.before_request
    def limit_json_request():
        request.max_content_length = 16 * 1024

    @blueprint.post("/test/setup")
    def setup():
        body = request.get_json(silent=True)
        try:
            target = base_url(body.get("target_url") if isinstance(body, dict) else None)
        except ValueError as exc:
            return error(400, str(exc))
        if target not in current_app.config["TESTER_APPROVED_TARGETS"]:
            return error(403, "The target system base URL is outside the configured pod targets.")

        session_id = str(uuid4())
        client = TargetClient(target, session_id, SETUP_SECONDS)
        try:
            response = client.request("POST", "/test/setup",
                                      json_body={"session_id": session_id, "setup": {}}, evaluate=False)
            payload = response.json()
        except CommunicationError as exc:
            return error(500, str(exc), "communication")
        except (TargetError, ValueError):
            return error(500, "The target returned an invalid setup response.", "system")

        if isinstance(payload, dict):
            if response.status_code == 201 and payload.get("session_id") == session_id:
                with connect(database) as connection:
                    connection.execute("INSERT INTO tester_sessions VALUES (?, ?, 'ready', NULL)", (session_id, target))
                return jsonify(session_id=session_id), 201
            if (
                400 <= response.status_code < 600
                and valid_text(payload.get("message"), RESPONSE_LIMIT)
                and payload.get("source") in tuple(ERROR_SOURCES)
            ):
                return jsonify(payload), response.status_code
        return error(500, "The target returned an invalid setup response or session ID.", "system")

    @blueprint.post("/test/run")
    def run():
        body = request.get_json(silent=True)
        session_id = body.get("session_id") if isinstance(body, dict) else None
        if not valid_text(session_id):
            return error(400, "Supply a nonempty session_id.")
        with connect(database) as connection:
            claimed = connection.execute(
                "UPDATE tester_sessions SET status = 'running' WHERE id = ? AND status = 'ready'",
                (session_id,),
            ).rowcount
            row = connection.execute("SELECT target_url FROM tester_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return error(404, "The tester session does not exist.")
        if not claimed:
            return error(409, "This session has already started its one suite.")

        report, aborted = run_suite(session_id, row["target_url"])
        with connect(database) as connection:
            connection.execute(
                "UPDATE tester_sessions SET status = 'finished', report = ? WHERE id = ?",
                (json.dumps(report), session_id),
            )
        if aborted:
            return error(500, "The tester aborted. Retrieve the stored report with the same session ID.")
        return jsonify(report)

    @blueprint.get("/test/report")
    def report():
        ids = request.args.getlist("session_id")
        if len(ids) != 1 or not valid_text(ids[0]):
            return error(400, "Supply one nonempty session_id.")
        with connect(database) as connection:
            row = connection.execute("SELECT status, report FROM tester_sessions WHERE id = ?", (ids[0],)).fetchone()
        if row is None:
            return error(404, "The tester session does not exist.")
        if row["status"] != "finished":
            return error(409, "The suite has not ended.")
        response = jsonify(json.loads(row["report"]))
        response.cache_control.no_store = True
        return response

    @blueprint.errorhandler(500)
    def internal_error(_exc):
        return error(500, "The tester could not complete the operation.")

    return blueprint
