"""Decode, validate, and preprocess draft images (SPEC §7.1, §7.4, §16)."""

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.errors import AppError
from app.services.images.upload import (
    allowed_content_types,
    draft_image_path,
    heic_decoder_available,
    image_too_large_message,
    normalize_content_type,
)

MIN_EDGE_PX = 200
VISION_LONGEST_EDGE = 2048
DEFAULT_MAX_IMAGE_BYTES = 10_485_760

_FORMAT_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "HEIF": "image/heic",
    "HEIC": "image/heic",
}

_HEIC_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heif", b"heim", b"heis", b"mif1", b"msf1"}
)

_DECODE_ERRORS = (
    UnidentifiedImageError,
    OSError,
    ValueError,
    SyntaxError,
    Image.DecompressionBombError,
)


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    content_type: str
    width: int
    height: int


def validate_image_bytes(
    data: bytes,
    *,
    content_type: str | None = None,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> ValidatedImage:
    """Decode `data` and reject disallowed type, oversize, unreadable, or tiny images.

    Both edges must be at least 200 px. A declared MIME, when given, must be on
    the allowlist and must match the type Pillow actually decoded.
    """
    declared = _declared_mime(content_type)
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise AppError("IMAGE_UNREADABLE")
    if len(data) > max_bytes:
        raise AppError("IMAGE_TOO_LARGE", image_too_large_message(max_bytes))

    fmt, width, height = _decode(bytes(data))
    mime = _FORMAT_MIME.get(fmt or "")
    if mime is None or mime not in allowed_content_types():
        raise AppError("IMAGE_UNSUPPORTED_TYPE")
    if declared is not None and declared != mime:
        raise AppError("IMAGE_UNSUPPORTED_TYPE")
    if width < MIN_EDGE_PX or height < MIN_EDGE_PX:
        raise AppError("IMAGE_TOO_SMALL")
    return ValidatedImage(content_type=mime, width=width, height=height)


def _declared_mime(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    declared = normalize_content_type(content_type)
    if declared not in allowed_content_types():
        raise AppError("IMAGE_UNSUPPORTED_TYPE")
    return declared


def _decode(data: bytes) -> tuple[str | None, int, int]:
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            width, height = image.size
            fmt = image.format
    except _DECODE_ERRORS as exc:
        if _is_heic_container(data) and not heic_decoder_available():
            raise AppError("IMAGE_UNSUPPORTED_TYPE") from exc
        raise AppError("IMAGE_UNREADABLE") from exc
    if width <= 0 or height <= 0:
        raise AppError("IMAGE_UNREADABLE")
    return fmt, width, height


def _is_heic_container(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS


@dataclass(frozen=True, slots=True)
class PreprocessedImage:
    content_type: str
    width: int
    height: int
    byte_size: int
    checksum_sha256: str
    storage_path: str


def preprocess_image(
    data: bytes,
    *,
    tmp_dir: Path,
    analysis_id: str,
    image_id: str,
    content_type: str | None = None,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> PreprocessedImage:
    """Strip metadata, cap the longest edge at 2048, and write one draft file.

    The file is `{tmp}/{analysis_id}/{image_id}`. Callers persist the path,
    checksum, and dimensions. Image bytes stay on disk.
    """
    validated = validate_image_bytes(data, content_type=content_type, max_bytes=max_bytes)
    stripped = _strip_metadata(data)
    fitted = _fit_longest_edge(stripped, VISION_LONGEST_EDGE)
    encoded, mime = _encode(fitted, validated.content_type)
    path = draft_image_path(tmp_dir, analysis_id, image_id)
    path.write_bytes(encoded)
    return PreprocessedImage(
        content_type=mime,
        width=fitted.size[0],
        height=fitted.size[1],
        byte_size=len(encoded),
        checksum_sha256=hashlib.sha256(encoded).hexdigest(),
        storage_path=str(path),
    )


def _strip_metadata(data: bytes) -> Image.Image:
    """Copy pixels only so GPS, EXIF, and other metadata are not kept."""
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            oriented = ImageOps.exif_transpose(image) or image
            if oriented.mode not in {"L", "LA", "RGB", "RGBA"}:
                oriented = oriented.convert("RGB")
            return Image.frombytes(oriented.mode, oriented.size, oriented.tobytes())
    except _DECODE_ERRORS as exc:
        raise AppError("IMAGE_UNREADABLE") from exc


def _fit_longest_edge(image: Image.Image, max_edge: int) -> Image.Image:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image
    if width >= height:
        new_size = (max_edge, max(1, round(height * max_edge / width)))
    else:
        new_size = (max(1, round(width * max_edge / height)), max_edge)
    return image.resize(new_size, Image.Resampling.LANCZOS)


def _encode(image: Image.Image, content_type: str) -> tuple[bytes, str]:
    if content_type == "image/jpeg":
        output_type, image_format = "image/jpeg", "JPEG"
        if image.mode != "RGB":
            image = image.convert("RGB")
    elif content_type == "image/png":
        output_type, image_format = "image/png", "PNG"
    elif content_type == "image/webp":
        output_type, image_format = "image/webp", "WEBP"
    elif content_type == "image/heic":
        output_type, image_format = "image/jpeg", "JPEG"
        if image.mode != "RGB":
            image = image.convert("RGB")
    else:
        raise AppError("IMAGE_UNSUPPORTED_TYPE")

    buffer = BytesIO()
    save_kwargs: dict[str, int] = {}
    if image_format in {"JPEG", "WEBP"}:
        save_kwargs["quality"] = 90
    image.save(buffer, format=image_format, **save_kwargs)
    return buffer.getvalue(), output_type
