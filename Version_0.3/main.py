import flask
from flask import send_file
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile

import imageio_ffmpeg

from detection import detect_watermark_bytes
from tester_runner import get_tester_run, start_tester_run
from watermark import SUPPORTED_EXTENSIONS, VIDEO_EXTENSIONS, WatermarkError, watermark_bytes

app = flask.Flask(__name__, static_folder="assets", static_url_path="/assets")
# Accept a full 500 MiB file plus 1 MiB for multipart form data. Keep the
# request limit in sync with the Elastic Beanstalk nginx configuration.
app.config["MAX_FILE_SIZE"] = 500 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_FILE_SIZE"] + 1024 * 1024

# The application currently holds one processed upload at a time.
temp_file = None
original_name = None
video_preview = None


def _video_preview_bytes(data, extension):
    """Convert marked video to H.264/AAC MP4 for browser playback."""
    with tempfile.TemporaryDirectory(prefix="video-preview-") as directory:
        source = Path(directory) / ("source" + extension)
        target = Path(directory) / "preview.mp4"
        source.write_bytes(data)
        subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner",
             "-loglevel", "error", "-y", "-i", str(source),
             "-map", "0:v:0", "-map", "0:a:0?",
             "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-preset", "fast", "-crf", "20", "-c:a", "aac",
             "-movflags", "+faststart", str(target)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, check=True, timeout=180,
        )
        return target.read_bytes()


@app.errorhandler(413)
def upload_too_large(_error):
    limit_mib = app.config["MAX_FILE_SIZE"] / (1024 * 1024)
    return (
        f"Upload too large. The maximum file size is {limit_mib:g} MiB. "
        "Choose a smaller file and try again.",
        413,
        {"Content-Type": "text/plain; charset=utf-8"},
    )


def _read_uploaded_file(upload):
    limit = app.config["MAX_FILE_SIZE"]
    data = upload.read(limit + 1)
    if len(data) > limit:
        flask.abort(413)
    return data


@app.route("/", methods=["GET", "POST"])
def start():
    return flask.redirect("/insert")


@app.route("/insert", methods=["GET"])
def insert():
    return flask.render_template("insert.html")


@app.route("/detection", methods=["GET", "POST"])
def detection():
    if flask.request.method == "GET":
        return flask.render_template("detectionMode.html")

    files = flask.request.files.getlist("user-file")
    if len(files) != 1 or not files[0].filename:
        return flask.render_template(
            "detectionMode.html", error="Please upload exactly one file to check."
        ), 400

    file = files[0]
    if Path(file.filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return flask.render_template(
            "detectionMode.html",
            error="Unsupported file type. Choose one of the supported formats listed above.",
        ), 400

    try:
        detected = detect_watermark_bytes(_read_uploaded_file(file), file.filename)
    except WatermarkError as exc:
        app.logger.warning("Watermark detection failed: %s", exc)
        return flask.render_template(
            "detectionMode.html",
            error="Unable to check this file. Please choose a valid file in a supported format.",
        ), 400

    endpoint = "watermark_found" if detected else "watermark_not_found"
    return flask.redirect(flask.url_for(endpoint), code=303)


@app.route("/detection/found", methods=["GET"])
def watermark_found():
    return flask.render_template("watermarkFound.html")


@app.route("/detection/not-found", methods=["GET"])
def watermark_not_found():
    return flask.render_template("watermarkNotFound.html")


@app.route("/tester", methods=["GET"])
def tester_mode():
    run = None
    run_id = flask.request.args.get("run")
    if run_id:
        try:
            run = get_tester_run(run_id)
        except (ValueError, FileNotFoundError):
            flask.abort(404)
    response = flask.make_response(flask.render_template("testerMode.html", run=run))
    response.cache_control.no_store = True
    return response


@app.route("/tester/run", methods=["POST"])
def tester_run():
    mode = flask.request.form.get("mode", "full")
    if mode not in {"full", "quick"}:
        return flask.render_template(
            "testerMode.html", run=None, error="Choose full or quick tester mode."
        ), 400
    try:
        run_id = start_tester_run(mode)
    except (OSError, RuntimeError):
        app.logger.exception("Unable to start the automatic tester")
        return flask.render_template(
            "testerMode.html", run=None,
            error="The tester could not start. Try again shortly and check the server logs if it continues.",
        ), 503
    return flask.redirect(flask.url_for("tester_mode", run=run_id), code=303)


@app.route("/tester/report/<run_id>", methods=["GET"])
def tester_report(run_id):
    try:
        run = get_tester_run(run_id)
    except (ValueError, FileNotFoundError):
        flask.abort(404)
    response = flask.jsonify(run)
    response.headers["Content-Disposition"] = f'attachment; filename="tester-{run_id}.json"'
    response.cache_control.no_store = True
    return response


@app.route("/insert_submit", methods=["POST"])
def submitFile():
    global temp_file, original_name, video_preview
    # A failed new upload must not leave an earlier download available.
    temp_file = None
    original_name = None
    video_preview = None
    files = flask.request.files.getlist("user-file")
    if len(files) != 1 or not files[0].filename:
        return "Please upload exactly one file.", 400

    file = files[0]
    if Path(file.filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return flask.render_template("unsupportedFile.html"), 400

    try:
        marked = watermark_bytes(_read_uploaded_file(file), file.filename)
    except WatermarkError as exc:
        return str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"}

    temp_file = marked
    original_name = file.filename
    extension = Path(file.filename).suffix.lower()
    return flask.render_template(
        "postWatermark.html", file_type=extension,
        is_image=extension in {".png", ".jpg", ".jpeg"},
        is_audio=extension in {".m4a", ".mp3", ".wav"},
        is_video=extension in VIDEO_EXTENSIONS,
    )


@app.route("/preview", methods=["GET"])
def preview():
    global video_preview
    # Serve processed media inline without caching an earlier upload.
    mime_types = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    }
    extension = Path(original_name or "").suffix.lower()
    if temp_file is None or extension not in (mime_types.keys() | VIDEO_EXTENSIONS):
        flask.abort(404)
        
    data = temp_file
    preview_name = original_name
    if extension in VIDEO_EXTENSIONS:
        if video_preview is None:
            try:
                video_preview = _video_preview_bytes(temp_file, extension)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                app.logger.exception("Unable to prepare video preview")
                return "Video preview unavailable. Use Download to save the watermarked file.", 503
        data = video_preview
        preview_name = "preview.mp4"
    response = send_file(
        BytesIO(data), as_attachment=False, download_name=preview_name,
        mimetype="video/mp4" if extension in VIDEO_EXTENSIONS else mime_types[extension],
    )
    response.cache_control.no_store = True
    return response


@app.route("/download", methods=["GET"])
def download():
    if temp_file is None:
        return "Upload and watermark a file before downloading.", 400
    return send_file(
        BytesIO(temp_file),
        as_attachment=True,
        download_name=original_name,
    )


if __name__ == "__main__":
    app.run(debug=True)
