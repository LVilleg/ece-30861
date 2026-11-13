# AI Media Watermarking System and Cross-Team Tester

Revise the Flask service in `Version_0.3` to implement the job-based watermarking API and build the ECE 30861 cross-team tester. The tester must run the same ordered suite against any approved, compatible pod system selected by `target_url`, including our own system.

## Sources and repository

- Use the course PDF for the tester lifecycle, JSON contract, and error handling (pp. 2–7), the Phase 3 check (p. 8), and the tester OpenAPI starter (p. 9). The burger classifier illustrates the protocol. [Course interface](student-facing-spec.docx.pdf)
- Use the project OpenAPI document (OpenAPI 3.1.0, API version 0.2.0) for the system routes, request and response schemas, survey payload, and supported branch behavior. Its development server is `http://localhost:8000`. Apply the course PDF’s lifecycle rules to the system test endpoints. [Project API](project.yaml)
- `Version_0.3/main.py` owns the Flask routes, and `application.py` exports the Elastic Beanstalk WSGI application. `tester.py` provides local regression tests and synthetic fixtures. `tester_runner.py` starts those checks for the existing browser tester. Reuse the fixtures where useful and preserve the existing browser and CLI workflows during migration.

## Target architecture and migration

1. Keep the browser routes separate from the system API and course tester API. Use distinguishable system and tester base URLs, such as `/system` and `/tester` when deployed on one host. Paths in each contract are relative to its own base. Keep the Flask WSGI entry point working.
2. Build the system API around asynchronous watermark jobs. Validate the multipart request, assign a job UUID, and enqueue work before returning `202`. The worker takes the job through `queued` and `processing` to `completed` or `failed`. Store job metadata, errors, and result artifacts by UUID. Make state transitions and result publication consistent across the API and workers, and keep one job’s artifact separate from other jobs and the browser’s shared download slot.
3. Add the invisible image watermark encoder and detector for the survey payload in `project.yaml`. The current fixed marker and Boolean detector in `watermark.py` and `detection.py` cover the legacy flow. Use the new payload-aware detection path for `/detect`. Enforce the project API’s 100 MB file limit for both uploads, allowing for multipart overhead in the request cap. The legacy browser uses a 500 MiB file cap.
4. Add the system session setup and report endpoints, then build the cross-team runner on the public system HTTP API. Keep the runner’s one-suite session and report state independent of watermark job state and the browser’s local regression jobs.

## System project API

The current contract supports PNG and JPEG (`.png`, `.jpg`, `.jpeg`). WAV, MP3, and MP4 appear as planned formats in the schemas. Apply the current image-only scope to implementation and test expectations.

| Operation | Request and response |
| --- | --- |
| `GET /health` | `200` with JSON status, illustrated by `{"status":"ok"}`. |
| `POST /watermark-jobs` | Multipart fields `file`, `ai_status`, and repeated `ai_tools`. Return `202`, a `Location` header for the new job, and a JSON `Job`. |
| `GET /watermark-jobs/{jobId}` | `200` with the current `Job` or `404` for an unknown UUID. Poll until `completed` or `failed` within the agreed deadline. |
| `GET /watermark-jobs/{jobId}/result` | `200` with binary media in the original format once complete. Return `404` for an unknown UUID and `409` while queued or processing. |
| `POST /detect` | Multipart `file`. Return `200` with JSON `detected`, `confidence` in `[0,1]`, and `mediaKind`. `payload` is a lowercase hex string on a detected watermark. The schema allows null or omission for an undetected payload. |

- Require `ai_status` to be `generated`, `edited`, or `both`. Require one to four unique `ai_tools` values as repeated form fields. The supported values and byte codes are `unknown` (0), `openai` (1), `midjourney` (2), `stable_diffusion` (3), `flux` (4), `firefly` (5), `google` (6), and `other` (254).
- A `Job` contains `id` (UUID), `status`, `mediaKind`, and `createdAt` (date-time). Supply `resultUrl` when completed and error details when failed. Resolve `Location` and `resultUrl` against the selected target while preserving its deployment prefix and approved origin.
- For both upload routes, return `400` for malformed requests, `413` for an oversized file, and `415` for an unsupported type. Project error responses have `code` and `message`. Course test endpoint errors use `message` and `source`.
- Encode the 14-byte survey payload as one status byte, one tool count byte, four tool-code bytes with unused slots set to zero, four CRC32 bytes, and four Reed–Solomon parity bytes. Detection exposes the payload as 28 lowercase hexadecimal characters. `confidence` measures mean normalized bit-vote consistency before error correction and has no provenance or authenticity semantics.

## Details to confirm with the pod

`project.yaml` specifies `setup: {}` but leaves the session association for project calls, timeouts and retries, concurrency, resets, cleanup, and access policy unspecified. It also leaves the component and acceptance-criteria inventories and their coverage counting rules unspecified. Confirm and document these details before relying on them for cross-team runs.

Confirm the status-to-byte mapping, tool ordering, checksum and parity parameters, and interoperable image embedding and extraction rules against the pod’s reference implementation or agreed test vectors. Confirm the exact 100 MB byte threshold and the result response for a failed job. These details can be resolved alongside the job API and course lifecycle work.

## Tester API

Paths below are relative to the tester base URL. Use JSON bodies for the POST operations and JSON responses for all three operations.

| Operation | Input | Success and state handling |
| --- | --- | --- |
| `POST /test/setup` | `{"target_url":"https://team-b.example.edu/system"}` | `201` with `{"session_id":"<id>"}` after the target confirms readiness. |
| `POST /test/run` | `{"session_id":"<id>"}` | `200` with the stored suite report once the suite finishes, including failures and target errors. Return `409` for a duplicate or concurrent run of the same session, even after an abort. |
| `GET /test/report?session_id=<id>` | Query parameter only | `200` with the stored suite report after completion or abort. Return `409` before the suite ends. Lookup is read-only. |

Validate required nonempty strings and JSON object bodies before starting work. Return `400` for invalid requests, `404` for unknown sessions, `409` for conflicts or reports that are pending, and `500` for service failures or tester aborts. Error bodies contain a nonempty `message` and a `source` of `tester`, `system`, `communication`, or `unknown`. Use `401` or `403` where authentication or authorization applies.

### Setup

1. Accept an approved HTTP(S) system base URL with no `/test` suffix, query, fragment, or embedded credentials. Preserve path prefixes when appending endpoint paths. For example, a base ending in `/system` resolves setup to `/system/test/setup`.
2. Create a fresh UUID for each setup. Send `POST <target_url>/test/setup` with `{"session_id":"<id>","setup":{}}`. This project uses an empty setup object.
3. Wait for target HTTP `201` with the exact session ID sent. Store the ID and target URL as a ready session, then return `201`. Setup only prepares a session.
4. Forward a valid target setup error with its original status, `message`, and `source`. Use `500` with `source: "tester"` for an internal tester setup failure, `source: "communication"` when a connection failure leaves no response, or `source: "system"` for a malformed target setup response such as a mismatched ID. Failed setup leaves no runnable session.

### Run and recovery

1. Atomically claim a ready session after validating the request. Invalid requests leave the session available. Keep IDs, target URLs, and reports until the tester instance stops. Ensure concurrent requests and the chosen worker layout share consistent session state.
2. Run the planned tests in order through the target’s regular project API. Use the pod-defined mechanism to associate each call with the prepared session. The tester owns assertions and any pod-defined sequence or reset steps. Apply the agreed project-call timeouts and retry rules.
3. After project calls finish, request `GET <target_url>/test/report?session_id=<id>` with no body. Read the returned `coverage.code` for that session. Compute component and system coverage from the pod contract.
4. Store the complete report before returning `200`. For a tester abort, preserve recorded outcomes, mark cases prevented from evaluation by the abort as `error` with `source: "tester"`, and mark remaining unexecuted cases `skip`. Store the resulting report before returning `500` with a nonempty `message` and `source: "tester"`.
5. Serve the same finalized report on each lookup. If the run response is lost or times out, the caller retrieves the report and retries the lookup if it returns `409`. Repeating the suite or changing targets requires a fresh setup and ID.

## Suite report

The report has `session_id`, `target_url`, an ordered `results` array, and `coverage` with `code`, `component`, and `system` entries. Include every planned test exactly once. Use a stable, unique, nonempty `test_id` for each case and a nonempty `message` that describes the expectation, observed behavior, or reason for the outcome.

| `outcome` | Meaning |
| --- | --- |
| `pass` | The response matches expectations, including an expected project error. |
| `fail` | The response can be evaluated and differs from expectations. |
| `error` | A failure prevents evaluation. Include `source`. Use `system` for an unexpected target internal failure, `communication` for a timeout or connection failure with no response, `tester` for a tester fault, and `unknown` if the location cannot be identified. |
| `skip` | The case was never executed. Explain why. |

Each coverage entry contains `covered`, `total`, and a nonempty `basis` describing the measure and scope. Counts are nonnegative integers with `covered <= total`. For any unavailable measure, both counts are `null` and `basis` explains why.

- `code` comes from the target’s source-line or branch measurements for this session. If the target report is unavailable or unusable, record the reason in `basis` and keep the suite outcomes.
- `component` counts distinct pod-defined components exercised against the full component list, applying the pod’s counting rules.
- `system` counts distinct pod-defined acceptance criteria exercised against the full criteria list, applying the pod’s counting rules. A failed test can still exercise a component or criterion.

## System test endpoints

- `POST /test/setup` accepts a caller-supplied `session_id` of 1 to 200 characters and uses `{}` for starting data. Return `201` with the same ID when ready, `409` for a duplicate ID, and `500` with `source: "system"` for a preparation failure. Keep setup reachable while project resources are stopped.
- `GET /test/report?session_id=<id>` returns `200` with `session_id` and `coverage.code`. While instrumentation is unavailable, use `null` for both counts and explain why in `basis`. Return `404` for an unknown session and `409` while preparation is in progress, as required by the course PDF. Keep session state and coverage available through report retrieval. Report lookup does not run project operations.

## Watermark suite design

Give each planned case a stable ID, deterministic synthetic fixture, and expected status and response. Map cases to the pod’s components and acceptance criteria once their definitions are agreed.

1. Check system health and submit PNG and JPEG fixtures with valid survey inputs. Assert `202`, the job schema, and the job status link. Poll until completion, download the result by job ID, and verify that it decodes in the same image format. A fast job can complete between polls.
2. Submit the downloaded bytes to `/detect` and require `detected: true`, `mediaKind: "image"`, a valid confidence, and a payload matching an independently confirmed test vector for the submitted survey values. Also check `detected: false` on a known-clean image, as the project describes.
3. Verify the specified `400`, `413`, `415`, `404`, and pending-result `409` cases. Use a controlled worker or mock target for deterministic pending and failed job checks. Verify that two jobs return their own artifacts and survey payloads.

## Verify and submit

- Exercise the real HTTP flow against a local mock mounted beneath a path prefix. Verify that setup sends the generated ID and starting data, the project calls run in order, the code report is fetched after them, and tester run and report return the same JSON.
- Check failed setup forwarding, mismatched setup IDs, missing or invalid input, unknown IDs, report lookup before and during a run, concurrent and repeated run rejection, and a new ID for each new setup. Check expected errors as `pass`, mismatches as `fail`, target/communication failures as `error`, and missing coverage as `null` counts. Simulate a tester abort mid-suite and retrieve the finalized report with recorded, error, and skipped results.
- Run the existing local regression suite from `Version_0.3` with `python tester.py --quick` and `python tester.py` using the dependencies in `requirements.txt`. The full run covers legacy audio and video. Run the cross-team tester against the migrated system over HTTP to verify the new image-only workflow and stored suite report.
- Describe the three tester operations in OpenAPI 3.1.1 using the registered tester base URL. Define request, report, and error schemas when using validation or code generation. Maintain the system’s own API description for its test endpoints and the pod project operations.
- For submission, deploy the two services at distinguishable, reachable HTTP(S) bases and register both URLs, the shared pod specification URL, access instructions, and maximum tester setup, tester run, and system setup times. Tester times include all relevant target calls. Keep credentials out of URLs and the registry.
- For Phase 3, test both directions with at least one other team in the pod. Record teams, date, outcomes, and issues to fix. The check requires a working shared interface and well-formed reports. Individual cases may fail.
