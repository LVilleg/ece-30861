"""Basic content watermarks for one image, audio clip, or video at a time.

Images/video receive an opaque binary pattern in the top-left corner. Audio
receives a 64-bit tone sequence mixed into its first 1.28 seconds; shorter audio
is padded to fit. These marks are intentionally simple, visible/audible, and
removable, not cryptographic proof of ownership or AI generation.

Install requirements.txt, then use watermark_bytes(data, filename) from Flask,
or run: python watermark.py input.mp3 output.mp3

has_watermark(data, filename) verifies the mark from decoded media, not metadata.
Audio is resampled to 48 kHz using 16-bit PCM for processing. Audio/video are
re-encoded in the original format. PNG/JPEG inputs are treated as still images.
"""

from io import BytesIO
from pathlib import Path
import argparse
import subprocess
import tempfile
import wave

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageOps


IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
AUDIO_EXTENSIONS = frozenset({".m4a", ".mp3", ".wav"})
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".avi", ".mov", ".mpg", ".mpeg", ".mpeg2", ".mpeg-2", ".m2v",
})
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# Fixed 64-bit identifier shared by the visual and audio marks.
_MARKER_BITS = np.unpackbits(
    np.frombuffer(bytes.fromhex("a6d35c9e71b48f02"), dtype=np.uint8)
).reshape(8, 8)
_SAMPLE_RATE = 48000
_SYMBOL_SAMPLES = 960  # 20 ms per bit, 1.28 seconds for all 64 bits.
_FREQUENCIES = (1200, 2200)


class WatermarkError(ValueError):
    """The input cannot be watermarked or its format is unsupported."""


def _run_ffmpeg(args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-hide_banner",
             "-loglevel", "error", "-y", *map(str, args)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
    except (OSError, RuntimeError) as exc:
        raise WatermarkError("FFmpeg is unavailable; install requirements.txt.") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise WatermarkError(f"Cannot process this media file: {detail[-1500:]}")
    return result.stdout


def _suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise WatermarkError("Unsupported file type.")
    return suffix


def _audio_basis() -> tuple[np.ndarray, np.ndarray]:
    time = np.arange(_SYMBOL_SAMPLES) / _SAMPLE_RATE
    phase = 2 * np.pi * np.asarray(_FREQUENCIES)[:, None] * time
    return np.sin(phase), np.cos(phase)


def _decode_audio(source: Path, target: Path, *, prefix_only: bool = False) -> None:
    args = ["-i", str(source), "-map", "0:a:0", "-vn"]
    if prefix_only:
        args += ["-t", "1.28"]
    _run_ffmpeg(args + ["-ar", str(_SAMPLE_RATE), "-c:a", "pcm_s16le", str(target)])


def _read_pcm(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
    return samples.reshape(-1, channels).astype(np.float64) / 32768, channels


def _watermark_audio(source: Path, target: Path, suffix: str) -> None:
    decoded = target.with_name("decoded.wav")
    _decode_audio(source, decoded)
    samples, channels = _read_pcm(decoded)
    if not len(samples):
        raise WatermarkError("The audio contains no samples.")
    required = _MARKER_BITS.size * _SYMBOL_SAMPLES
    if len(samples) < required:
        samples = np.pad(samples, ((0, required - len(samples)), (0, 0)))

    sine, cosine = _audio_basis()
    # Remove just the two carrier components in each symbol so an existing
    # musical tone cannot mask the bit. Reserve headroom before mixing it in.
    fade = np.ones(_SYMBOL_SAMPLES)
    ramp = np.linspace(0, 1, 48)
    fade[:48], fade[-48:] = ramp, ramp[::-1]
    for index, bit in enumerate(_MARKER_BITS.flat):
        start = index * _SYMBOL_SAMPLES
        block = samples[start:start + _SYMBOL_SAMPLES]
        block *= 0.85
        for basis in (sine, cosine):
            block -= basis.T @ (basis @ block) * (2 / _SYMBOL_SAMPLES)
        block += (0.10 * sine[int(bit)] * fade)[:, None]

    marked = target.with_name("marked.wav")
    with wave.open(str(marked), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(_SAMPLE_RATE)
        audio.writeframes((np.clip(samples, -1, 32767 / 32768) * 32768).astype("<i2").tobytes())
    codecs = {
        ".m4a": ["-c:a", "aac", "-b:a", "192k", "-f", "ipod"],
        ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"],
        ".wav": ["-c:a", "pcm_s16le", "-f", "wav"],
    }
    _run_ffmpeg(["-i", str(marked), "-map", "0:a:0", *codecs[suffix], str(target)])


def _audio_has_watermark(source: Path) -> bool:
    decoded = source.with_name("detect.wav")
    _decode_audio(source, decoded, prefix_only=True)
    samples, _ = _read_pcm(decoded)
    required = _MARKER_BITS.size * _SYMBOL_SAMPLES
    if len(samples) < required:
        return False
    blocks = samples[:required].mean(axis=1).reshape(-1, _SYMBOL_SAMPLES)
    sine, cosine = _audio_basis()
    energy = (blocks @ sine.T) ** 2 + (blocks @ cosine.T) ** 2
    detected = energy.argmax(axis=1)
    chosen = energy[np.arange(len(detected)), detected]
    other = energy[np.arange(len(detected)), 1 - detected]
    return bool(
        np.array_equal(detected, _MARKER_BITS.ravel())
        and np.all(chosen > 4 * other)
        and np.all(chosen > 1)
    )


def watermark_bytes(data: bytes, filename: str) -> bytes:
    """Return marked media bytes, keeping the filename's format.

    Images must be at least 8 by 8 pixels. Short audio is padded to 1.28 s.
    The caller's data and original file are never modified.
    """
    suffix = _suffix(filename)
    if not data:
        raise WatermarkError("The uploaded file is empty.")
    try:
        if suffix in IMAGE_EXTENSIONS:
            return _watermark_image(data, suffix)
        with tempfile.TemporaryDirectory(prefix="watermark-") as directory:
            source = Path(directory) / ("input" + suffix)
            target = Path(directory) / ("output" + suffix)
            source.write_bytes(data)
            if suffix in AUDIO_EXTENSIONS:
                _watermark_audio(source, target, suffix)
            else:
                _watermark_video(source, target, suffix)
            return target.read_bytes()
    except (OSError, ValueError, wave.Error) as exc:
        if isinstance(exc, WatermarkError):
            raise
        raise WatermarkError("The uploaded file is invalid or could not be processed.") from exc


def has_watermark(data: bytes, filename: str) -> bool:
    """Detect this module's fixed marker in decoded content; no original needed.

    Cropping, trimming, or other edits can remove this basic watermark.
    Invalid/unsupported files raise WatermarkError rather than report a match.
    """
    suffix = _suffix(filename)
    try:
        if suffix in IMAGE_EXTENSIONS:
            return _image_has_watermark(data)
        with tempfile.TemporaryDirectory(prefix="watermark-check-") as directory:
            source = Path(directory) / ("input" + suffix)
            source.write_bytes(data)
            if suffix in AUDIO_EXTENSIONS:
                return _audio_has_watermark(source)
            return _video_has_watermark(source)
    except (OSError, ValueError, wave.Error) as exc:
        if isinstance(exc, WatermarkError):
            raise
        raise WatermarkError("The file is invalid or could not be decoded.") from exc


def watermark_file(input_path: str | Path, output_path: str | Path) -> Path:
    """Save a marked copy, refusing to replace the input or an existing output."""
    source, target = Path(input_path), Path(output_path)
    suffix = _suffix(str(source))
    output_suffix = _suffix(str(target))
    jpeg_alias = {suffix, output_suffix} <= {".jpg", ".jpeg"}
    if suffix != output_suffix and not jpeg_alias:
        raise WatermarkError("Input and output must use the same file extension.")
    if source.resolve() == target.resolve():
        raise WatermarkError("Choose a different output path to keep the original.")
    marked = watermark_bytes(source.read_bytes(), source.name)
    with target.open("xb") as output:
        output.write(marked)
    return target


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Input media file")
    parser.add_argument("output", help="New watermarked file with the same extension")
    args = parser.parse_args()
    try:
        result = watermark_file(args.input, args.output)
    except (WatermarkError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Watermarked file saved to {result}")


def _marker_cell_size(width: int, height: int) -> int:
    """Use up to a 32 x 32 marker, shrinking for small inputs."""
    cell = min(4, width // 8, height // 8)
    if cell < 1:
        raise WatermarkError("Images and videos must be at least 8 by 8 pixels.")
    return cell


def _watermark_image(data: bytes, suffix: str) -> bytes:
    """Put an opaque binary marker in the upper-left corner of an image."""
    expected_format = "PNG" if suffix.lower() == ".png" else "JPEG"
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format != expected_format:
                raise WatermarkError("The image contents do not match its extension.")
            oriented = ImageOps.exif_transpose(source)
            has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            mode = "RGBA" if expected_format == "PNG" and has_alpha else "RGB"
            image = oriented.convert(mode)
            cell = _marker_cell_size(*image.size)
            pixels = (_MARKER_BITS * 255).astype(np.uint8)
            pixels = np.repeat(np.repeat(pixels, cell, axis=0), cell, axis=1)
            marker = Image.fromarray(pixels).convert(mode)
            image.paste(marker, (0, 0))
            output = BytesIO()
            options = {"quality": 95, "subsampling": 0} if expected_format == "JPEG" else {}
            image.save(output, format=expected_format, **options)
            return output.getvalue()
    except OSError as exc:
        raise WatermarkError("Unable to read or watermark the image.") from exc


def _image_has_watermark(data: bytes) -> bool:
    """Detect the marker from pixels, allowing minor compression damage."""
    try:
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("L")
            for cell in range(1, 5):
                side = 8 * cell
                if image.width < side or image.height < side:
                    continue
                pixels = np.asarray(image.crop((0, 0, side, side)), dtype=np.float32)
                cells = pixels.reshape(8, cell, 8, cell).mean(axis=(1, 3))
                matches = np.count_nonzero((cells >= 128) == _MARKER_BITS)
                contrast = cells[_MARKER_BITS == 1].mean() - cells[_MARKER_BITS == 0].mean()
                if matches >= 62 and contrast >= 100:
                    return True
    except (OSError, ValueError) as exc:
        raise WatermarkError("The file is invalid or could not be decoded.") from exc
    return False


def _first_video_frame(source: Path) -> bytes:
    """Decode the first video stream to PNG, including display rotation."""
    return _run_ffmpeg([
        "-i", str(source), "-map", "0:v:0", "-frames:v", "1",
        "-c:v", "png", "-f", "image2pipe", "pipe:1",
    ])


def _watermark_video(source: Path, target: Path, suffix: str) -> None:
    """Mark every frame and re-encode to a codec suitable for its container.

    MP4/MOV use H.264/AAC, AVI uses MPEG-4/MP3, and MPEG-2 program streams
    use MPEG-2/MP2 at 25 fps. M2V is an elementary MPEG-2 video-only stream.
    Odd dimensions are padded by at most one row/column for compatibility.
    """
    with Image.open(BytesIO(_first_video_frame(source))) as frame:
        cell = _marker_cell_size(*frame.size)

    # Drawing directly avoids an extra image stream and its EOF/timing behavior.
    filters = ["pad=ceil(iw/2)*2:ceil(ih/2)*2"]
    for row in range(8):
        for column in range(8):
            color = "white" if _MARKER_BITS[row, column] else "black"
            filters.append(
                f"drawbox=x={column * cell}:y={row * cell}:w={cell}:h={cell}:"
                f"color={color}:t=fill"
            )

    suffix = suffix.lower()
    args = ["-i", str(source), "-map", "0:v:0"]
    if suffix == ".m2v":
        args += ["-an"]
    else:
        args += ["-map", "0:a:0?"]
    args += ["-vf", ",".join(filters), "-pix_fmt", "yuv420p"]

    if suffix in {".mp4", ".mov"}:
        muxer = "mp4" if suffix == ".mp4" else "mov"
        args += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-f", muxer,
        ]
    elif suffix == ".avi":
        args += [
            "-c:v", "mpeg4", "-q:v", "2", "-c:a", "libmp3lame",
            "-q:a", "2", "-f", "avi",
        ]
    else:
        args += ["-c:v", "mpeg2video", "-q:v", "2", "-r", "25"]
        if suffix == ".m2v":
            args += ["-f", "mpeg2video"]
        else:
            args += ["-c:a", "mp2", "-b:a", "192k", "-f", "mpeg"]
    _run_ffmpeg(args + [str(target)])


def _video_has_watermark(source: Path) -> bool:
    """Check the decoded first frame; no container metadata is required."""
    return _image_has_watermark(_first_video_frame(source))


if __name__ == "__main__":
    _main()

