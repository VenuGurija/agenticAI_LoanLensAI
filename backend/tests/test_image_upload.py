import struct
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.errors import ERROR_SPECS
from app.main import create_app
from app.models.database import AnalysisRow, BookRow, ImageRow
from app.core.config import Settings
from app.services.images.upload import image_too_large_message, resolve_tmp_dir

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def tiny_png() -> bytes:
    """1×1 PNG generated in the test. No customer document bytes."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = zlib.compress(b"\x00\xff\x00\x00")
    return _PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


@contextmanager
def upload_client(tmp_path: Path, **overrides: object) -> Iterator[TestClient]:
    db_path = tmp_path / "loanlens.db"
    settings = Settings(
        loanlens_database_url=f"sqlite:///{db_path.as_posix()}",
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        loanlens_analysis_ttl_hours=24,
        loanlens_max_image_bytes=1024,
        _env_file=None,
        **overrides,
    )
    application = create_app(settings)
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        application.state.engine.dispose()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with upload_client(tmp_path) as test_client:
        yield test_client


def _draft(client: TestClient) -> tuple[str, str]:
    book = client.post("/api/v1/books", json={})
    assert book.status_code == 201
    book_id = book.json()["book_id"]
    draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert draft.status_code == 201
    return book_id, draft.json()["analysis_id"]


def _png_part(name: str = "screenshot.png", payload: bytes | None = None) -> tuple[str, tuple[str, bytes, str]]:
    return ("files", (name, tiny_png() if payload is None else payload, "image/png"))


def _error_code(response) -> str:
    return response.json()["error"]["code"]


def test_upload_one_image_then_get_shows_it(client: TestClient) -> None:
    _book_id, analysis_id = _draft(client)
    png = tiny_png()

    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[_png_part(payload=png)],
    )

    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["analysis_id"] == analysis_id
    assert body["status"] == "images_pending"
    assert len(body["images"]) == 1
    image = body["images"][0]
    assert set(image) == {"image_id", "filename", "content_type", "byte_size", "status"}
    assert image["image_id"].startswith("img_")
    assert image["filename"] == "screenshot.png"
    assert image["content_type"] == "image/png"
    assert image["byte_size"] == len(png)
    assert image["status"] == "uploaded"
    assert "storage_path" not in image

    fetched = client.get(f"/api/v1/analyses/{analysis_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "images_pending"
    assert fetched.json()["images"] == body["images"]

    session: Session = client.app.state.session_factory()
    try:
        row = session.get(ImageRow, image["image_id"])
        assert row is not None
        assert row.checksum_sha256 is None
        assert row.width is None
        assert row.height is None
        assert Path(row.storage_path).read_bytes() == png
        assert Path(row.storage_path).name == image["image_id"]
        assert Path(row.storage_path).parent.name == analysis_id
        assert png not in row.filename.encode()
        assert png not in (row.storage_path.encode())
    finally:
        session.close()


def test_oversize_image_is_rejected(client: TestClient) -> None:
    _book_id, analysis_id = _draft(client)
    payload = b"x" * 1025

    rejected = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[("files", ("big.png", payload, "image/png"))],
    )

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "IMAGE_TOO_LARGE"
    assert rejected.json()["error"]["retryable"] is False
    assert rejected.json()["error"]["message"] == "Each image must be 1024 bytes or smaller."
    assert rejected.json()["error"]["details"] == {"filename": "big.png"}

    fetched = client.get(f"/api/v1/analyses/{analysis_id}")
    assert fetched.json()["status"] == "created"
    assert fetched.json()["images"] == []


def test_ten_mib_limit_message_matches_spec() -> None:
    assert image_too_large_message(10 * 1024 * 1024) == ERROR_SPECS["IMAGE_TOO_LARGE"].message


def test_unsupported_type_is_rejected_and_batch_is_atomic(client: TestClient) -> None:
    _book_id, analysis_id = _draft(client)

    rejected = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[
            _png_part(),
            ("files", ("notes.txt", b"hello", "text/plain")),
        ],
    )

    assert rejected.status_code == 400
    assert _error_code(rejected) == "IMAGE_UNSUPPORTED_TYPE"
    assert rejected.json()["error"]["retryable"] is False
    assert rejected.json()["error"]["details"] == {"filename": "notes.txt"}

    heic = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[("files", ("shot.heic", b"not-heic", "image/heic"))],
    )
    assert heic.status_code == 400
    assert _error_code(heic) == "IMAGE_UNSUPPORTED_TYPE"

    fetched = client.get(f"/api/v1/analyses/{analysis_id}")
    assert fetched.json()["status"] == "created"
    assert fetched.json()["images"] == []
    stored = client.app.state.settings.loanlens_tmp_dir
    assert list(Path(stored).glob("*")) == []


def test_too_many_images_is_rejected(client: TestClient) -> None:
    _book_id, analysis_id = _draft(client)
    overflow = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[_png_part(name=f"page-{index}.png") for index in range(13)],
    )
    assert overflow.status_code == 400
    assert _error_code(overflow) == "TOO_MANY_IMAGES"
    assert overflow.json()["error"]["retryable"] is False
    assert client.get(f"/api/v1/analyses/{analysis_id}").json()["images"] == []

    accepted = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[_png_part(name=f"page-{index}.png") for index in range(12)],
    )
    assert accepted.status_code == 201
    assert len(accepted.json()["images"]) == 12

    extra = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[_png_part(name="one-more.png")],
    )
    assert extra.status_code == 400
    assert _error_code(extra) == "TOO_MANY_IMAGES"
    assert len(client.get(f"/api/v1/analyses/{analysis_id}").json()["images"]) == 12


def test_delete_image_removes_it_from_get_and_disk(client: TestClient) -> None:
    _book_id, analysis_id = _draft(client)
    first = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[_png_part(name="one.png"), _png_part(name="two.png")],
    )
    assert first.status_code == 201
    images = first.json()["images"]
    removed_id = images[0]["image_id"]

    session: Session = client.app.state.session_factory()
    try:
        stored_path = session.get(ImageRow, removed_id).storage_path
    finally:
        session.close()
    assert Path(stored_path).is_file()

    deleted = client.delete(f"/api/v1/analyses/{analysis_id}/images/{removed_id}")
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert not Path(stored_path).exists()

    fetched = client.get(f"/api/v1/analyses/{analysis_id}")
    assert fetched.json()["status"] == "images_pending"
    assert [image["image_id"] for image in fetched.json()["images"]] == [images[1]["image_id"]]

    missing = client.delete(f"/api/v1/analyses/{analysis_id}/images/{removed_id}")
    assert missing.status_code == 404
    assert _error_code(missing) == "NOT_FOUND"


def test_upload_and_delete_require_created_or_images_pending(client: TestClient) -> None:
    book_id, analysis_id = _draft(client)
    session: Session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        book.result_json = '{"accepted":true}'
        analysis = session.get(AnalysisRow, analysis_id)
        assert analysis is not None
        analysis.status = "completed"
        session.commit()
    finally:
        session.close()

    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[_png_part()],
    )
    assert uploaded.status_code == 409
    assert _error_code(uploaded) == "INVALID_STATUS"
    assert uploaded.json()["error"]["retryable"] is False

    deleted = client.delete(f"/api/v1/analyses/{analysis_id}/images/img_MISSING")
    assert deleted.status_code == 409
    assert _error_code(deleted) == "INVALID_STATUS"

    session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        assert book.result_json == '{"accepted":true}'
    finally:
        session.close()


def test_upload_does_not_change_accepted_book_loans(client: TestClient) -> None:
    book_id, analysis_id = _draft(client)
    session: Session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        book.result_json = '{"lender_groups":[{"loans":[{"loan_internal_id":"lon_kept"}]}]}'
        session.commit()
    finally:
        session.close()

    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[_png_part()],
    )
    assert uploaded.status_code == 201

    session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        assert book.result_json == '{"lender_groups":[{"loans":[{"loan_internal_id":"lon_kept"}]}]}'
    finally:
        session.close()

    got_book = client.get(f"/api/v1/books/{book_id}")
    assert got_book.status_code == 200
    kept = got_book.json()["lender_groups"][0]["loans"][0]
    assert kept["loan_internal_id"] == "lon_kept"
    assert kept["calculated"]["implied_cost"]["value"] is None
    assert kept["calculated"]["implied_cost"]["source"] == "unknown"


def test_client_filename_is_not_used_as_a_path(client: TestClient) -> None:
    _book_id, analysis_id = _draft(client)
    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[_png_part(name="../secret.png")],
    )
    assert uploaded.status_code == 201
    assert uploaded.json()["images"][0]["filename"] == "secret.png"
    image_id = uploaded.json()["images"][0]["image_id"]
    session: Session = client.app.state.session_factory()
    try:
        row = session.get(ImageRow, image_id)
        assert row is not None
        assert Path(row.storage_path).name == image_id
    finally:
        session.close()


def test_unknown_analysis_upload_and_delete(client: TestClient) -> None:
    uploaded = client.post(
        "/api/v1/analyses/anl_MISSING/images",
        files=[_png_part()],
    )
    assert uploaded.status_code == 404
    assert _error_code(uploaded) == "ANALYSIS_NOT_FOUND"

    deleted = client.delete("/api/v1/analyses/anl_MISSING/images/img_MISSING")
    assert deleted.status_code == 404
    assert _error_code(deleted) == "ANALYSIS_NOT_FOUND"


def test_deleting_the_draft_removes_image_files(client: TestClient) -> None:
    _book_id, analysis_id = _draft(client)
    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[_png_part()],
    )
    image_id = uploaded.json()["images"][0]["image_id"]
    session: Session = client.app.state.session_factory()
    try:
        stored_path = session.get(ImageRow, image_id).storage_path
    finally:
        session.close()

    deleted = client.delete(f"/api/v1/analyses/{analysis_id}")
    assert deleted.status_code == 204
    assert not Path(stored_path).exists()


def test_analyze_rejects_a_tiny_image_and_keeps_the_draft(client: TestClient) -> None:
    _book_id, analysis_id = _draft(client)
    client.post(f"/api/v1/analyses/{analysis_id}/images", files=[_png_part()])

    analyze = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyze.status_code == 202
    assert analyze.json()["status"] == "queued"
    deadline = time.monotonic() + 5
    fetched = client.get(f"/api/v1/analyses/{analysis_id}")
    while time.monotonic() < deadline and fetched.json()["status"] not in {"failed", "completed", "completed_needs_review"}:
        time.sleep(0.05)
        fetched = client.get(f"/api/v1/analyses/{analysis_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "failed"
    assert fetched.json()["error"]["code"] == "IMAGE_TOO_SMALL"
    assert fetched.json()["images"][0]["status"] == "failed"


def test_relative_tmp_dir_resolves_from_repo_root() -> None:
    resolved = resolve_tmp_dir("data/tmp/images")
    repo_root = Path(__file__).resolve().parents[2]
    assert resolved == (repo_root / "data" / "tmp" / "images").resolve()


def test_jpeg_and_webp_content_types_are_accepted(client: TestClient) -> None:
    _book_id, analysis_id = _draft(client)
    payload = tiny_png()
    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[
            ("files", ("a.jpg", payload, "image/jpeg")),
            ("files", ("b.webp", payload, "image/webp")),
        ],
    )
    assert uploaded.status_code == 201
    assert [image["content_type"] for image in uploaded.json()["images"]] == [
        "image/jpeg",
        "image/webp",
    ]
