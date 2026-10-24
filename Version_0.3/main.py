import flask
from flask import send_file
from io import BytesIO
from pathlib import Path

from detection import detect_watermark_bytes
from watermark import SUPPORTED_EXTENSIONS, WatermarkError, watermark_bytes

app = flask.Flask(__name__)

# The application currently holds one processed upload at a time.
temp_file = None
original_name = None


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
        detected = detect_watermark_bytes(file.read(), file.filename)
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


@app.route("/insert_submit", methods=["POST"])
def submitFile():
    global temp_file, original_name
    # A failed new upload must not leave an earlier download available.
    temp_file = None
    original_name = None
    files = flask.request.files.getlist("user-file")
    if len(files) != 1 or not files[0].filename:
        return "Please upload exactly one file.", 400

    file = files[0]
    if Path(file.filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return flask.render_template("unsupportedFile.html"), 400

    try:
        marked = watermark_bytes(file.read(), file.filename)
    except WatermarkError as exc:
        return str(exc), 400, {"Content-Type": "text/plain; charset=utf-8"}

    temp_file = marked
    original_name = file.filename
    return flask.render_template("postWatermark.html", file_type=Path(file.filename).suffix)


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
