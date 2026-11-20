"""Store an optional text watermark in media metadata without re-encoding the media."""

import json
import struct
import zlib
from io import BytesIO
from pathlib import PurePath

from mutagen import MutagenError
from mutagen.id3 import TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import AtomError, Atoms, MP4, MP4FreeForm
from mutagen.wave import WAVE
from PIL import Image


class UnsupportedMedia(ValueError):
    """The uploaded media type is outside the project contract."""


_TYPES = {
    ".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"), ".wav": ("audio", "audio/wav"),
    ".mp3": ("audio", "audio/mpeg"), ".mp4": ("video", "video/mp4"),
}
_KEY = "ECE30861-Watermark"
_MP4_KEY = "----:edu.purdue.ece30861:watermark"
_PREFIX = b"ECE30861-WM-1:"


def _media_type(filename):
    try:
        return _TYPES[PurePath(filename).suffix.lower()]
    except KeyError:
        raise UnsupportedMedia("Supported formats are PNG, JPEG, WAV, MP3, and MP4.") from None


def _open_image(stream, mime):
    try:
        image = Image.open(stream)
        if image.format not in ("PNG", "JPEG"):
            raise UnsupportedMedia("Only PNG and JPEG images are supported.")
        if Image.MIME[image.format] != mime:
            raise ValueError("The image format does not match its filename.")
        image.load()
        return image
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("The image is damaged or incomplete.") from exc


def _png_record_slot(data):
    # Walk the container rather than searching for IEND inside compressed data.
    # Pillow can decode pixels from a truncated PNG that has no intact IEND.
    offset = 8
    slot = None
    key = _KEY.encode("ascii") + b"\0"
    view = memoryview(data)
    while offset + 12 <= len(data):
        length, name = struct.unpack_from(">I4s", data, offset)
        end = offset + 12 + length
        if end > len(data):
            break
        if zlib.crc32(view[offset + 4:end - 4]) != struct.unpack_from(">I", data, end - 4)[0]:
            raise ValueError("The PNG image has a damaged chunk.")
        if name == b"IEND" and length == 0:
            return slot or (offset, offset)
        if name == b"tEXt" and length >= len(key) and data.startswith(key, offset + 8):
            slot = (offset, end)
        offset = end
    raise ValueError("The PNG image is missing its end chunk.")


def _open_audio_video(stream, mime):
    try:
        if mime == "video/mp4":
            atoms = Atoms(stream)
            if b"ftyp" not in atoms or b"mdat" not in atoms:
                raise ValueError("The file is missing its MP4 media or file-type box.")
            file_size = stream.seek(0, 2)
            if any(atom.offset + atom.length > file_size for atom in atoms.atoms):
                raise ValueError("The MP4 file contains a truncated box.")
            for track in atoms[b"moov"].findall(b"trak"):
                valid, handler = track[b"mdia", b"hdlr"].read(stream)
                if valid and handler[8:12] == b"vide":
                    stream.seek(0)
                    return MP4(stream)
            raise UnsupportedMedia("The MP4 file contains no video track.")
        audio = MP3(stream) if mime == "audio/mpeg" else WAVE(stream)
        if mime == "audio/mpeg" and audio.info.layer != 3:
            raise UnsupportedMedia("Only MPEG Layer III audio is supported in MP3 files.")
        if audio.info.length <= 0 or audio.info.channels < 1:
            raise ValueError("The audio contains no playable samples.")
        return audio
    except (MutagenError, AtomError, OSError, KeyError, EOFError, struct.error, RecursionError, OverflowError) as exc:
        raise ValueError("The audio or video file is damaged or incomplete.") from exc


def validate_media(data: bytes, filename: str) -> tuple[str, str]:
    """Read the media headers and return the contract's media kind and MIME type."""
    kind, mime = _media_type(filename)
    with BytesIO(data) as stream:
        if kind == "image":
            _open_image(stream, mime).close()
            if mime == "image/png":
                _png_record_slot(data)
        else:
            _open_audio_video(stream, mime)
    return kind, mime


def _mark_image(data, mime, record):
    if mime == "image/jpeg":
        # Leave JFIF/Exif first, as required by those formats. JPEG APP15
        # contains application-specific bytes and does not affect the image.
        offset = 2
        if data[offset:offset + 2] in (b"\xff\xe0", b"\xff\xe1"):
            offset += 2 + int.from_bytes(data[offset + 2:offset + 4], "big")
        marker = b"\xff\xef" + struct.pack(">H", len(record) + 2) + record
        end = offset
        if data[offset:offset + 2] == b"\xff\xef" and data.startswith(_PREFIX, offset + 4):
            end += 2 + int.from_bytes(data[offset + 2:offset + 4], "big")
        return data[:offset] + marker + data[end:]

    start, end = _png_record_slot(data)
    content = b"tEXt" + _KEY.encode("ascii") + b"\0" + record
    chunk = struct.pack(">I", len(content) - 4) + content + struct.pack(">I", zlib.crc32(content))
    return data[:start] + chunk + data[end:]


def embed(data: bytes, filename: str, payload: str | None) -> bytes:
    """Embed a versioned text record. A null payload still marks the file."""
    if payload is not None and (not isinstance(payload, str) or len(payload) > 64):
        raise ValueError("The optional payload must be text of at most 64 characters.")
    # ASCII JSON preserves Unicode, control characters, empty text and null
    # equally across the different container tag encodings.
    record = _PREFIX + json.dumps(payload, ensure_ascii=True).encode("ascii")
    kind, mime = _media_type(filename)
    with BytesIO(data) as stream:
        if kind == "image":
            _open_image(stream, mime).close()
            return _mark_image(data, mime, record)
        media = _open_audio_video(stream, mime)
        if media.tags is None:
            media.add_tags()
        if mime == "video/mp4":
            media.tags[_MP4_KEY] = [MP4FreeForm(record)]
        else:
            media.tags.add(TXXX(encoding=3, desc=_KEY, text=[record.decode("ascii")]))
        try:
            media.save(stream)
        except (MutagenError, OSError) as exc:
            raise ValueError("Unable to write the watermark in this media container.") from exc
        return stream.getvalue()


def _read_record(data, kind, mime):
    with BytesIO(data) as stream:
        if kind == "image":
            with _open_image(stream, mime) as image:
                if mime == "image/png":
                    start, end = _png_record_slot(data)
                    return data[start + 8 + len(_KEY) + 1:end - 4] if end > start else b""
                return next((value for tag, value in image.applist
                             if tag == "APP15" and value.startswith(_PREFIX)), b"")
        media = _open_audio_video(stream, mime)
        if media.tags is None:
            return b""
        if mime == "video/mp4":
            return next(iter(media.tags.get(_MP4_KEY, [])), b"")
        tag = media.tags.get("TXXX:" + _KEY)
        return tag.text[0].encode("utf-8") if tag and tag.text else b""


def detect(data: bytes, filename: str) -> dict:
    """Confidence is binary for this exact, versioned container record."""
    kind, mime = _media_type(filename)
    record = _read_record(data, kind, mime)
    # A supplementary Unicode character needs up to 12 bytes of JSON escaping.
    if record.startswith(_PREFIX) and len(record) <= len(_PREFIX) + 2 + 64 * 12:
        try:
            payload = json.loads(record[len(_PREFIX):].decode("ascii"))
            if isinstance(payload, str):
                payload.encode("utf-8")  # Reject invalid surrogate escapes in foreign tags.
        except (ValueError, RecursionError):
            pass
        else:
            if payload is None or isinstance(payload, str) and len(payload) <= 64:
                return {"detected": True, "confidence": 1.0, "mediaKind": kind, "payload": payload}
    return {"detected": False, "confidence": 0.0, "mediaKind": kind, "payload": None}
