"""Run the service's automated checks: python tester.py [--quick] [--report PATH].

Default: generated images/audio/video, detection, local Flask workflows, and
upload limits. --quick omits audio/video checks. No live server or AWS access
is needed. Samples use temporary directories; existing files are not inputs.
"""

import argparse
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    import imageio_ffmpeg
    import numpy as np
    from PIL import Image
    import main as web
    import detection as detector
    import watermark as wm
except ImportError as exc:
    print(f"Cannot load the application: {exc}", file=sys.stderr)
    print("Install requirements.txt with the Python interpreter running this tester.", file=sys.stderr)
    raise SystemExit(2)


PROJECT_ROOT = Path(__file__).resolve().parent


def sample_image(suffix=".png", size=(96, 64), transparent=False):
    mode = "RGBA" if transparent else "RGB"
    color = (45, 70, 90, 32) if transparent else (45, 70, 90)
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, format="PNG" if suffix == ".png" else "JPEG")
    return output.getvalue()


class ImageTests(unittest.TestCase):
    def test_supported_image_formats(self):
        for suffix in sorted(wm.IMAGE_EXTENSIONS):
            with self.subTest(extension=suffix):
                original = sample_image(suffix)
                self.assertIs(detector.detect_watermark_bytes(original, "sample" + suffix), False)
                marked = wm.watermark_bytes(original, "sample" + suffix)
                self.assertNotEqual(original, marked)
                self.assertIs(detector.detect_watermark_bytes(marked, "sample" + suffix), True)

    def test_small_images_and_transparency(self):
        for size in [(8, 8), (17, 25), (96, 64)]:
            with self.subTest(size=size):
                marked = wm.watermark_bytes(sample_image(size=size, transparent=True), "sample.png")
                self.assertIs(detector.detect_watermark_bytes(marked, "sample.png"), True)
                with Image.open(BytesIO(marked)) as image:
                    self.assertEqual(image.size, size)
                    self.assertEqual(image.getpixel((0, 0))[3], 255)
                    if size == (96, 64):
                        self.assertEqual(image.getpixel((95, 63))[3], 32)

    def test_jpeg_recompression(self):
        marked = wm.watermark_bytes(sample_image(".jpeg"), "sample.jpeg")
        with Image.open(BytesIO(marked)) as image:
            output = BytesIO()
            image.save(output, format="JPEG", quality=60)
        self.assertIs(detector.detect_watermark_bytes(output.getvalue(), "sample.jpeg"), True)

    def test_file_api_preserves_original_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="watermark-file-test-") as directory:
            source, output = Path(directory) / "original.png", Path(directory) / "marked.png"
            original = sample_image()
            source.write_bytes(original)
            self.assertEqual(wm.watermark_file(source, output), output)
            self.assertEqual(source.read_bytes(), original)
            self.assertIs(detector.detect_watermark_file(source), False)
            self.assertIs(detector.detect_watermark_file(output), True)
            with self.assertRaises(FileExistsError):
                wm.watermark_file(source, output)
            with self.assertRaises(wm.WatermarkError):
                wm.watermark_file(source, source)
            with self.assertRaises(wm.WatermarkError):
                wm.watermark_file(source, Path(directory) / "different.wav")
            with self.assertRaises(OSError):
                detector.detect_watermark_file(Path(directory) / "missing.png")

    def test_invalid_inputs_raise_instead_of_returning_false(self):
        for filename, data in [("empty.png", b""), ("broken.png", b"invalid image"), ("notes.txt", b"text")]:
            with self.subTest(filename=filename):
                with self.assertRaises(wm.WatermarkError):
                    detector.detect_watermark_bytes(data, filename)
                with self.assertRaises(wm.WatermarkError):
                    wm.watermark_bytes(data, filename)


class WebTests(unittest.TestCase):
    def setUp(self):
        original_config = dict(web.app.config)
        original_file, original_name = web.temp_file, web.original_name
        original_logging = web.app.logger.disabled

        def restore():
            web.app.config.clear()
            web.app.config.update(original_config)
            web.temp_file, web.original_name = original_file, original_name
            web.app.logger.disabled = original_logging

        self.addCleanup(restore)
        web.app.config["TESTING"] = True
        web.app.logger.disabled = True
        web.temp_file = None
        web.original_name = None
        self.client = web.app.test_client()

    def upload(self, route, data, filename="sample.png"):
        return self.client.post(route, data={"user-file": (BytesIO(data), filename)})

    def test_navigation_and_result_links(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/insert")
        self.assertIn(b'href="/detection"', self.client.get("/insert").data)
        self.assertIn(b'href="/insert"', self.client.get("/detection").data)
        for route in ["/detection/found", "/detection/not-found"]:
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'href="/insert"', response.data)
            self.assertIn(b'href="/detection"', response.data)

    def test_watermark_download_and_detection_results(self):
        original = sample_image()
        self.assertEqual(self.client.get("/download").status_code, 400)
        response = self.upload("/insert_submit", original, "sample.PNG")
        self.assertEqual(response.status_code, 200)
        download = self.client.get("/download")
        self.assertEqual(download.status_code, 200)
        self.assertIn("sample.PNG", download.headers["Content-Disposition"])
        self.assertIs(detector.detect_watermark_bytes(download.data, "sample.png"), True)
        for content, destination, heading in [
            (original, "/detection/not-found", b"<h3>No watermark found</h3>"),
            (download.data, "/detection/found", b"<h3>Watermark found</h3>"),
        ]:
            with self.subTest(result=destination):
                response = self.upload("/detection", content)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["Location"], destination)
                self.assertIn(heading, self.client.get(destination).data)
                self.assertEqual(self.client.get("/download").data, download.data)

    def test_invalid_uploads_and_download_state(self):
        for route in ["/insert_submit", "/detection"]:
            with self.subTest(route=route):
                self.assertEqual(self.client.post(route).status_code, 400)
                multiple = {"user-file": [(BytesIO(sample_image()), "a.png"), (BytesIO(sample_image()), "b.png")]}
                self.assertEqual(self.client.post(route, data=multiple).status_code, 400)
                self.assertEqual(self.upload(route, sample_image(), "").status_code, 400)
                for name, data in [("notes.txt", b"text"), ("empty.png", b""), ("broken.png", b"bad image")]:
                    self.assertEqual(self.upload(route, data, name).status_code, 400)
        self.assertEqual(self.upload("/insert_submit", sample_image()).status_code, 200)
        saved = self.client.get("/download").data
        response = self.upload("/detection", b"bad image")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Location", response.headers)
        self.assertEqual(self.client.get("/download").data, saved)
        response = self.upload("/insert_submit", b"text", "notes.txt")
        self.assertIn(b"Unsupported File Type", response.data)
        self.assertEqual(self.client.get("/download").status_code, 400)

    def test_file_size_boundary_with_scaled_limits(self):
        # Use a 1 MiB cap to check real multipart parsing without allocating
        # 500 MiB. Media processors are mocked only for this boundary test.
        cap = 1024 * 1024
        web.app.config["MAX_FILE_SIZE"] = cap
        web.app.config["MAX_CONTENT_LENGTH"] = cap + 1024 * 1024
        for route, name, returned, accepted in [
            ("/insert_submit", "watermark_bytes", b"marked", 200),
            ("/detection", "detect_watermark_bytes", True, 303),
        ]:
            for size in [cap - 1, cap, cap + 1]:
                with self.subTest(route=route, size=size):
                    with patch.object(web, name, return_value=returned) as processor:
                        response = self.upload(route, b"x" * size)
                        self.assertEqual(response.status_code, accepted if size <= cap else 413)
                        if size <= cap:
                            processor.assert_called_once()
                            self.assertEqual(len(processor.call_args.args[0]), size)
                        else:
                            processor.assert_not_called()

    def test_oversized_request_headers_return_413(self):
        limit = web.app.config["MAX_CONTENT_LENGTH"]
        for route in ["/insert_submit", "/detection"]:
            with self.subTest(route=route):
                response = self.client.post(
                    route, input_stream=BytesIO(b""),
                    content_type="multipart/form-data; boundary=test",
                    environ_overrides={"CONTENT_LENGTH": str(limit + 1)},
                )
                self.assertEqual(response.status_code, 413)
                self.assertIn(b"500 MiB", response.data)


    def test_tester_navigation_and_start(self):
        self.assertIn(b'href="/tester"', self.client.get("/insert").data)
        self.assertIn(b'href="/tester"', self.client.get("/detection").data)
        page = self.client.get("/tester")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Run automatic tester", page.data)
        run_id = "a" * 32
        with patch.object(web, "start_tester_run", return_value=run_id) as start:
            for mode in ("full", "quick"):
                response = self.client.post("/tester/run", data={"mode": mode})
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["Location"], "/tester?run=" + run_id)
                start.assert_called_with(mode)
            start.reset_mock()
            self.assertEqual(self.client.post("/tester/run", data={"mode": "invalid"}).status_code, 400)
            start.assert_not_called()
        self.assertEqual(self.client.get("/tester/run").status_code, 405)
        self.assertEqual(self.client.get("/tester?run=invalid").status_code, 404)

    def test_tester_results_and_report(self):
        run_id = "b" * 32
        for status in ("running", "passed", "failed", "error"):
            with self.subTest(status=status):
                record = {
                    "id": run_id, "mode": "full", "status": status,
                    "message": "A tester result", "report": None,
                    "output": "<script>untrusted test output</script>",
                }
                with patch.object(web, "get_tester_run", return_value=record):
                    response = self.client.get("/tester?run=" + run_id)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn(b"<script>untrusted test output</script>", response.data)
                    self.assertIn(b'href="/insert"', response.data)
                    self.assertIn(b'href="/detection"', response.data)
                    self.assertEqual(b'http-equiv="refresh"' in response.data, status == "running")
                    report = self.client.get("/tester/report/" + run_id)
                    self.assertEqual(report.status_code, 200)
                    self.assertEqual(report.json["status"], status)
                    self.assertIn("attachment", report.headers["Content-Disposition"])


class ConfigurationTests(unittest.TestCase):
    def test_flask_and_nginx_size_settings_agree(self):
        self.assertEqual(web.app.config["MAX_FILE_SIZE"], 500 * 1024 * 1024)
        request_limit = web.app.config["MAX_CONTENT_LENGTH"]
        self.assertEqual(request_limit, 501 * 1024 * 1024)
        config = PROJECT_ROOT / ".platform/nginx/conf.d/client_max_body_size.conf"
        text = config.read_text(encoding="utf-8")
        directives = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        match = re.search(r"\bclient_max_body_size\s+(\d+)[mM]\s*;", directives)
        self.assertIsNotNone(match, "nginx upload limit directive is missing")
        self.assertEqual(int(match.group(1)) * 1024 * 1024, request_limit)
        self.assertFalse(text.startswith("\ufeff"), "nginx config must not start with a BOM")

    def test_wsgi_entry_point(self):
        import application
        self.assertIs(application.application, web.app)



class CommandLineTests(unittest.TestCase):
    def test_watermark_and_detection_commands(self):
        with tempfile.TemporaryDirectory(prefix="watermark-cli-tests-") as directory:
            source, output = Path(directory) / "original.png", Path(directory) / "marked.png"
            source.write_bytes(sample_image())
            commands = [
                ("watermark.py", [str(source), str(output)], 0, "Watermarked file saved"),
                ("detection.py", [str(output)], 0, "Watermark found"),
                ("detection.py", [str(source)], 0, "No watermark found"),
                ("watermark.py", [str(source), str(output)], 1, ""),
                ("detection.py", [str(Path(directory) / "missing.png")], 2, ""),
            ]
            for script, args, code, message in commands:
                with self.subTest(command=script, expected=code, message=message):
                    result = subprocess.run(
                        [sys.executable, "-B", str(PROJECT_ROOT / script), *args],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                    self.assertIn(message, result.stdout)
            self.assertIs(detector.detect_watermark_file(output), True)


class MediaTests(unittest.TestCase):
    """Exercise real audio/video encoding and the public detection interfaces."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="watermark-media-tests-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def _ffmpeg(self, args):
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run(
            [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             *map(str, args)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60, check=False,
        )
        self.assertEqual(
            result.returncode, 0,
            "FFmpeg failed: " + result.stderr.decode("utf-8", errors="replace")[-1500:],
        )
        return result.stdout

    def _roundtrip(self, source):
        original = source.read_bytes()
        self.assertIs(detector.detect_watermark_bytes(original, source.name), False)
        marked = wm.watermark_bytes(original, source.name)
        self.assertNotEqual(marked, original)
        self.assertEqual(source.read_bytes(), original, "The original file changed")
        self.assertIs(detector.detect_watermark_bytes(marked, source.name), True)
        output = source.with_name(source.stem + "_marked" + source.suffix)
        output.write_bytes(marked)
        self.assertIs(detector.detect_watermark_file(output), True)
        # Decode the complete output, not just the prefix/frame used by detection.
        self._ffmpeg(["-i", output, "-f", "null", "-"])
        return output

    def test_audio_formats(self):
        cases = [
            (".m4a", "aac", "ipod"),
            (".mp3", "libmp3lame", "mp3"),
            (".wav", "pcm_s16le", "wav"),
        ]
        # The nearby 1250 Hz tone catches carrier-analysis regressions that
        # a simple low-frequency tone would not reveal.
        for frequency in (440, 1250):
            for suffix, codec, muxer in cases:
                with self.subTest(extension=suffix, frequency=frequency):
                    source = self.root / (f"audio_{frequency}" + suffix)
                    self._ffmpeg([
                        "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:sample_rate=48000:duration=1.6",
                        "-ac", "2", "-c:a", codec, "-f", muxer, source,
                    ])
                    self._roundtrip(source)

    def test_short_audio_is_padded(self):
        source = self.root / "short.wav"
        self._ffmpeg([
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.08",
            "-ac", "2", "-c:a", "pcm_s16le", source,
        ])
        output = self._roundtrip(source)
        pcm = self._ffmpeg([
            "-i", output, "-map", "0:a:0", "-ar", "48000", "-ac", "2",
            "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
        ])
        self.assertEqual(len(pcm), 61440 * 2 * 2)  # 1.28 s, stereo, 16-bit.

    def test_invalid_audio_and_video_are_errors(self):
        for suffix in (".wav", ".mp4"):
            with self.subTest(extension=suffix):
                with self.assertRaises(wm.WatermarkError):
                    detector.detect_watermark_bytes(b"invalid media", "broken" + suffix)
                with self.assertRaises(wm.WatermarkError):
                    wm.watermark_bytes(b"invalid media", "broken" + suffix)

    def test_video_formats_and_optional_audio(self):
        cases = [
            (".mp4", "libx264", "mp4", "aac"),
            (".avi", "mpeg4", "avi", "libmp3lame"),
            (".mov", "libx264", "mov", "aac"),
            (".mpg", "mpeg2video", "mpeg", "mp2"),
            (".mpeg", "mpeg2video", "mpeg", "mp2"),
            (".mpeg2", "mpeg2video", "mpeg", "mp2"),
            (".mpeg-2", "mpeg2video", "mpeg", "mp2"),
            (".m2v", "mpeg2video", "mpeg2video", None),
        ]
        width, height, frames = 96, 64, 10
        for suffix, codec, muxer, audio_codec in cases:
            for with_audio in ((False, True) if audio_codec else (False,)):
                with self.subTest(extension=suffix, audio=with_audio):
                    source = self.root / (f"video_{int(with_audio)}" + suffix)
                    args = [
                        "-f", "lavfi", "-i",
                        "testsrc2=size=96x64:rate=25:duration=0.4",
                    ]
                    if with_audio:
                        args += [
                            "-f", "lavfi", "-i",
                            "sine=frequency=440:sample_rate=48000:duration=0.4",
                            "-c:a", audio_codec,
                        ]
                    args += ["-c:v", codec, "-pix_fmt", "yuv420p", "-f", muxer, source]
                    self._ffmpeg(args)
                    output = self._roundtrip(source)
                    raw = self._ffmpeg([
                        "-i", output, "-map", "0:v:0", "-fps_mode", "passthrough",
                        "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
                    ])
                    self.assertEqual(len(raw), width * height * frames)
                    last = np.frombuffer(raw[-width * height:], np.uint8).reshape(height, width)
                    png = BytesIO()
                    Image.fromarray(last).save(png, format="PNG")
                    self.assertIs(detector.detect_watermark_bytes(png.getvalue(), "last.png"), True)
                    if with_audio:
                        self._ffmpeg(["-i", output, "-map", "0:a:0", "-f", "null", "-"])


def run_tests(quick=False, report_path=None):
    """Run local checks and return 0 for success, 1 for failed checks, or 2 for a report error."""
    classes = [ImageTests, WebTests, ConfigurationTests, CommandLineTests]
    if not quick:
        classes.append(MediaTests)
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in classes
    )
    mode = "quick" if quick else "full"
    print(f"Running {mode} tests against {PROJECT_ROOT}", file=sys.stderr, flush=True)
    print("Local application checks; no live AWS requests or 500 MiB load test.", file=sys.stderr, flush=True)
    started = time.perf_counter()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    elapsed = time.perf_counter() - started
    if report_path is not None:
        report = {
            "mode": mode,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "project_directory": str(PROJECT_ROOT),
            "successful": result.wasSuccessful(),
            "tests_run": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "elapsed_seconds": round(elapsed, 3),
            "failure_details": [
                {"test": test.id(), "traceback": detail}
                for test, detail in result.failures + result.errors
            ],
        }
        try:
            report_path = Path(report_path)
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Could not write test report: {exc}", file=sys.stderr)
            return 2
        print(f"JSON report: {report_path.resolve()}", file=sys.stderr)
    return 0 if result.wasSuccessful() else 1


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true", help="Run image, web, configuration, and command-line checks only")
    parser.add_argument("--report", type=Path, metavar="PATH", help="Write a JSON report, replacing PATH if it exists")
    args = parser.parse_args()
    return run_tests(quick=args.quick, report_path=args.report)


if __name__ == "__main__":
    raise SystemExit(_main())

