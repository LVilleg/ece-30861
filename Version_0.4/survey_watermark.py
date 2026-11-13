"""Invisible PNG/JPEG watermarks for the Version 0.4 survey API.

PNG uses color-channel low bits. JPEG uses quantized luminance coefficients.
Files that cannot reliably carry the payload in pixels carry it in a private
PNG text chunk or JPEG application segment instead. Such marks survive an
unchanged download but may be lost if the file is re-encoded.
"""

from collections.abc import Sequence
from io import BytesIO
from math import pi, sqrt
import struct
import zlib

import numpy as np
from PIL import Image
from reedsolo import RSCodec, ReedSolomonError


STATUS_CODES = {"generated": 1, "edited": 2, "both": 3}
TOOL_CODES = {
    "unknown": 0,
    "openai": 1,
    "midjourney": 2,
    "stable_diffusion": 3,
    "flux": 4,
    "firefly": 5,
    "google": 6,
    "other": 254,
}
_PAYLOAD_BYTES = 14
_PAYLOAD_BITS = _PAYLOAD_BYTES * 8
_MAX_REPEATS = 5
_RS_CODEC = RSCodec(4)
_IMAGE_FORMATS = {"PNG", "JPEG"}
_PNG_KEY = "AI Media Survey v1"
_JPEG_PREFIX = b"AI Media Survey v1\x00"

_BLOCK_SIZE = 8
_COEFFICIENTS = ((0, 1), (1, 0), (1, 1), (0, 2))
_COEFFICIENT_STEP = 24
_frequency = np.arange(_BLOCK_SIZE)[:, None]
_position = np.arange(_BLOCK_SIZE) + 0.5
_DCT = sqrt(2 / _BLOCK_SIZE) * np.cos(pi * _frequency * _position / _BLOCK_SIZE)
_DCT[0] /= sqrt(2)


class WatermarkInputError(ValueError):
    """An upload could not be read as a watermarkable image."""


class UnsupportedImageError(WatermarkInputError):
    """The upload is an image outside the current project formats."""


def encode_payload(ai_status: str, ai_tools: Sequence[str]) -> bytes:
    """Encode a survey using CRC32 and the four-byte Reed–Solomon parity."""
    if ai_status not in STATUS_CODES:
        raise WatermarkInputError("ai_status must be generated, edited, or both.")
    if not 1 <= len(ai_tools) <= 4 or len(set(ai_tools)) != len(ai_tools):
        raise WatermarkInputError("ai_tools must contain one to four unique values.")
    try:
        tools = bytes(TOOL_CODES[tool] for tool in ai_tools)
    except KeyError as exc:
        raise WatermarkInputError("ai_tools contains an unsupported value.") from exc
    survey = bytes((STATUS_CODES[ai_status], len(tools))) + tools.ljust(4, b"\x00")
    checksum = zlib.crc32(survey).to_bytes(4, "big")
    return bytes(_RS_CODEC.encode(survey + checksum))


def _verified_payload(payload: bytes) -> bytes | None:
    """Correct up to two erroneous bytes, then check the survey and CRC."""
    if len(payload) != _PAYLOAD_BYTES:
        return None
    try:
        message, corrected, _ = _RS_CODEC.decode(payload)
    except ReedSolomonError:
        return None
    survey, checksum = message[:6], message[6:]
    if zlib.crc32(survey).to_bytes(4, "big") != checksum:
        return None
    status, tool_count = survey[:2]
    if status not in STATUS_CODES.values() or not 1 <= tool_count <= 4:
        return None
    tools = survey[2:2 + tool_count]
    if len(set(tools)) != tool_count or any(tool not in TOOL_CODES.values() for tool in tools):
        return None
    if any(survey[2 + tool_count:]):
        return None
    return bytes(corrected)


def _open_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(BytesIO(data))
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        raise WatermarkInputError("The file could not be read as an image.") from exc
    try:
        if image.format not in _IMAGE_FORMATS:
            raise UnsupportedImageError("Only PNG and JPEG images are supported.")
        # Decode JPEG directly as YCbCr so RGB conversion does not clip its luma.
        if image.format == "PNG":
            _png_end_offset(data)
        elif image.mode == "RGB":
            image.draft("YCbCr", image.size)
        image.load()
        return image
    except WatermarkInputError:
        image.close()
        raise
    except (OSError, ValueError, SyntaxError) as exc:
        image.close()
        raise WatermarkInputError("The image is damaged or incomplete.") from exc


def validate_image(data: bytes) -> str:
    """Decode an uploaded image and return its format."""
    with _open_image(data) as image:
        return image.format


def _bit_locations(capacity: int):
    """Distribute repeated payload bits evenly over the available slots."""
    repeats = min(_MAX_REPEATS, capacity // _PAYLOAD_BITS)
    if not repeats:
        return
    if repeats % 2 == 0:
        repeats -= 1
    votes = repeats * _PAYLOAD_BITS
    for index in range(votes):
        yield index % _PAYLOAD_BITS, index * capacity // votes


def _read_votes(votes) -> tuple[bytes | None, float]:
    signed = np.zeros(_PAYLOAD_BITS, dtype=int)
    repeats = 0
    for index, value in votes:
        signed[index] += 1 if value else -1
        repeats += index == 0
    if not repeats:
        return None, 0.0
    encoded = np.packbits(signed > 0).tobytes()
    payload = _verified_payload(encoded)
    confidence = float(np.abs(signed).mean() / repeats) if payload is not None else 0.0
    return payload, confidence


def _png_slots(image: Image.Image):
    channels = 1 if image.mode in ("L", "LA") else 3
    for bit, slot in _bit_locations(image.width * image.height * channels):
        pixel, channel = divmod(slot, channels)
        row, column = divmod(pixel, image.width)
        yield bit, column, row, channel


def _png_mode(image: Image.Image) -> str | None:
    if image.mode in ("RGB", "RGBA", "L", "LA"):
        if "transparency" not in image.info:
            return image.mode
    return None


def _embed_png(image: Image.Image, bits: np.ndarray) -> bytes | None:
    if _png_mode(image) is None or not any(_png_slots(image)):
        return None
    pixels = image.load()
    for bit, x, y, channel in _png_slots(image):
        if image.mode == "L":
            pixels[x, y] = (pixels[x, y] & 0xFE) | int(bits[bit])
        else:
            values = list(pixels[x, y])
            values[channel] = (values[channel] & 0xFE) | int(bits[bit])
            pixels[x, y] = tuple(values)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _read_png(image: Image.Image) -> tuple[bytes | None, float]:
    if _png_mode(image) is None:
        return None, 0.0
    pixels = image.load()
    if image.mode == "L":
        return _read_votes((bit, pixels[x, y] & 1) for bit, x, y, _ in _png_slots(image))
    return _read_votes((bit, pixels[x, y][channel] & 1) for bit, x, y, channel in _png_slots(image))


def _jpeg_slots(image: Image.Image):
    blocks_wide = image.width // _BLOCK_SIZE
    blocks_high = image.height // _BLOCK_SIZE
    blocks = {}
    for bit, slot in _bit_locations(blocks_wide * blocks_high * len(_COEFFICIENTS)):
        block, coefficient = divmod(slot, len(_COEFFICIENTS))
        row, column = divmod(block, blocks_wide)
        coordinates = (column * _BLOCK_SIZE, row * _BLOCK_SIZE)
        blocks.setdefault(coordinates, []).append((bit, _COEFFICIENTS[coefficient]))
    return blocks


def _luminance(image: Image.Image) -> Image.Image:
    return image if image.mode == "L" else image.getchannel("Y")


def _coefficients(luma: Image.Image, x: int, y: int) -> np.ndarray:
    patch = luma.crop((x, y, x + _BLOCK_SIZE, y + _BLOCK_SIZE))
    return _DCT @ np.asarray(patch, dtype=float) @ _DCT.T


def _embed_jpeg(image: Image.Image, bits: np.ndarray) -> bytes | None:
    slots = _jpeg_slots(image)
    if image.mode not in ("L", "YCbCr") or not slots:
        return None
    luma = _luminance(image)
    for (x, y), coefficients in slots.items():
        frequencies = _coefficients(luma, x, y)
        for bit, coordinate in coefficients:
            value = frequencies[coordinate] / _COEFFICIENT_STEP
            bucket = 2 * round((value - int(bits[bit])) / 2) + int(bits[bit])
            frequencies[coordinate] = bucket * _COEFFICIENT_STEP
        patch = _DCT.T @ frequencies @ _DCT
        # Shift the block just enough to retain the adjusted AC values at black
        # or white when the full block still fits in the 8-bit range.
        minimum, maximum = patch.min(), patch.max()
        if maximum - minimum <= 255:
            patch += np.clip(0, -minimum, 255 - maximum)
        pixels = np.clip(np.rint(patch), 0, 255).astype(np.uint8)
        luma.paste(Image.fromarray(pixels), (x, y))
    if image.mode == "YCbCr":
        image = Image.merge("YCbCr", (luma, image.getchannel("Cb"), image.getchannel("Cr")))
    output = BytesIO()
    image.save(output, format="JPEG", quality=95, subsampling=0)
    return output.getvalue()


def _read_jpeg(image: Image.Image) -> tuple[bytes | None, float]:
    if image.mode not in ("L", "YCbCr"):
        return None, 0.0
    luma = _luminance(image)

    def votes():
        for (x, y), coefficients in _jpeg_slots(image).items():
            frequencies = _coefficients(luma, x, y)
            for bit, coordinate in coefficients:
                yield bit, round(frequencies[coordinate] / _COEFFICIENT_STEP) % 2

    return _read_votes(votes())


def _png_end_offset(data: bytes) -> int:
    # Follow chunk lengths from the PNG signature to the actual IEND.
    offset = 8
    while offset + 12 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        if kind == b"IEND" and size == 0:
            return offset
        offset += size + 12
    raise WatermarkInputError("The PNG file is missing its end chunk.")


def _embed_metadata(data: bytes, image_format: str, payload: bytes) -> bytes:
    if image_format == "JPEG":
        segment = _JPEG_PREFIX + payload
        # Keep the JFIF APP0 marker immediately after the JPEG start marker.
        offset = 2
        if data[offset:offset + 2] == b"\xff\xe0":
            offset += 2 + struct.unpack_from(">H", data, offset + 2)[0]
        marker = b"\xff\xef" + struct.pack(">H", len(segment) + 2) + segment
        return data[:offset] + marker + data[offset:]
    content = b"tEXt" + _PNG_KEY.encode("ascii") + b"\x00" + payload.hex().encode("ascii")
    chunk = struct.pack(">I", len(content) - 4) + content + struct.pack(">I", zlib.crc32(content))
    offset = _png_end_offset(data)
    return data[:offset] + chunk + data[offset:]


def _metadata_payload(image: Image.Image) -> bytes | None:
    if image.format == "JPEG":
        for name, value in image.applist:
            if name == "APP15" and value.startswith(_JPEG_PREFIX):
                payload = _verified_payload(value[len(_JPEG_PREFIX):])
                if payload is not None:
                    return payload
        return None
    encoded = image.text.get(_PNG_KEY)
    if encoded is not None:
        try:
            return _verified_payload(bytes.fromhex(encoded))
        except ValueError:
            pass
    return None


def watermark_image(data: bytes, payload: bytes) -> bytes:
    """Embed a survey and return marked image bytes in the original format."""
    if _verified_payload(payload) != payload:
        raise WatermarkInputError("The survey payload is invalid.")
    bits = np.unpackbits(np.frombuffer(payload, dtype=np.uint8))
    with _open_image(data) as image:
        image_format = image.format
        # Preserve uncommon encodings and animations with the container marker.
        if image_format == "PNG" and not image.is_animated and not image.info:
            marked = _embed_png(image, bits)
            if marked is not None:
                return marked
        if image_format == "JPEG":
            if "exif" not in image.info and "icc_profile" not in image.info:
                marked = _embed_jpeg(image, bits)
                if marked is not None and detect_image(marked).get("payload") == payload.hex():
                    return marked
        return _embed_metadata(data, image_format, payload)


def detect_image(data: bytes) -> dict:
    """Report the decoded payload and its pre-correction vote consistency."""
    with _open_image(data) as image:
        payload = _metadata_payload(image)
        if payload is not None:
            confidence = 1.0
        elif image.format == "JPEG":
            payload, confidence = _read_jpeg(image)
        else:
            payload, confidence = _read_png(image)
    result = {"detected": payload is not None, "confidence": confidence, "mediaKind": "image"}
    if payload is not None:
        result["payload"] = payload.hex()
    return result
