import flask
from flask import render_template, send_file, send_from_directory
from io import BytesIO
from pathlib import Path

app = flask.Flask(__name__)

#there is definitly a better way to do this
temp_file = None
original_name = None

@app.route("/", methods=['GET','POST'])
def start():
    return flask.redirect("/insert")

@app.route("/insert", methods=['GET'])
def insert():
    return flask.render_template("insert.html")

@app.route("/insert_submit", methods=['POST'])
def submitFile():
    global temp_file, original_name
    #gets the uploaded file from insert.html

    file = flask.request.files["user-file"]

    #valid file types can add to or remove
    allowed_images = (".png", ".jpeg")
    allowed_audio = (".mp3", ".wav")
    allowed_video = (".mp4")

    #This entire bit is made to do stuff with later down the line
    #it currently all does the same thing
    if file.filename.lower().endswith(allowed_images):
        temp_file = file.read()
        original_name = file.filename
        return flask.render_template("postWatermark.html",file_type=Path(file.filename).suffix)
    elif file.filename.lower().endswith(allowed_audio):
        temp_file = file.read()
        original_name = file.filename
        return flask.render_template("postWatermark.html",file_type=Path(file.filename).suffix)
    elif file.filename.lower().endswith(allowed_video):
        temp_file = file.read()
        original_name = file.filename
        return flask.render_template("postWatermark.html",file_type=Path(file.filename).suffix)
    else:
        return "Invalid file type"

@app.route("/download", methods=["GET"])
def download():
    return send_file(
        BytesIO(temp_file),
        as_attachment=True,
        download_name=original_name
    )
    #I could technically just say download but this keeps the typing the same for now.


if __name__== "__main__":
    app.run(debug=True)