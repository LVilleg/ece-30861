"""Ordered black-box checks for the project API in project.yaml."""

from datetime import datetime
from io import BytesIO
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from PIL import Image


SURVEY_VECTORS = (
    ("PNG", "both", ("stable_diffusion", "firefly"), "03020305000059e8ed687f6d5776"),
    ("JPEG", "generated", ("openai", "google", "other"), "01030106fe000b30df3242e8f87f"),
)
# The PNG vector is published in project.yaml. The JPEG vector was generated
# independently with zlib.crc32 and the reference reedsolo RSCodec(4) convention.

IMAGE_FILES = {
    "PNG": ("sample.png", "image/png", (45, 70, 90)),
    "JPEG": ("sample.jpg", "image/jpeg", (130, 92, 80)),
    "GIF": ("sample.gif", "image/gif", (64, 64, 64)),
}
PENDING_STATUSES = ("queued", "processing")


class TargetJobError(Exception):
    """A target reported that its background job failed."""


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def image_file(image_format):
    filename, mime, color = IMAGE_FILES[image_format]
    with BytesIO() as output:
        Image.new("RGB", (128, 96), color).save(output, format=image_format)
        return filename, output.getvalue(), mime


def expect_status(response, status, operation):
    require(response.status_code == status,
            f"{operation} returned HTTP {response.status_code}; expected {status}.")


def expect_json(response, status, operation):
    expect_status(response, status, operation)
    try:
        value = response.json()
    except ValueError as exc:
        raise AssertionError(f"{operation} did not return valid JSON.") from exc
    require(isinstance(value, dict), f"{operation} did not return a JSON object.")
    return value


def expect_project_error(response, status, operation):
    value = expect_json(response, status, operation)
    for key in ("code", "message"):
        require(isinstance(value.get(key), str) and value[key].strip(),
                f"{operation} error is missing a nonempty {key}.")


def check_job_schema(job, operation):
    job_id = job.get("id")
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise AssertionError(f"{operation} did not return a job UUID.") from exc

    created_at = job.get("createdAt")
    try:
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise AssertionError(f"{operation} did not return a valid createdAt timestamp.") from exc
    require(timestamp.tzinfo is not None, f"{operation} createdAt has no time zone.")
    require(job.get("mediaKind") == "image", f"{operation} mediaKind must be image.")

    status = job.get("status")
    require(status in (*PENDING_STATUSES, "completed", "failed"),
            f"{operation} returned an invalid job status.")
    if status == "failed":
        error = job.get("error")
        message = error.strip()[:200] if isinstance(error, str) else ""
        raise TargetJobError(f"Target job {job_id} failed. {message}".rstrip())
    if status == "completed":
        require(isinstance(job.get("resultUrl"), str) and job["resultUrl"].strip(),
                f"{operation} completed job is missing its resultUrl.")


def job_link(client, link, expected_path, operation):
    require(isinstance(link, str) and link.strip(), f"{operation} is missing its job link.")
    try:
        path = client.link_path(link)
    except ValueError as exc:
        raise AssertionError(f"{operation} returned an invalid job link.") from exc
    require(urlsplit(path).path == expected_path,
            f"{operation} job link does not address the returned job.")
    return path


def submit_job(client, upload, status, tools):
    fields = [("ai_status", status)]
    fields.extend(("ai_tools", tool) for tool in tools)
    response = client.request("POST", "/watermark-jobs", fields=fields, file=upload)
    job = expect_json(response, 202, "Create job")
    check_job_schema(job, "Create job")
    path = f"/watermark-jobs/{job['id']}"
    status_path = job_link(client, response.headers.get("Location"), path, "Create job")
    return job, status_path


def download_job(client, created_job, status_path, image_format, upload):
    job_id = created_job["id"]
    while True:
        job = expect_json(client.request("GET", status_path), 200, "Poll job")
        check_job_schema(job, "Poll job")
        require(job["id"] == job_id, "Poll job returned the wrong job ID.")
        if job["status"] == "completed":
            break
        client.pause(0.2)

    path = job_link(client, job["resultUrl"], f"/watermark-jobs/{job_id}/result", "Poll job")
    response = client.request("GET", path)
    expect_status(response, 200, "Download result")
    name, original, mime = upload
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    require(content_type == mime, f"Download result did not return {mime}.")
    try:
        with Image.open(BytesIO(response.body)) as image:
            require(image.format == image_format, f"Download result is not {image_format}.")
            image.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise AssertionError("Download result did not contain a decodable image.") from exc
    require(response.body != original, "Downloaded image has the same bytes as the clean upload.")
    return name, response.body, mime


def check_detection(client, upload, expected_payload):
    result = expect_json(client.request("POST", "/detect", file=upload), 200, "Detect watermark")
    detected = expected_payload is not None
    require(result.get("detected") is detected, f"Detection did not return detected={detected}.")
    require(result.get("mediaKind") == "image", "Detection mediaKind must be image.")
    confidence = result.get("confidence")
    require(type(confidence) in (int, float) and 0 <= confidence <= 1,
            "Detection confidence must be a number between 0 and 1.")
    require(result.get("payload") == expected_payload,
            "Detection payload did not match the expected survey vector." if detected
            else "Detection returned a payload for a clean image.")


def check_health(client):
    value = expect_json(client.request("GET", "/health"), 200, "Health check")
    require(isinstance(value.get("status"), str) and value["status"].strip(),
            "Health check did not return a nonempty status.")
    return "Health endpoint returned HTTP 200 with a status."


def check_roundtrip(client, vector):
    image_format, status, tools, expected_payload = vector
    upload = image_file(image_format)
    job, status_path = submit_job(client, upload, status, tools)
    output = download_job(client, job, status_path, image_format, upload)
    check_detection(client, output, expected_payload)
    return f"{image_format} result decoded in its original format and carried the expected survey payload."


def check_png_roundtrip(client):
    return check_roundtrip(client, SURVEY_VECTORS[0])


def check_jpeg_roundtrip(client):
    return check_roundtrip(client, SURVEY_VECTORS[1])


def check_job_isolation(client):
    # Submit both jobs before polling either. Fetch the second result first to
    # expose implementations that overwrite a single shared download slot.
    submitted = []
    for image_format, status, tools, expected_payload in SURVEY_VECTORS:
        upload = image_file(image_format)
        job, status_path = submit_job(client, upload, status, tools)
        submitted.append((job, status_path, image_format, upload, expected_payload))
    require(submitted[0][0]["id"] != submitted[1][0]["id"], "Separate uploads returned the same job ID.")

    for job, status_path, image_format, upload, expected_payload in reversed(submitted):
        output = download_job(client, job, status_path, image_format, upload)
        check_detection(client, output, expected_payload)
    return "Two submitted jobs retained separate IDs, result formats, and survey payloads."


def check_clean_images(client):
    for image_format, *_ in SURVEY_VECTORS:
        check_detection(client, image_file(image_format), None)
    return "Clean PNG and JPEG fixtures returned detected=false without a payload."


def check_malformed_requests(client):
    upload = image_file("PNG")
    valid_status = ("ai_status", "generated")
    tool = ("ai_tools", "openai")
    malformed = (
        ((), "missing survey fields"),
        ((valid_status, tool, tool), "duplicate tools"),
        ((("ai_status", "invalid"), tool), "invalid survey status"),
    )
    for fields, reason in malformed:
        response = client.request("POST", "/watermark-jobs", fields=fields, file=upload)
        expect_project_error(response, 400, f"Create job with {reason}")
    response = client.request("POST", "/detect")
    expect_project_error(response, 400, "Detect without a file")
    return "Malformed uploads returned HTTP 400 with project errors."


def check_unsupported_media(client):
    upload = image_file("GIF")
    fields = (("ai_status", "generated"), ("ai_tools", "unknown"))
    response = client.request("POST", "/watermark-jobs", fields=fields, file=upload)
    expect_project_error(response, 415, "Create job with GIF")
    response = client.request("POST", "/detect", file=upload)
    expect_project_error(response, 415, "Detect on GIF")
    return "Both project upload endpoints rejected unsupported GIF media with HTTP 415."


def check_missing_job(client):
    path = f"/watermark-jobs/{uuid4()}"
    expect_project_error(client.request("GET", path), 404, "Poll unknown job")
    expect_project_error(client.request("GET", f"{path}/result"), 404, "Download unknown result")
    return "Status and result endpoints returned HTTP 404 for an unknown job UUID."


CASES = (
    ("T-01", check_health),
    ("T-02", check_png_roundtrip),
    ("T-03", check_jpeg_roundtrip),
    ("T-04", check_job_isolation),
    ("T-05", check_clean_images),
    ("T-06", check_malformed_requests),
    ("T-07", check_unsupported_media),
    ("T-08", check_missing_job),
)
