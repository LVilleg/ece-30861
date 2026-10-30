
# Phase 4 — Requirements Traceability Matrix

## Purpose

This document maps the requirements and acceptance criteria from
Phase 2 to their corresponding source code, tests, and verification
evidence.

The goal is to identify which requirements have been implemented,
which have been verified through testing, and which still need work.

## Requirements Matrix

| ID | Source Implementation | Relevant Tests | Implemented | Verified | Remaining |
|---|---|---|---|---|---|
| REQ-01 / AC-01 | `watermark.py` — Audio functions | `test_audio_formats`, `test_short_audio_is_padded` | To verify | Not yet confirmed | Confirm supported audio formats, padding behavior, and all AC-01 conditions. |
| REQ-02 / AC-02 | `watermark.py` — Image functions | `test_supported_image_formats`, `test_jpeg_recompression` | To verify | Not yet confirmed | Confirm supported image formats, JPEG recompression handling, and all AC-02 conditions. |
| REQ-03 / AC-03 | `watermark.py` — Video functions | `test_video_formats_and_optional_audio` | To verify | Not yet confirmed | Confirm supported video formats, optional audio handling, and all AC-03 conditions. |
| REQ-04 / AC-04 | `detection.py`, `watermark.py` | Existing positive/negative detection tests | To verify | Not yet confirmed | Run detection tests and complete the required accuracy benchmark. |

## Verification Status Definitions

- **Implemented:** The required functionality exists in the source code.
- **Verified:** Tests or other documented evidence demonstrate that the acceptance criterion is satisfied.
- **Remaining:** Any missing functionality, untested behavior, or incomplete acceptance criterion.

## Verification Evidence

Record the test command, execution date, results, and any relevant
logs or reports for each requirement.

Do not mark a requirement as verified solely because a test exists.
The test must execute successfully and validate the required behavior.

## Outstanding Work

- [ ] Confirm the implementation of REQ-01 / AC-01.
- [ ] Confirm the implementation of REQ-02 / AC-02.
- [ ] Confirm the implementation of REQ-03 / AC-03.
- [ ] Confirm the implementation of REQ-04 / AC-04.
- [ ] Execute the relevant tests and record their results.
- [ ] Complete the detection accuracy benchmark.
- [ ] Compare all results against the original Phase 2 acceptance criteria.
