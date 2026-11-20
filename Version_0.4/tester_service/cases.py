"""Black-box tests for the optional text payload and five media formats in the pod API."""

from functools import partial
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import wave

import imageio_ffmpeg
from PIL import Image

from .checks import (
    check_health, check_job_schema, check_missing_job, expect_json, expect_project_error,
    expect_status, image_file, job_link, require,
)


def wav_sample():
    output = BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "uncompressed"))
        audio.writeframes(b"\0\0" * 4000)
    return output.getvalue()


def fixture(fmt):
    if fmt in ("PNG", "JPEG"):
        return image_file(fmt)
    if fmt == "WAV":
        return "sample.wav", wav_sample(), "audio/wav"
    if fmt == "MP3":
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-f", "wav", "-i", "pipe:0",
                   "-f", "mp3", "pipe:1"]
        result = subprocess.run(command, input=wav_sample(), capture_output=True, check=True, timeout=5)
        return "sample.mp3", result.stdout, "audio/mpeg"
    path = Path(__file__).resolve().parents[1] / "testing" / "fixtures" / "crescent-moon.mp4"
    return "sample.mp4", path.read_bytes(), "video/mp4"


def validate_result(fmt, filename, data):
    if fmt in ("JPEG", "PNG"):
        try:
            with Image.open(BytesIO(data)) as image:
                require(image.format == fmt, f"Downloaded image was {image.format}; expected {fmt}.")
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise AssertionError(f"Downloaded media is not a decodable {fmt} image.") from exc
        return
    # Select the demuxer explicitly to detect a target returning another container.
    demuxer = {"WAV": "wav", "MP3": "mp3", "MP4": "mov"}[fmt]
    stream = "0:v:0" if fmt == "MP4" else "0:a:0"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / filename
        path.write_bytes(data)
        command = [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-xerror", "-protocol_whitelist", "file",
                   "-f", demuxer, "-i", str(path), "-map", stream, "-t", "0.15", "-f", "null", "-"]
        try:
            result = subprocess.run(command, capture_output=True, timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f"The downloaded {fmt} did not decode within five seconds.") from exc
        require(result.returncode == 0, f"The downloaded {fmt} could not be decoded.")


def check_detection(client, media, marked, payload):
    result = expect_json(client.request("POST", "/detect", file=media), 200, "Detect")
    require(result.get("detected") is marked, f"Expected detected={marked} for {media[0]}.")
    require(result.get("mediaKind") == media[2].split("/")[0], "Detection returned the wrong mediaKind.")
    confidence = result.get("confidence")
    require(type(confidence) in (float, int) and 0 <= confidence <= 1,
            "Detection confidence must be a number between zero and one.")
    require(result.get("payload") == payload, "Detection returned an unexpected payload.")


def submit(client, media, payload):
    fields = [("payload", payload)] if payload is not None else []
    response = client.request("POST", "/watermark-jobs", fields=fields, file=media)
    job = expect_json(response, 202, "Create job")
    check_job_schema(job, "Create job", media[2].split("/")[0])
    path = job_link(client, response.headers.get("Location"), "/watermark-jobs/" + job["id"], "Create job")
    return job["id"], path


def download(client, media, job_id, path):
    while True:
        job = expect_json(client.request("GET", path), 200, "Poll job")
        check_job_schema(job, "Poll job", media[2].split("/")[0])
        require(job["id"] == job_id, "Job ID changed during polling.")
        if job["status"] == "completed":
            break
        client.pause(0.1)
    result_path = job_link(client, job["resultUrl"], path + "/result", "Poll job")
    response = client.request("GET", result_path)
    expect_status(response, 200, "Download result")
    mime = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    require(mime == media[2], f"Downloaded Content-Type was {mime}; expected {media[2]}.")
    return media[0], response.body, media[2]


def roundtrip(client, fmt, payload):
    media = fixture(fmt)
    check_detection(client, media, False, None)
    marked = download(client, media, *submit(client, media, payload))
    validate_result(fmt, marked[0], marked[1])
    check_detection(client, marked, True, payload)
    return f"Clean {fmt} tested negative. Its marked download decoded as {fmt} and tested positive."


def isolated_jobs(client):
    media = image_file("PNG")
    first = submit(client, media, "first job")
    second = submit(client, media, "second job")
    require(first[0] != second[0], "Separate submissions returned the same job ID.")
    check_detection(client, download(client, media, *second), True, "second job")
    check_detection(client, download(client, media, *first), True, "first job")
    return "Two jobs on the same image retained their own IDs and payloads."


def malformed(client):
    for route in ("/watermark-jobs", "/detect"):
        expect_project_error(client.request("POST", route), 400, f"{route} without a file")
    response = client.request("POST", "/watermark-jobs", fields=[("payload", "x" * 65)], file=image_file("PNG"))
    expect_project_error(response, 400, "65-character payload")
    return "Missing files and a 65-character payload returned HTTP 400 with project errors."


def unsupported(client):
    for route in ("/watermark-jobs", "/detect"):
        expect_project_error(client.request("POST", route, file=image_file("GIF")), 415, f"{route} with GIF")
    return "Both upload routes rejected GIF with HTTP 415 and a project error."


CASES = (
    ("health", check_health),
    ("roundtrip-png", partial(roundtrip, fmt="PNG", payload="custom watermark")),
    ("roundtrip-jpeg", partial(roundtrip, fmt="JPEG", payload=None)),
    ("roundtrip-wav", partial(roundtrip, fmt="WAV", payload="pod-WAV")),
    ("roundtrip-mp3", partial(roundtrip, fmt="MP3", payload="\u03bc" * 64)),
    ("roundtrip-mp4", partial(roundtrip, fmt="MP4", payload="pod-MP4")),
    ("independent-jobs", isolated_jobs),
    ("malformed-upload", malformed),
    ("unsupported-media", unsupported),
    ("unknown-job", check_missing_job),
)
