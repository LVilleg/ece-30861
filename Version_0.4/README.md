# AI Media Watermarking Service — Version 0.4

## Run the system and tester on separate URLs

The system accepts media over HTTP. The tester accepts a system base URL during setup, then sends its tests to that URL over HTTP. The two services can run on different machines. Install the dependencies once from this directory in PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start the **system** in one terminal.

```powershell
.\.venv\Scripts\python.exe -m uvicorn system_service.api:app --host 0.0.0.0 --port 8000
```

Start the **tester** in a second terminal. Set `POD_SYSTEM_URLS` before startup to select the systems it may contact. It accepts a comma-separated list of exact HTTP or HTTPS base URLs, including deployment prefixes. Set `OWN_SYSTEM_URLS` to the addresses of your team's systems to identify self-tests.

```powershell
$env:POD_SYSTEM_URLS = 'http://127.0.0.1:8000'
$env:OWN_SYSTEM_URLS = 'http://127.0.0.1:8000'
.\.venv\Scripts\python.exe -m uvicorn tester_service.api:app --host 0.0.0.0 --port 8001
```

Open `http://localhost:8000/docs` for the system and `http://localhost:8001/docs` for the tester. From another machine, replace `localhost` with the hosting machine's reachable hostname or IP address. A tester on a separate machine also needs that reachable system URL in its configuration and in the setup request.

| Service and method | Path relative to the service base | HTTP behavior |
| --- | --- | --- |
| System GET | `/health` | Returns the service status. |
| System POST | `/watermark-jobs` | Accepts multipart `file` and optional text `payload` of up to 64 characters. Returns 202 and a job URL in `Location`. |
| System GET | `/watermark-jobs/{jobId}` | Poll for `completed` or `failed`. |
| System GET | `/watermark-jobs/{jobId}/result` | Downloads the marked file in its original format. Returns 409 while pending. |
| System POST | `/detect` | Accepts multipart `file`. Returns detection, confidence, media kind and any recovered payload. |
| System POST / GET | `/test/setup`, `/test/report?session_id=…` | Prepares a supplied session ID and returns that session's code coverage. |
| Tester POST | `/test/setup` | Accepts JSON `target_url` and returns 201 with a new session ID after the target confirms readiness. |
| Tester POST | `/test/run` | Accepts JSON `session_id`, runs one suite and stores the report. Each session supports one run. |
| Tester GET | `/test/report?session_id=…` | Returns the stored report, including available results after an abort. Returns 409 while pending. |

### Exercise a target through the tester

Run these commands in a third terminal. For another team's service, add its base URL to the tester's `POD_SYSTEM_URLS` before starting the tester and use that URL for `$systemUrl` below. Keep `$testerUrl` pointed at your tester.

```powershell
$systemUrl = 'http://127.0.0.1:8000'
$testerUrl = 'http://127.0.0.1:8001'
$setup = Invoke-RestMethod -Method Post -Uri "$testerUrl/test/setup" -ContentType 'application/json' -Body (@{target_url = $systemUrl} | ConvertTo-Json)
$runBody = @{session_id = $setup.session_id} | ConvertTo-Json
$report = Invoke-RestMethod -Method Post -Uri "$testerUrl/test/run" -ContentType 'application/json' -Body $runBody
$report.results | Format-Table test_id, outcome, message
Invoke-RestMethod -Uri "$testerUrl/test/report?session_id=$($setup.session_id)" | ConvertTo-Json -Depth 8
```

A completed suite returns HTTP 200 even when individual cases fail. A tester abort returns 500 and its report is available at the same report URL. Each report has one result for each planned case. Coverage reports identify available measurements. The supplied contract leaves code instrumentation, the component inventory, the acceptance-criterion mapping and the team's milestone design mapping unspecified. Unavailable counts are null, with a reason. Self-tests also include model coverage.

### System contract and storage

The system accepts PNG, JPEG, WAV, MP3 and MP4, with a maximum input file size of 100,000,000 bytes. The watermark carries optional text in the media container. Stripping the watermark metadata can remove it. For a direct upload, use `curl.exe -F "file=@photo.png" -F "payload=my label" http://localhost:8000/watermark-jobs`. Follow the returned job URL and, once completed, download its `resultUrl`. Send the downloaded bytes with `curl.exe -F "file=@marked.png" http://localhost:8000/detect`.

The tester sends `X-Test-Session-Id` with project requests so the target can associate jobs with the prepared session. Standalone clients can omit it. Set `SYSTEM_STATE_DB` and `TESTER_STATE_DB` to writable SQLite file paths for durable jobs and reports. Retain those files when restarting either process.

| Location | Responsibility |
| --- | --- |
| `system_service/` | FastAPI system, durable job processing and media encoding/detection. |
| `system_service/openapi.yaml` | System contract from the supplied `project(1).yaml`. Its two `components` sections are merged so every schema reference resolves. |
| `tester_service/` | FastAPI tester, target HTTP client, ordered cases and stored reports. |
| `tester.openapi.yaml` | Tester setup, run and report contract. |
| `application.py` | Combined server for a single-host deployment, with the browser and the two API prefixes. |
| `testing/` | HTTP integration tests, media and browser checks, and bundled fixtures. |

From this directory, run `.\.venv\Scripts\python.exe -m unittest discover -v` to check the FastAPI lifecycle, the two-service HTTP flow, and the earlier survey protocol regressions. Run `.\.venv\Scripts\python.exe tester.py` for the full browser and media regression suite.

### Run both APIs with the existing website

As an alternative launch mode, run `.\.venv\Scripts\python.exe main.py` on port 8000. The browser opens at `/`. The system API lives under `/system` and the tester API under `/tester`. Each has its own `/docs` and `/openapi.json`. In the PowerShell example above, use `http://127.0.0.1:8000/system` for the system and `http://127.0.0.1:8000/tester` for the tester. Include the prefixed system URL in the tester configuration if overriding the defaults.

For an ASGI host, launch `uvicorn application:app`. The included Elastic Beanstalk `Procfile` launches it on the configured port. Existing WSGI hosts can load `application:application`. Package the service directories, YAML files, requirements, templates, assets and test fixtures with the entry point. Exclude the local virtual environment and runtime databases.

## Legacy browser and CLI

This Flask application adds a basic, machine-detectable watermark to images,
audio, and video. Detection mode checks for the same watermark and directs the
user to a page showing whether it was found.

Each upload request accepts one file. The marks are visible in
images and video and may be audible in audio. Detection identifies this
service's fixed marker; it does not determine whether arbitrary content was
generated by AI.

## Setup and startup

The code requires Python 3.10 or newer. The development checks were run with
Python 3.13. Run these commands in PowerShell:

```powershell
Set-Location "C:\Users\PNF40\.vscode\ece-30861\Version_0.4"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in a browser. Stop the server
with `Ctrl+C`. Calling the virtual environment's Python directly avoids needing
to activate it first.

`main.py` starts the combined Uvicorn server on port 8000 and serves the Flask browser app alongside both FastAPI services.

| Requirement | Purpose |
| --- | --- |
| Flask >= 3.1 | Web routes, uploads, HTML templates, redirects, and downloads. |
| Pillow >= 10.0 | Reading, marking, and saving images. |
| NumPy >= 1.24 | Binary marker processing and audio signal calculations. |
| imageio-ffmpeg >= 0.6.0 | Locating FFmpeg for audio/video decoding and encoding. |
| reedsolo >= 1.7.0, < 2 | Historical survey codec. Reed–Solomon parity and correction for survey payloads. |

The standard Windows wheel for `imageio-ffmpeg` includes FFmpeg. If a different
installation does not supply the executable, `IMAGEIO_FFMPEG_EXE` can point to
an installed FFmpeg executable. Audio/video processing also needs a writable
temporary directory.

## Elastic Beanstalk upload limit

Both browser upload modes accept files up to **500 MiB (524,288,000 bytes)**. This also
accepts a 500 MB file measured as 500,000,000 bytes. The request-body limit is
**501 MiB (525,336,576 bytes)** to allow 1 MiB for multipart form data, so a file
exactly at the 500 MiB cap can be uploaded.

- `main.py` sets `MAX_FILE_SIZE` to `500 * 1024 * 1024` and Flask's
  `MAX_CONTENT_LENGTH` to `MAX_FILE_SIZE + 1024 * 1024`. Both upload routes reject
  files exceeding `MAX_FILE_SIZE` with HTTP 413 before media processing.
- `.platform/nginx/conf.d/client_max_body_size.conf` sets nginx's
  `client_max_body_size` to `501M` for Elastic Beanstalk's default nginx proxy
  on Amazon Linux 2 and Amazon Linux 2023.

Deploy a new application version to apply the change. Put `.platform` at the
**root of the deployment ZIP**, alongside `application.py`, `main.py`, and
`requirements.txt`; do not put a containing `Version_0.4` directory above them
in the archive. Ensure the dot-prefixed `.platform` directory is included.

To change the limit later, update the file cap and both request limits, keeping
the multipart allowance, then redeploy. This configuration
uses nginx; an environment configured with Apache needs its proxy setting
updated separately. See the [AWS proxy configuration guide](https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/platforms-linux-extend.proxy.html).

Oversized requests return HTTP 413. Flask provides a readable error when the
request reaches it; requests rejected earlier by nginx use nginx's error page.
On an instance, `sudo nginx -t` validates the active configuration and
`sudo nginx -T` displays it so you can confirm `client_max_body_size 501M;`.

## Supported files

Extension checks are case insensitive. The contents must also be valid media
that Pillow or FFmpeg can decode. A supported extension alone does not make
damaged or non-media contents valid.

| Media | Accepted extensions |
| --- | --- |
| PNG image | `.png` |
| JPEG image | `.jpeg`, `.jpg` |
| Audio | `.m4a`, `.mp3`, `.wav` |
| MP4, AVI, or MOV video | `.mp4`, `.avi`, `.mov` |
| MPEG-2 video | `.mpg`, `.mpeg`, `.mpeg2`, `.mpeg-2`, `.m2v` |

Images and video must be at least 8 by 8 pixels to receive the visual marker.
Animated PNG inputs are treated as still images. Audio shorter than 1.28 seconds
is padded so the entire marker fits.

## Using the website

To watermark a file:

1. Open the watermark page at `/system`.
2. Choose one supported file and select **Add watermark**.
3. On the output page, select **Download** to save the processed copy.

The download keeps the uploaded filename. Processing does not replace the
original file on the user's computer. An unsupported extension displays the
unsupported-file page. An empty, damaged, or otherwise unreadable file produces
an error instead of a download.

To check a file:

1. Select **Detect watermark** on the watermark page.
2. On `/detection`, choose one supported file and select **Detect watermark**.
3. The service checks the file and redirects to **Watermark found** or
   **No watermark found**.

Both result pages provide **Check another file** and **Back to watermark mode**
links. Missing, multiple, unsupported, or unreadable uploads remain on the
detection form with an error. An error is never reported as a negative match.
Detection leaves the current watermarked download unchanged.

## Files and responsibilities

| File | Responsibility |
| --- | --- |
| `application.py` | Serves the browser and both APIs through ASGI or WSGI. |
| `main.py` | Creates the Flask app, handles uploads, selects result pages, and serves the current download and local assets. |
| `watermark.py` | Embeds the marker, implements its underlying detection checks, and provides a watermarking command line interface. |
| `detection.py` | Provides the dedicated detection API and command line interface, using `watermark.has_watermark()`. |
| `tester.py` | Runs the local suite and writes optional JSON reports. |
| `testing/` | Local test definitions and bundled stock image and video fixtures. |
| `tester_runner.py` | Runs website-triggered checks in an isolated background process and stores temporary progress/results. |
| `requirements.txt` | Lists the third-party Python dependencies. |
| `templates/base.html` | Shared page layout, header, navigation, and local stylesheet link. |
| `templates/system.html` | Watermark upload form and switch to detection mode. |
| `templates/postWatermark.html` | Successful watermark output page with the download button. |
| `templates/unsupportedFile.html` | Error page for unsupported watermark uploads. |
| `templates/detectionMode.html` | Detection form, validation errors, and shared layout for detection results. |
| `templates/watermarkFound.html` | Positive detection result; extends `detectionMode.html`. |
| `templates/watermarkNotFound.html` | Negative detection result; extends `detectionMode.html`. |
| `templates/testerMode.html` | Automatic tester controls, progress, summary, and report links. |
| `assets/style.css` | Shared typography, panels, buttons, responsive layout, and background styling. |
| `assets/` | Local assets served by Flask at `/assets/`, including the supplied background image. |

All pages use the shared layout and stylesheet. The full-page background is
`Black and Blue Background with Wavy Lines - Pawel Czerwinski.png`, served from
the local `assets` folder. Panels keep forms and results readable over the image.
Page styling does not require the external W3.CSS stylesheet.

## Web routes

Upload forms use `multipart/form-data` with the file field named `user-file`.

| Method | Route | Behavior |
| --- | --- | --- |
| GET, POST | `/` | Redirects to `/system`. |
| GET | `/system` | Displays the watermark form. |
| POST | `/insert_submit` | Marks one upload and displays `postWatermark.html`. |
| GET | `/download` | Downloads the current marked bytes using the uploaded filename. |
| GET | `/detection` | Displays the detection form. |
| POST | `/detection` | Calls `detect_watermark_bytes()` and redirects with HTTP 303 to the matching result URL. |
| GET | `/detection/found` | Displays `watermarkFound.html`. |
| GET | `/detection/not-found` | Displays `watermarkNotFound.html`. |
| GET | `/tester` | Displays automatic tester controls or the run selected by `?run=ID`. |
| POST | `/tester/run` | Starts full or quick checks and redirects to the run's status page. |
| GET | `/tester/report/<run_id>` | Downloads the run's current JSON summary. |

Invalid uploads return HTTP 400. Requesting `/download` before a successful
watermark upload also returns HTTP 400. Unsupported watermark uploads render
`unsupportedFile.html`; unsupported detection uploads show an error within
`detectionMode.html`.

Requests above the configured upload-request limit return HTTP 413.

The two result URLs display fixed outcome pages. They do not run detection
again or store a detection history. The service chooses the appropriate URL
after processing the upload.

## Python interfaces

Import these functions while running from the project directory, or make the
project directory available on Python's module search path.

| Function | Return value | Behavior |
| --- | --- | --- |
| `watermark.watermark_bytes(data, filename)` | `bytes` | Returns a marked copy. The filename supplies the output format. |
| `watermark.watermark_file(input_path, output_path)` | `Path` | Saves a marked copy without replacing the input or an existing output. |
| `watermark.has_watermark(data, filename)` | `bool` | Performs the shared image, audio, or video marker check. |
| `detection.detect_watermark_bytes(data, filename)` | `bool` | Checks uploaded bytes without changing them. |
| `detection.detect_watermark_file(file_path)` | `bool` | Reads and checks a local file without changing it. |

For all detection functions, `True` means the service's marker was found and
`False` means no matching marker was found. Invalid or unsupported media raises
`WatermarkError`, a subclass of `ValueError`. File-based functions can also raise
`OSError`, including `FileNotFoundError` and `FileExistsError` where applicable.

For `watermark_file()`, input and output extensions must match, with `.jpg` and
`.jpeg` allowed interchangeably. Choose a new output filename for each run.

Example using local files:

```python
from detection import detect_watermark_file
from watermark import WatermarkError, watermark_file

# Supply an existing supported image as input.png.
try:
    output = watermark_file("input.png", "watermarked.png")
    found = detect_watermark_file(output)
    print("Watermark found" if found else "No watermark found")
except (WatermarkError, OSError) as error:
    print(f"Unable to process file: {error}")
```

Example using bytes:

```python
from pathlib import Path
from detection import detect_watermark_bytes
from watermark import watermark_bytes

original = Path("input.png").read_bytes()
marked = watermark_bytes(original, "input.png")
found = detect_watermark_bytes(marked, "input.png")  # True for this marked copy.
```

## Command line use

Run from the project directory, substituting your own input and output names:

```powershell
.\.venv\Scripts\python.exe watermark.py input.png watermarked.png
.\.venv\Scripts\python.exe detection.py watermarked.png
```

The same commands work with the other supported extensions. The watermarking
command prints the output path. Detection prints either `Watermark found` or
`No watermark found`.

| Command | Exit status |
| --- | --- |
| `watermark.py` | `0` on success; `1` for processing/file errors; `2` for invalid command arguments. |
| `detection.py` | `0` for both valid detection outcomes; `2` for processing/file errors or invalid command arguments. |

Use the Python detection functions when a program needs the Boolean result.
The detection command's exit status does not distinguish found from not found.

## How the marker works

All formats use one fixed 64-bit identifier, `a6d35c9e71b48f02`. It does not
contain a user ID, filename, timestamp, or unique record for each upload.

**Images.** The bits form an opaque black-and-white 8-by-8 grid in the upper-left
corner. Cells are 1 to 4 pixels wide, producing a marker up to 32 by 32 pixels.
EXIF orientation is applied before marking. PNG transparency is preserved
outside the marker. JPEG output uses quality 95 with chroma subsampling disabled.
Detection tries each cell size, requires at least 62 of 64 matching bits, and
checks that the white and black cells have sufficient brightness contrast.

**Audio.** FFmpeg decodes the input to 48 kHz, 16-bit PCM. Each of the first
64 symbols lasts 20 ms: a 1200 Hz tone represents zero and a 2200 Hz tone
represents one. Existing carrier components are reduced in each symbol before
the marker is mixed in. The complete sequence occupies the first 1.28 seconds.
Detection averages the channels, compares tone energies, and requires the exact
bit sequence plus energy checks. The marked audio is re-encoded as AAC for M4A,
MP3 for MP3, or 16-bit PCM for WAV.

**Video.** FFmpeg draws the visual grid on every frame. Detection checks the
decoded first frame. Processing selects the first video stream and, when
available, the first audio stream. Odd dimensions are padded to even dimensions.

| Output | Video encoding | Audio encoding |
| --- | --- | --- |
| MP4, MOV | H.264 | AAC |
| AVI | MPEG-4 | MP3 |
| MPEG-2 program stream | MPEG-2 at 25 fps | MP2 |
| `.m2v` | MPEG-2 at 25 fps | None; video-only output |

Re-encoding can change media quality, size, timing, and metadata. Keeping the
file extension does not preserve the original codec settings.

## Current limitations

- The marker can be removed or copied. A match identifies the pattern and is
  not cryptographic proof of authorship, ownership, or AI generation. A negative
  result does not establish that a file was created by a human.
- Cropping, resizing, trimming, compression, and other edits can prevent
  detection. The checks expect the marker at its original location or start time.
- The current web app holds one marked download in module-level variables,
  shared by clients in the same server process. It has no per-user isolation or
  concurrency protection. Separate server workers do not share that download.
- Every new watermark submission clears the previous download before validating
  the new upload. Restarting the app also loses the download. Detection does not
  clear it.
- Uploaded and processed bytes are held in memory. Audio/video processing also
  uses temporary files, which are cleaned up when processing finishes. There is
  a 500 MiB file limit and a 501 MiB request limit, with no processing queue or
  persistent upload store.
- The browser uses the single-file workflow. The system API stores each job and its download separately.

## Automatic tester

Select **Automatic tester** on either mode's page, choose **Full** or **Quick**,
then select **Run automatic tester**. The page refreshes every two seconds while
the checks run. It then displays pass/fail status, counts, elapsed time, and
expandable test output. A completed run's JSON summary can be downloaded.
Navigation back to watermark and detection modes remains available.

Website checks run in a separate subprocess, so their temporary Flask settings
and sample downloads do not change the live app's current download. Only one run
is active per server instance, with a 180-second deadline. Status and reports
use temporary files shared by workers on that instance; they are not durable
or shared across multiple Elastic Beanstalk instances. In a multi-instance
environment, another request may reach an instance that cannot find the run.
Include the `testing/` directory with its fixtures in the deployment ZIP so the
website tester can run the suite.

Run the local automatic tester from the project directory:

```powershell
.\.venv\Scripts\python.exe tester.py
```

The default run uses synthetic samples and the [bundled stock media](testing/fixtures/README.md).
It checks all 14 accepted
extensions, watermark/detection round trips, short audio, video frames and
optional audio, original-file preservation, web navigation and uploads,
download behavior, result redirects, invalid inputs, and command-line behavior.
It also checks the 500 MiB file / 501 MiB request settings against nginx's
configuration and exercises size boundaries with a smaller test cap.

For a faster image/web/configuration/command-line check, or a JSON report:

```powershell
.\.venv\Scripts\python.exe tester.py --quick
.\.venv\Scripts\python.exe tester.py --report test-report.json
```

The tester prints a pass/fail summary. Exit codes are `0` for success, `1` for
failed checks, and `2` for setup, command arguments, or report-writing errors.
`--report` replaces the named file if it exists; its parent directory must
already exist. The report contains mode, outcome, test counts, elapsed time,
and failure details. Counts refer to test groups; formats are also checked in
subcases within those groups.

No running web server or internet access is needed. The tester reads the stock
fixtures and writes generated samples and marked copies to temporary storage.
It restores any Flask settings and download state changed inside its process. The default full run fails
if required FFmpeg functionality is unavailable; `--quick` omits audio/video
checks. This tests the local application, not a deployed AWS environment or an
actual 500 MiB transfer.

## Troubleshooting and checking the workflow

| Symptom | Check |
| --- | --- |
| Missing Python module | Install `requirements.txt` with the same Python interpreter used to run the app. |
| FFmpeg unavailable | Check the `imageio-ffmpeg` installation or set `IMAGEIO_FFMPEG_EXE` to the correct executable. |
| Valid extension but processing fails | Confirm the file can actually be decoded, the image/video is large enough, and the temporary directory is writable. Detection processing details are logged by the Flask app. |
| No downloadable file | Finish a successful watermark upload. A failed subsequent watermark upload clears the previous result. |
| Background image missing | Include the original PNG in `assets/` when deploying. Confirm its filename is unchanged and its `/assets/` URL loads. |
| Styling missing | Include `templates/base.html` and `assets/style.css` in the deployment. Confirm `/assets/style.css` loads and refresh the browser cache. |

For a quick manual check, watermark a supported file, download the result, and
check it in detection mode. The copy should lead to **Watermark found**. Check an
original without the service's marker to confirm **No watermark found**. Try an
unsupported file and an unreadable file to confirm that each produces an error
instead of a detection result. Both result pages should allow returning to
either mode.
