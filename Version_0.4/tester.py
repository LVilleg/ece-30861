"""Run the service's automated checks: python tester.py [--quick] [--report PATH].

The full suite covers synthetic and stock media, local Flask workflows, and
upload limits. --quick omits audio/video checks. All checks run locally and
use bundled fixtures or temporary files.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import unittest

try:
    from testing.legacy_cases import (
        PROJECT_ROOT,
        CommandLineTests,
        ConfigurationTests,
        ImageTests,
        MediaTests,
        WebTests,
    )
except ImportError as exc:
    print(f"Cannot load the application: {exc}", file=sys.stderr)
    print("Install requirements.txt with the Python interpreter running this tester.", file=sys.stderr)
    raise SystemExit(2)


def run_tests(quick=False, report_path=None):
    """Run local checks and return 0 for success, 1 for failed checks, or 2 for a report error."""
    classes = [ImageTests, WebTests, ConfigurationTests, CommandLineTests]
    if not quick:
        classes.append(MediaTests)
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in classes
    )
    mode = "quick" if quick else "full"
    print(f"Running {mode} tests against {PROJECT_ROOT}", file=sys.stderr, flush=True)
    print("Local application checks; no live AWS requests or 500 MiB load test.", file=sys.stderr, flush=True)
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    elapsed = time.perf_counter() - started
    if report_path is not None:
        report = {
            "mode": mode,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "project_directory": str(PROJECT_ROOT),
            "successful": result.wasSuccessful(),
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "elapsed_seconds": round(elapsed, 3),
            "failure_details": [
                {"test": test.id(), "traceback": detail}
                for test, detail in result.failures + result.errors
            ],
        }
        try:
            report_path = Path(report_path)
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Could not write test report: {exc}", file=sys.stderr)
            return 2
        print(f"JSON report: {report_path.resolve()}", file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="Run image, web, configuration, and command-line checks only")
    parser.add_argument("--report", type=Path, metavar="PATH", help="Write a JSON report, replacing PATH if it exists")
    args = parser.parse_args()
    return run_tests(quick=args.quick, report_path=args.report)


if __name__ == "__main__":
    raise SystemExit(_main())
