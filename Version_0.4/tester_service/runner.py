"""Execute the tester-owned cases and retain each result for report recovery."""

import logging

from .checks import TargetJobError
from .client import CommunicationError, TargetClient, TargetError, code_coverage, unavailable


logger = logging.getLogger(__name__)


def initial_report(session_id, target, cases, own_team):
    coverage = {
        "code": unavailable("The target's per-session code measurement has not been received."),
        "component": unavailable("The supplied pod API does not enumerate components or their exercise rules."),
        "system": unavailable("The supplied pod API describes behavior but provides no criterion IDs or counting rules."),
    }
    if own_team:
        coverage["model"] = unavailable(
            "No versioned milestone design requirement inventory or mapping to these tests was supplied."
        )
    return {
        "session_id": session_id,
        "target_url": target,
        "results": [{"test_id": test_id, "outcome": "skip", "message": "The suite ended before this test ran."}
                    for test_id, _ in cases],
        "coverage": coverage,
    }


def run_suite(report, cases, save_progress, duration):
    client = TargetClient(report["target_url"], report["session_id"], duration)
    aborted = False
    for index, (test_id, run_case) in enumerate(cases):
        if not client.remaining():
            for pending in report["results"][index:]:
                pending["message"] = "The suite's target-call budget expired before this test ran."
            break
        result = report["results"][index]
        # The checkpoint is already a complete report if this worker dies inside the case.
        result.update(outcome="error", source="tester", message="The tester stopped while evaluating this case.")
        save_progress(report)
        try:
            message = run_case(client)
        except AssertionError as exc:
            result.update(outcome="fail", message=str(exc) or "The response differed from expectations.")
            del result["source"]
        except (TargetJobError, TargetError) as exc:
            result.update(source="system", message=str(exc) or "The target reported an internal failure.")
        except CommunicationError as exc:
            result.update(source="communication", message=str(exc) or "No complete target response was received.")
        except Exception:
            logger.exception("Tester aborted in %s", test_id)
            aborted = True
            break
        else:
            result.update(outcome="pass", message=message)
            del result["source"]
        save_progress(report)
    report["coverage"]["code"] = code_coverage(client)
    return aborted
