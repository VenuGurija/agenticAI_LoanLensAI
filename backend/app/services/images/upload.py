"""Light upload checks and draft file layout (SPEC §7.2, §14.4).

Full decode, dimensions, EXIF stripping, and checksums belong to later sessions.
"""

from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.core.errors import ERROR_SPECS, AppError

_BASE_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_TEN_MIB = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PendingUpload:
    filename: str
    content_type: str
    data: bytes


def heic_decoder_available() -> bool:
    """HEIC is allowed only when a decoder is installed (SPEC §7.2)."""
    try:
        import pillow_heif  # noqa: F401
    except ImportError:
        return False
    return True


def allowed_content_types() -> frozenset[str]:
    types = set(_BASE_CONTENT_TYPES)
    if heic_decoder_available():
        types.add("image/heic")
    return frozenset(types)


def normalize_content_type(value: str | None) -> str:
    if not value:
        return ""
    return value.split(";", 1)[0].strip().lower()


def display_filename(name: str | None) -> str:
    """Keep a display name. Never use the client name as a storage path."""
    if not name:
        return "upload"
    base = Path(name).name.replace("\x00", "").strip()
    if base in {"", ".", ".."}:
        return "upload"
    return base[:255]


def image_too_large_message(max_bytes: int) -> str:
    if max_bytes == _TEN_MIB:
        return ERROR_SPECS["IMAGE_TOO_LARGE"].message
    if max_bytes > 0 and max_bytes % (1024 * 1024) == 0:
        return f"Each image must be {max_bytes // (1024 * 1024)} MiB or smaller."
    return f"Each image must be {max_bytes} bytes or smaller."


def read_upload(upload: UploadFile, max_bytes: int) -> PendingUpload:
    filename = display_filename(upload.filename)
    content_type = normalize_content_type(upload.content_type)
    if content_type not in allowed_content_types():
        raise AppError("IMAGE_UNSUPPORTED_TYPE", details={"filename": filename})
    try:
        data = upload.file.read(max_bytes + 1)
    finally:
        upload.file.close()
    if len(data) > max_bytes:
        raise AppError(
            "IMAGE_TOO_LARGE",
            image_too_large_message(max_bytes),
            details={"filename": filename},
        )
    return PendingUpload(filename=filename, content_type=content_type, data=data)


def resolve_tmp_dir(tmp_dir: str) -> Path:
    """Resolve `LOANLENS_TMP_DIR`. Relative paths are under the repository root."""
    path = Path(tmp_dir)
    if not path.is_absolute():
        repo_root = Path(__file__).resolve().parents[4]
        path = repo_root / path
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def draft_image_path(tmp_dir: Path, analysis_id: str, image_id: str) -> Path:
    """`{tmp}/{analysis_id}/{image_id}` using server-generated ids only."""
    _reject_path_segment(analysis_id)
    _reject_path_segment(image_id)
    root = tmp_dir.resolve()
    directory = (root / analysis_id).resolve()
    if not directory.is_relative_to(root):
        raise AppError("INTERNAL_ERROR")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / image_id


def book_image_path(tmp_dir: Path, book_id: str, loan_id: str, image_id: str) -> Path:
    """`{tmp}/books/{book_id}/{loan_id}/{image_id}` for an accepted screenshot."""
    _reject_path_segment(book_id)
    _reject_path_segment(loan_id)
    _reject_path_segment(image_id)
    root = tmp_dir.resolve()
    directory = (root / "books" / book_id / loan_id).resolve()
    if directory == root or not directory.is_relative_to(root):
        raise AppError("INTERNAL_ERROR")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / image_id


def remove_stored_file(tmp_dir: Path, storage_path: str) -> None:
    """Delete one draft file when it sits under the temp directory."""
    root = tmp_dir.resolve()
    try:
        path = Path(storage_path).resolve()
    except OSError:
        return
    if not path.is_relative_to(root):
        return
    if path.is_file():
        path.unlink()
    parent = path.parent
    if parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


def _reject_path_segment(segment: str) -> None:
    if segment in {"", ".", ".."} or "/" in segment or "\\" in segment:
        raise AppError("INTERNAL_ERROR")
