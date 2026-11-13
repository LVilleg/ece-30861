"""Detect the content watermark written by this service's watermark.py.

detect_watermark_bytes(data, filename) and detect_watermark_file(path) return:
    True: the service's watermark was found.
    False: no matching watermark was found.

Invalid, unsupported, or unreadable files raise an exception instead of being
reported as unwatermarked. Detection uses watermark.py's existing decoder and
marker checks, so both modes recognize the same image, audio, and video marks.

Standalone use: python detection.py path/to/file.png
"""

import argparse
from pathlib import Path

from watermark import WatermarkError, has_watermark


def detect_watermark_bytes(data: bytes, filename: str) -> bool:
    """Return True for this service's watermark, or False when it is absent.

    filename supplies the supported file extension. Malformed or unsupported
    media raises WatermarkError. The original data is never modified.
    """
    if not data:
        raise WatermarkError("The file is empty.")
    return has_watermark(data, filename)


def detect_watermark_file(file_path: str | Path) -> bool:
    """Check a local file without changing it; file access errors raise OSError."""
    source = Path(file_path)
    return detect_watermark_bytes(source.read_bytes(), source.name)


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Supported image, audio, or video file to check")
    args = parser.parse_args()
    try:
        detected = detect_watermark_file(args.input)
    except (WatermarkError, OSError) as exc:
        parser.exit(2, f"{exc}\n")
    print("Watermark found" if detected else "No watermark found")


if __name__ == "__main__":
    _main()
