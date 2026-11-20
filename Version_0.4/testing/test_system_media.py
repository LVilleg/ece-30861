"""Exercise the project API's text watermark independently of the browser codec."""

from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import unittest

import imageio_ffmpeg
from PIL import Image

from system_service.media import UnsupportedMedia, detect, embed, validate_media


def ffmpeg(*args):
    return subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", *map(str, args)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=True,
    ).stdout


class SystemMediaTests(unittest.TestCase):
    def test_all_formats_preserve_playback_and_unicode_payload(self):
        fixtures = Path(__file__).parent / "fixtures"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            png = root / "test.png"
            Image.new("RGBA", (17, 13), (8, 90, 138, 73)).save(png)
            files = [png, fixtures / "earthrise.jpg", fixtures / "crescent-moon.mp4"]
            for extension in ("wav", "mp3"):
                path = root / ("test." + extension)
                ffmpeg("-f", "lavfi", "-i", "sine=duration=0.25", path)
                files.append(path)

            for path in files:
                with self.subTest(format=path.suffix):
                    source = path.read_bytes()
                    kind, _ = validate_media(source, path.name)
                    self.assertFalse(detect(source, path.name)["detected"])
                    expected = "\U0001f600" * 63 + "\0"
                    marked = embed(source, path.name, expected)
                    self.assertEqual(detect(marked, path.name), {
                        "detected": True, "confidence": 1.0, "mediaKind": kind, "payload": expected,
                    })
                    output = root / ("marked" + path.suffix)
                    output.write_bytes(marked)
                    # Compare FFmpeg's checksums of all decoded frames/samples.
                    self.assertEqual(ffmpeg("-i", path, "-f", "framemd5", "-"),
                                     ffmpeg("-i", output, "-f", "framemd5", "-"))
                    for payload in ("", None):
                        marked = embed(marked, path.name, payload)
                        recovered = detect(marked, path.name)
                        self.assertTrue(recovered["detected"])
                        self.assertEqual(recovered["payload"], payload)

    def test_invalid_files_and_payload(self):
        for suffix in ("jpeg", "png", "mp3", "mp4", "wav"):
            with self.subTest(extension=suffix), self.assertRaises(ValueError):
                validate_media(b"not media", "test." + suffix)
        with self.assertRaises(UnsupportedMedia):
            validate_media(b"not media", "test.txt")
        with self.assertRaises(ValueError):
            embed(b"", "test.png", "a" * 65)
        data = BytesIO()
        Image.new("RGB", (1, 1)).save(data, "GIF")
        with self.assertRaises(UnsupportedMedia):
            validate_media(data.getvalue(), "test.png")
        png = BytesIO()
        Image.new("RGB", (1, 1)).save(png, "PNG")
        with self.assertRaises(ValueError):
            validate_media(png.getvalue()[:-12], "truncated.png")
