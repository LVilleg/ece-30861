import flask
from flask import render_template

app = flask.Flask(__name__)

@app.route("/", methods=['GET','POST'])
def start():
    return flask.redirect("/insert")

@app.route("/insert", methods=['GET'])
def insert():
    return flask.render_template("insert.html")

@app.route("/insert_submit", methods=['POST'])
def submitFile():
    #gets the uploaded file from insert.html

    file = flask.request.files["user-file"]

    #valid file types can add to or remove
    allowed_images = (".png", ".jpeg")
    allowed_audio = (".mp3", ".wav")
    allowed_video = (".mp4")

    if file.filename.lower().endswith(allowed_images):
        return flask.render_template("postWatermark.html")
    elif file.filename.lower().endswith(allowed_audio):
        return flask.render_template("postWatermark.html")
    elif file.filename.lower().endswith(allowed_video):
        return flask.render_template("postWatermark.html")
    else:
        return "Invalid file type"
    #print file type and spit the file back to user
    #send_from_directory or send_file check usecase

@app.route("/download")
def download():
    return "Placeholder"


if __name__== "__main__":
    app.run(debug=True)