"""Shared HTTP response assertions and deterministic image fixtures."""

from datetime import datetime
from io import BytesIO
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from PIL import Image


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


def check_job_schema(job, operation, media_kind="image"):
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
    require(job.get("mediaKind") == media_kind, f"{operation} mediaKind must be {media_kind}.")

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


def check_health(client):
    value = expect_json(client.request("GET", "/health"), 200, "Health check")
    require(isinstance(value.get("status"), str) and value["status"].strip(),
            "Health check did not return a nonempty status.")
    return "Health endpoint returned HTTP 200 with a status."


def check_missing_job(client):
    path = f"/watermark-jobs/{uuid4()}"
    expect_project_error(client.request("GET", path), 404, "Poll unknown job")
    expect_project_error(client.request("GET", f"{path}/result"), 404, "Download unknown result")
    return "Status and result endpoints returned HTTP 404 for an unknown job UUID."
