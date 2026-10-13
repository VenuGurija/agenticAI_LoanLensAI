"""POST /analyze queues a draft and does not call vision (SPEC §13.1, §14.5)."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.main import create_app
from app.models.database import AnalysisRow, Base, BookRow, create_sqlite_engine, run_in_session
from app.persistence.analyses import add_uploaded_image, create_book, create_draft, mark_images_pending
from app.schemas.vision import VisionRequest, VisionResponse
from app.services.classification import run_analysis
from app.services.vision.fake import FakeVisionProvider

_KEPT_BOOK = '{"lender_groups":[{"loans":[{"loan_internal_id":"lon_kept"}]}]}'
_IN_PROGRESS = (
    "queued",
    "running",
    "preprocessing",
    "classifying",
    "extracting",
    "grouping",
    "validating",
)


class CountingFakeVisionProvider(FakeVisionProvider):
    """Fake provider that records every call. Enqueue must leave the count at zero."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def analyze_images(self, request: VisionRequest) -> VisionResponse:
        self.calls += 1
        return await super().analyze_images(request)


class _SessionProbe(Exception):
    """Raised after the vision entry point checks that SQLite is not locked."""


class _LockProbeProvider:
    """Vision stand-in. A write must succeed before this call returns."""

    provider_name = "fake"
    model_name = "fake"

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.calls = 0
        self.lock_released = False

    async def analyze_images(self, request: VisionRequest) -> VisionResponse:
        self.calls += 1
        probe = self.factory()
        try:
            probe.execute(text("UPDATE analyses SET pages_done = pages_done"))
            probe.commit()
            self.lock_released = True
        finally:
            probe.close()
        raise _SessionProbe()


def _page_png() -> bytes:
    image = Image.new("RGB", (200, 200), (30, 90, 140))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@contextmanager
def _client(tmp_path: Path) -> Iterator[tuple[TestClient, CountingFakeVisionProvider]]:
    provider = CountingFakeVisionProvider()
    db_path = tmp_path / "loanlens.db"
    settings = Settings(
        loanlens_env="test",
        loanlens_database_url=f"sqlite:///{db_path.as_posix()}",
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        loanlens_analysis_ttl_hours=24,
        loanlens_max_image_bytes=10_485_760,
        vision_provider="fake",
        loanlens_worker_enabled=False,
        _env_file=None,
    )
    application = create_app(settings)
    application.state.vision_provider = provider
    try:
        with TestClient(application) as test_client:
            yield test_client, provider
    finally:
        application.state.engine.dispose()


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, CountingFakeVisionProvider]]:
    with _client(tmp_path) as resources:
        yield resources


def _draft(client: TestClient) -> tuple[str, str]:
    book = client.post("/api/v1/books", json={})
    assert book.status_code == 201
    book_id = book.json()["book_id"]
    draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert draft.status_code == 201
    return book_id, draft.json()["analysis_id"]


def _upload(client: TestClient, analysis_id: str) -> str:
    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[("files", ("page.png", _page_png(), "image/png"))],
    )
    assert uploaded.status_code == 201
    return uploaded.json()["images"][0]["image_id"]


def _session(client: TestClient) -> Session:
    return client.app.state.session_factory()


def _keep_book(client: TestClient, book_id: str) -> None:
    session = _session(client)
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        book.result_json = _KEPT_BOOK
        session.commit()
    finally:
        session.close()


def _book_result(client: TestClient, book_id: str) -> str | None:
    session = _session(client)
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        return book.result_json
    finally:
        session.close()


def _set_status(client: TestClient, analysis_id: str, **fields: object) -> None:
    session = _session(client)
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        session.commit()
    finally:
        session.close()


def _draft_count(client: TestClient, book_id: str) -> int:
    session = _session(client)
    try:
        rows = session.scalars(select(AnalysisRow).where(AnalysisRow.book_id == book_id)).all()
        return len(rows)
    finally:
        session.close()


def _stored_result(client: TestClient, analysis_id: str) -> str | None:
    session = _session(client)
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        return row.result_json
    finally:
        session.close()


def test_images_pending_analyze_is_queued_and_does_not_call_vision(api) -> None:
    client, provider = api
    book_id, analysis_id = _draft(client)
    _upload(client, analysis_id)
    _keep_book(client, book_id)

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 202
    body = analyzed.json()
    assert body["status"] == "queued"
    assert body["analysis_id"] == analysis_id
    assert body["pages_done"] == 0
    assert body["pages_total"] == 1
    assert isinstance(body["queued_at"], str)
    assert body["queued_at"].endswith("Z")
    assert body["error"] is None
    assert body["lender_groups"] == []
    assert provider.calls == 0
    assert _book_result(client, book_id) == _KEPT_BOOK
    assert _draft_count(client, book_id) == 1

    again = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert again.status_code == 202
    assert again.json()["status"] == "queued"
    assert again.json()["queued_at"] == body["queued_at"]
    assert again.json()["pages_done"] == 0
    assert again.json()["pages_total"] == 1
    assert provider.calls == 0
    assert _draft_count(client, book_id) == 1
    assert _book_result(client, book_id) == _KEPT_BOOK


@pytest.mark.parametrize("status", _IN_PROGRESS)
def test_in_progress_analyze_returns_the_current_draft(api, status: str) -> None:
    client, provider = api
    book_id, analysis_id = _draft(client)
    _upload(client, analysis_id)
    _keep_book(client, book_id)
    _set_status(
        client,
        analysis_id,
        status=status,
        queued_at="2026-09-25T10:00:00Z",
        pages_done=1,
        pages_total=4,
    )

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 202
    body = analyzed.json()
    assert body["status"] == status
    assert body["queued_at"] == "2026-09-25T10:00:00Z"
    assert body["pages_done"] == 1
    assert body["pages_total"] == 4
    assert provider.calls == 0
    assert _draft_count(client, book_id) == 1
    assert _book_result(client, book_id) == _KEPT_BOOK


@pytest.mark.parametrize("status", ("completed", "completed_needs_review"))
def test_finished_preview_is_returned_without_vision(api, status: str) -> None:
    client, provider = api
    book_id, analysis_id = _draft(client)
    _upload(client, analysis_id)
    _keep_book(client, book_id)
    stored = (
        '{"lender_groups":[{"lender_group_id":"ldg_kept","lender_brand":"Dhanrishi",'
        '"legal_lending_entity":null,"loans":[]}]}'
    )
    _set_status(
        client,
        analysis_id,
        status=status,
        result_json=stored,
        completed_at="2026-09-22T15:00:12Z",
        pages_done=2,
        pages_total=2,
        queued_at="2026-09-25T10:00:00Z",
    )

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 200
    body = analyzed.json()
    assert body["status"] == status
    assert body["completed_at"] == "2026-09-22T15:00:12Z"
    assert body["pages_done"] == 2
    assert body["pages_total"] == 2
    assert body["queued_at"] == "2026-09-25T10:00:00Z"
    assert body["lender_groups"][0]["lender_brand"] == "Dhanrishi"
    assert "raw_extracts" not in analyzed.text
    assert provider.calls == 0
    assert _book_result(client, book_id) == _KEPT_BOOK
    assert _stored_result(client, analysis_id) == stored


def test_retryable_failure_is_queued_again(api) -> None:
    client, provider = api
    book_id, analysis_id = _draft(client)
    _upload(client, analysis_id)
    _keep_book(client, book_id)
    checkpoint = '{"lender_groups":[],"raw_extracts":[{"image_id":"img_kept"}]}'
    _set_status(
        client,
        analysis_id,
        status="failed",
        error_code="VISION_TIMEOUT",
        error_message="The vision service timed out.",
        result_json=checkpoint,
        pages_done=1,
        pages_total=2,
        completed_at="2026-09-22T15:00:12Z",
    )

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 202
    body = analyzed.json()
    assert body["status"] == "queued"
    assert body["error"] is None
    assert body["completed_at"] is None
    assert body["pages_done"] == 1
    assert body["pages_total"] == 1
    assert body["queued_at"].endswith("Z")
    assert provider.calls == 0
    assert _stored_result(client, analysis_id) == checkpoint
    assert _draft_count(client, book_id) == 1
    assert _book_result(client, book_id) == _KEPT_BOOK


def test_non_retryable_failure_stays_failed(api) -> None:
    client, provider = api
    _book_id, analysis_id = _draft(client)
    _upload(client, analysis_id)
    _set_status(
        client,
        analysis_id,
        status="failed",
        error_code="NO_LOAN_DOCUMENTS",
        error_message="No loan documents were found.",
        completed_at="2026-09-22T15:00:12Z",
    )

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 200
    body = analyzed.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "NO_LOAN_DOCUMENTS"
    assert body["queued_at"] is None
    assert provider.calls == 0


def test_accepted_analyze_is_invalid_status(api) -> None:
    client, provider = api
    _book_id, analysis_id = _draft(client)
    _upload(client, analysis_id)
    _set_status(client, analysis_id, status="accepted", expires_at=None)

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 409
    assert analyzed.json()["error"]["code"] == "INVALID_STATUS"
    assert provider.calls == 0


def test_analyze_without_images_is_no_images(api) -> None:
    client, provider = api
    _book_id, analysis_id = _draft(client)

    rejected = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "NO_IMAGES"
    assert provider.calls == 0
    fetched = client.get(f"/api/v1/analyses/{analysis_id}")
    assert fetched.json()["status"] == "created"
    assert fetched.json()["pages_done"] == 0
    assert fetched.json()["pages_total"] == 0
    assert fetched.json()["queued_at"] is None


def test_missing_analysis_is_not_found(api) -> None:
    client, provider = api

    missing = client.post("/api/v1/analyses/anl_missing/analyze", json={})

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ANALYSIS_NOT_FOUND"
    assert provider.calls == 0


def test_existing_sqlite_database_gains_queue_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "loanlens.db"
    engine = create_sqlite_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE books (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    result_json TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE analyses (
                    id TEXT PRIMARY KEY,
                    book_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    expires_at TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    result_json TEXT,
                    FOREIGN KEY(book_id) REFERENCES books (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE images (
                    id TEXT PRIMARY KEY,
                    analysis_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    checksum_sha256 TEXT,
                    width INTEGER,
                    height INTEGER,
                    storage_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    document_class TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(analysis_id) REFERENCES analyses (id)
                )
                """
            )
        )
    engine.dispose()

    provider = CountingFakeVisionProvider()
    settings = Settings(
        loanlens_env="test",
        loanlens_database_url=f"sqlite:///{db_path.as_posix()}",
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        vision_provider="fake",
        loanlens_worker_enabled=False,
        _env_file=None,
    )
    application = create_app(settings)
    application.state.vision_provider = provider
    try:
        columns = {
            column["name"] for column in inspect(application.state.engine).get_columns("analyses")
        }
        assert {"queued_at", "pages_done", "pages_total"} <= columns
        with TestClient(application) as client:
            _book_id, analysis_id = _draft(client)
            _upload(client, analysis_id)
            analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})
            assert analyzed.status_code == 202
            assert analyzed.json()["status"] == "queued"
            assert provider.calls == 0
    finally:
        application.state.engine.dispose()


def test_run_in_session_closes_before_the_caller_continues(tmp_path: Path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{(tmp_path / 'loanlens.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    held: list[Session] = []

    def work(session: Session) -> str:
        held.append(session)
        return "ok"

    try:
        assert run_in_session(factory, work) == "ok"
        assert held[0].in_transaction() is False
    finally:
        engine.dispose()


def test_vision_call_can_write_the_database(tmp_path: Path) -> None:
    """The classify call must not run while a SQLAlchemy session holds SQLite."""

    db_path = tmp_path / "loanlens.db"
    engine = create_sqlite_engine(f"sqlite:///{db_path.as_posix()}")

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout = 1000")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    png = _page_png()
    image_path = tmp_path / "page.png"
    image_path.write_bytes(png)
    session = factory()
    try:
        book = create_book(session)
        draft = create_draft(session, book.id, 24)
        add_uploaded_image(
            session,
            image_id="img_probe",
            analysis_id=draft.id,
            filename="page.png",
            content_type="image/png",
            byte_size=len(png),
            storage_path=str(image_path),
        )
        mark_images_pending(session, draft)
        analysis_id = draft.id
        session.commit()
    finally:
        session.close()

    provider = _LockProbeProvider(factory)
    settings = Settings(
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        loanlens_max_image_bytes=10_485_760,
        _env_file=None,
    )
    try:
        with pytest.raises(_SessionProbe):
            asyncio.run(
                run_analysis(
                    factory,
                    analysis_id,
                    settings=settings,
                    provider=provider,
                )
            )
        assert provider.calls == 1
        assert provider.lock_released is True
    finally:
        engine.dispose()
