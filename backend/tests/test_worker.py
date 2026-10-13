"""In-process worker runs one queued draft at a time (SPEC §3.1, §13.1)."""

import asyncio
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.main import create_app
from app.models.database import AnalysisRow, Base, BookRow, create_sqlite_engine, run_in_session
from app.persistence.analyses import (
    add_uploaded_image,
    create_book,
    create_draft,
    mark_images_pending,
    mark_queued,
)
from app.schemas.vision import VisionRequest, VisionResponse
from app.services import worker as worker_module
from app.services.vision.fake import FakeVisionProvider
from app.services.worker import requeue_interrupted_drafts, run_one_queued_draft

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "vision"
_KEPT_BOOK = '{"lender_groups":[{"loans":[{"loan_internal_id":"lon_kept"}]}]}'
_TERMINAL = {"completed", "completed_needs_review"}
_INTERRUPTED = (
    "running",
    "preprocessing",
    "classifying",
    "extracting",
    "grouping",
    "validating",
)


class SwitchingFakeVisionProvider:
    """Fake fixtures chosen by task."""

    provider_name = "fake"
    model_name = "fake"

    def __init__(self, classify_dir: Path, extract_dir: Path) -> None:
        self.classify_provider = FakeVisionProvider(fixture_dir=classify_dir)
        self.extract_provider = FakeVisionProvider(fixture_dir=extract_dir)
        self.calls: list[VisionRequest] = []

    async def analyze_images(self, request: VisionRequest) -> VisionResponse:
        self.calls.append(request)
        if request.task == "classify":
            return await self.classify_provider.analyze_images(request)
        if request.task == "extract":
            return await self.extract_provider.analyze_images(request)
        raise AssertionError(request.task)


class _SessionProbe(Exception):
    """Raised after the vision entry point checks that SQLite is not locked."""


class _LockProbeProvider:
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


def _template(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _write_fixture(directory: Path, image_id: str, template_name: str) -> None:
    payload = _template(template_name)
    for item in payload["results"]:
        item["image_id"] = image_id
    (directory / f"{image_id}.json").write_text(json.dumps(payload), encoding="utf-8")


@contextmanager
def _client(
    tmp_path: Path,
    *,
    worker_enabled: bool,
) -> Iterator[tuple[TestClient, SwitchingFakeVisionProvider, Path, Path]]:
    classify_dir = tmp_path / "classify"
    extract_dir = tmp_path / "extract"
    classify_dir.mkdir()
    extract_dir.mkdir()
    provider = SwitchingFakeVisionProvider(classify_dir, extract_dir)
    db_path = tmp_path / "loanlens.db"
    settings = Settings(
        loanlens_env="test",
        loanlens_database_url=f"sqlite:///{db_path.as_posix()}",
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        loanlens_analysis_ttl_hours=24,
        loanlens_max_image_bytes=10_485_760,
        vision_provider="fake",
        loanlens_worker_enabled=worker_enabled,
        _env_file=None,
    )
    application = create_app(settings)
    application.state.vision_provider = provider
    try:
        with TestClient(application) as test_client:
            yield test_client, provider, classify_dir, extract_dir
    finally:
        application.state.engine.dispose()


def _draft_on_book(client: TestClient, book_id: str) -> str:
    draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert draft.status_code == 201
    return draft.json()["analysis_id"]


def _book(client: TestClient) -> str:
    created = client.post("/api/v1/books", json={})
    assert created.status_code == 201
    return created.json()["book_id"]


def _upload(client: TestClient, analysis_id: str) -> str:
    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[("files", ("page.png", _page_png(), "image/png"))],
    )
    assert uploaded.status_code == 201
    return uploaded.json()["images"][0]["image_id"]


def _enqueue(
    client: TestClient,
    book_id: str,
    classify_dir: Path,
    extract_dir: Path,
) -> str:
    analysis_id = _draft_on_book(client, book_id)
    image_id = _upload(client, analysis_id)
    _write_fixture(classify_dir, image_id, "classify_loan_document.json")
    _write_fixture(extract_dir, image_id, "extract_loan_document.json")
    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})
    assert analyzed.status_code == 202
    assert analyzed.json()["status"] == "queued"
    return analysis_id


def _session(client: TestClient) -> Session:
    return client.app.state.session_factory()


def _set_fields(client: TestClient, analysis_id: str, **fields: object) -> None:
    session = _session(client)
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        session.commit()
    finally:
        session.close()


def _row(client: TestClient, analysis_id: str) -> AnalysisRow:
    session = _session(client)
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        session.expunge(row)
        return row
    finally:
        session.close()


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


def test_worker_enabled_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOANLENS_WORKER_ENABLED", raising=False)
    settings = Settings(_env_file=None)
    assert settings.loanlens_worker_enabled is True


def test_worker_does_not_use_a_broker_or_accept() -> None:
    source = Path(worker_module.__file__).read_text(encoding="utf-8").lower()
    for name in ("kafka", "celery", "redis", "accept_draft"):
        assert name not in source


def test_run_one_finishes_only_the_older_draft_and_leaves_the_book(tmp_path: Path) -> None:
    with _client(tmp_path, worker_enabled=False) as (client, provider, classify_dir, extract_dir):
        book_id = _book(client)
        older = _enqueue(client, book_id, classify_dir, extract_dir)
        newer = _enqueue(client, book_id, classify_dir, extract_dir)
        _set_fields(client, older, queued_at="2026-09-25T10:00:00Z")
        _set_fields(client, newer, queued_at="2026-09-25T10:00:01Z")
        _keep_book(client, book_id)

        worked = asyncio.run(
            run_one_queued_draft(
                client.app.state.session_factory,
                settings=client.app.state.settings,
                provider=provider,
            )
        )

        assert worked is True
        assert _row(client, older).status in _TERMINAL
        assert _row(client, older).status != "accepted"
        newer_row = _row(client, newer)
        assert newer_row.status == "queued"
        assert newer_row.queued_at == "2026-09-25T10:00:01Z"
        assert len(provider.calls) == 2
        assert _book_result(client, book_id) == _KEPT_BOOK

        again = asyncio.run(
            run_one_queued_draft(
                client.app.state.session_factory,
                settings=client.app.state.settings,
                provider=provider,
            )
        )
        assert again is True
        assert _row(client, newer).status in _TERMINAL
        assert _row(client, newer).status != "accepted"
        assert len(provider.calls) == 4
        assert _book_result(client, book_id) == _KEPT_BOOK

        idle = asyncio.run(
            run_one_queued_draft(
                client.app.state.session_factory,
                settings=client.app.state.settings,
                provider=provider,
            )
        )
        assert idle is False
        assert len(provider.calls) == 4
        assert _book_result(client, book_id) == _KEPT_BOOK


@pytest.mark.parametrize("status", _INTERRUPTED)
def test_requeue_moves_a_stuck_pipeline_draft_back_to_queued(
    tmp_path: Path,
    status: str,
) -> None:
    with _client(tmp_path, worker_enabled=False) as (client, provider, _classify_dir, _extract_dir):
        book_id = _book(client)
        analysis_id = _draft_on_book(client, book_id)
        _keep_book(client, book_id)
        _set_fields(
            client,
            analysis_id,
            status=status,
            queued_at="2026-09-25T09:00:00Z",
            pages_done=2,
            pages_total=3,
            result_json='{"lender_groups":[]}',
        )

        count = run_in_session(client.app.state.session_factory, requeue_interrupted_drafts)

        row = _row(client, analysis_id)
        assert count == 1
        assert row.status == "queued"
        assert row.queued_at == "2026-09-25T09:00:00Z"
        assert row.pages_done == 2
        assert row.pages_total == 3
        assert row.result_json == '{"lender_groups":[]}'
        assert provider.calls == []
        assert _book_result(client, book_id) == _KEPT_BOOK


def test_requeue_leaves_queued_and_finished_drafts(tmp_path: Path) -> None:
    with _client(tmp_path, worker_enabled=False) as (client, _provider, _classify_dir, _extract_dir):
        book_id = _book(client)
        queued_id = _draft_on_book(client, book_id)
        finished_id = _draft_on_book(client, book_id)
        _set_fields(client, queued_id, status="queued", queued_at="2026-09-25T08:00:00Z", pages_done=1)
        _set_fields(
            client,
            finished_id,
            status="completed",
            queued_at="2026-09-25T07:00:00Z",
            pages_done=1,
            result_json=_KEPT_BOOK,
        )

        count = run_in_session(client.app.state.session_factory, requeue_interrupted_drafts)

        assert count == 0
        assert _row(client, queued_id).status == "queued"
        assert _row(client, queued_id).queued_at == "2026-09-25T08:00:00Z"
        assert _row(client, queued_id).pages_done == 1
        assert _row(client, finished_id).status == "completed"
        assert _row(client, finished_id).result_json == _KEPT_BOOK


def test_run_one_closes_the_database_before_vision(tmp_path: Path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{(tmp_path / 'loanlens.db').as_posix()}")

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
        book.result_json = _KEPT_BOOK
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
        mark_queued(session, draft, pages_total=1)
        analysis_id = draft.id
        book_id = book.id
        session.commit()
    finally:
        session.close()

    provider = _LockProbeProvider(factory)
    settings = Settings(
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        loanlens_max_image_bytes=10_485_760,
        loanlens_worker_enabled=False,
        _env_file=None,
    )
    try:
        with pytest.raises(_SessionProbe):
            asyncio.run(
                run_one_queued_draft(
                    factory,
                    settings=settings,
                    provider=provider,
                )
            )
        assert provider.calls == 1
        assert provider.lock_released is True
        check = factory()
        try:
            row = check.get(AnalysisRow, analysis_id)
            stored = check.get(BookRow, book_id)
            assert row is not None
            assert row.status != "accepted"
            assert stored is not None
            assert stored.result_json == _KEPT_BOOK
        finally:
            check.close()
    finally:
        engine.dispose()


def test_enabled_lifespan_runs_the_queued_draft(tmp_path: Path) -> None:
    with _client(tmp_path, worker_enabled=True) as (client, provider, classify_dir, extract_dir):
        book_id = _book(client)
        _keep_book(client, book_id)
        analysis_id = _enqueue(client, book_id, classify_dir, extract_dir)
        deadline = time.monotonic() + 5
        status = "queued"
        while time.monotonic() < deadline:
            status = _row(client, analysis_id).status
            if status in _TERMINAL:
                break
            time.sleep(0.05)

        assert status in _TERMINAL
        assert status != "accepted"
        assert provider.calls
        assert _book_result(client, book_id) == _KEPT_BOOK


def test_disabled_lifespan_leaves_the_draft_queued(tmp_path: Path) -> None:
    with _client(tmp_path, worker_enabled=False) as (client, provider, classify_dir, extract_dir):
        book_id = _book(client)
        _keep_book(client, book_id)
        analysis_id = _enqueue(client, book_id, classify_dir, extract_dir)
        time.sleep(0.4)

        assert _row(client, analysis_id).status == "queued"
        assert provider.calls == []
        assert _book_result(client, book_id) == _KEPT_BOOK
