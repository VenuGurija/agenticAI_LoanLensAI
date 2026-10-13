"""Per-image vision checkpoints (SPEC §6.5, §13.1, §17)."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.main import create_app
from app.models.database import AnalysisRow, BookRow, ImageRow
from app.schemas.vision import VisionRequest, VisionResponse
from app.services.vision.fake import FakeVisionProvider
from app.services.vision.openai_compatible import VisionTimeoutError
from app.services.worker import run_one_queued_draft

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "vision"
_KEPT_BOOK = '{"lender_groups":[{"loans":[{"loan_internal_id":"lon_kept"}]}]}'
_TERMINAL = {"completed", "completed_needs_review"}


class _SwitchingFakeVisionProvider:
    """Fake fixtures chosen by task. One image per call."""

    provider_name = "fake"
    model_name = "fake"

    def __init__(self, classify_dir: Path, extract_dir: Path) -> None:
        self.classify_provider = FakeVisionProvider(fixture_dir=classify_dir)
        self.extract_provider = FakeVisionProvider(fixture_dir=extract_dir)

    async def analyze_images(self, request: VisionRequest) -> VisionResponse:
        if request.task == "classify":
            return await self.classify_provider.analyze_images(request)
        if request.task == "extract":
            return await self.extract_provider.analyze_images(request)
        raise AssertionError(request.task)


class _FailingImageProvider:
    """Fails once on one image. Every call is a single image and sees a free database."""

    provider_name = "fake"
    model_name = "fake"

    def __init__(
        self,
        classify_dir: Path,
        extract_dir: Path,
        *,
        fail_task: str | None,
    ) -> None:
        self.inner = _SwitchingFakeVisionProvider(classify_dir, extract_dir)
        self.classify_dir = classify_dir
        self.extract_dir = extract_dir
        self.fail_task = fail_task
        self.fail_image_id: str | None = None
        self.failed_once = False
        self.calls: list[VisionRequest] = []
        self.factory: sessionmaker[Session] | None = None

    async def analyze_images(self, request: VisionRequest) -> VisionResponse:
        if len(request.images) != 1:
            raise AssertionError("each vision call must send one image")
        self._assert_database_is_free()
        image_id = request.images[0].image_id
        self.calls.append(request)
        if (
            image_id == self.fail_image_id
            and not self.failed_once
            and (self.fail_task is None or request.task == self.fail_task)
        ):
            self.failed_once = True
            raise VisionTimeoutError()
        return await self.inner.analyze_images(request)

    def _assert_database_is_free(self) -> None:
        if self.factory is None:
            return
        probe = self.factory()
        try:
            probe.execute(text("PRAGMA busy_timeout = 200"))
            probe.execute(text("UPDATE analyses SET pages_done = pages_done"))
            probe.commit()
        finally:
            probe.close()


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
    fail_task: str | None,
) -> Iterator[tuple[TestClient, _FailingImageProvider]]:
    classify_dir = tmp_path / "classify"
    extract_dir = tmp_path / "extract"
    classify_dir.mkdir()
    extract_dir.mkdir()
    provider = _FailingImageProvider(classify_dir, extract_dir, fail_task=fail_task)
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
    provider.factory = application.state.session_factory
    application.state.vision_provider = provider
    try:
        with TestClient(application) as test_client:
            yield test_client, provider
    finally:
        application.state.engine.dispose()


def _book(client: TestClient) -> str:
    created = client.post("/api/v1/books", json={})
    assert created.status_code == 201
    return created.json()["book_id"]


def _keep_book(client: TestClient, book_id: str) -> None:
    session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        book.result_json = _KEPT_BOOK
        session.commit()
    finally:
        session.close()


def _book_result(client: TestClient, book_id: str) -> str | None:
    session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        return book.result_json
    finally:
        session.close()


def _upload(client: TestClient, analysis_id: str, classify_dir: Path, extract_dir: Path) -> str:
    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[("files", ("page.png", _page_png(), "image/png"))],
    )
    assert uploaded.status_code == 201
    image_id = uploaded.json()["images"][0]["image_id"]
    _write_fixture(classify_dir, image_id, "classify_loan_document.json")
    _write_fixture(extract_dir, image_id, "extract_loan_document.json")
    return image_id


def _enqueue_two(client: TestClient, provider: _FailingImageProvider) -> tuple[str, str, str, str]:
    book_id = _book(client)
    _keep_book(client, book_id)
    draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert draft.status_code == 201
    analysis_id = draft.json()["analysis_id"]
    classify_dir = provider.classify_dir
    extract_dir = provider.extract_dir
    _upload(client, analysis_id, classify_dir, extract_dir)
    _upload(client, analysis_id, classify_dir, extract_dir)
    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})
    assert analyzed.status_code == 202
    assert analyzed.json()["status"] == "queued"
    images = client.get(f"/api/v1/analyses/{analysis_id}").json()["images"]
    assert len(images) == 2
    return book_id, analysis_id, images[0]["image_id"], images[1]["image_id"]


def _row(client: TestClient, analysis_id: str) -> AnalysisRow:
    session = client.app.state.session_factory()
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        session.expunge(row)
        return row
    finally:
        session.close()


def _stored(client: TestClient, analysis_id: str) -> dict:
    raw = _row(client, analysis_id).result_json
    assert raw is not None
    loaded = json.loads(raw)
    assert isinstance(loaded, dict)
    return loaded


def _image(client: TestClient, image_id: str) -> ImageRow:
    session = client.app.state.session_factory()
    try:
        row = session.get(ImageRow, image_id)
        assert row is not None
        session.expunge(row)
        return row
    finally:
        session.close()


def _run(client: TestClient, provider: _FailingImageProvider) -> bool:
    return asyncio.run(
        run_one_queued_draft(
            client.app.state.session_factory,
            settings=client.app.state.settings,
            provider=provider,
        )
    )


def _call_pairs(calls: list[VisionRequest]) -> list[tuple[str, str]]:
    return [(call.task, call.images[0].image_id) for call in calls]


def test_second_image_failure_keeps_the_first_page_and_retry_skips_it(tmp_path: Path) -> None:
    with _client(tmp_path, fail_task=None) as (client, provider):
        book_id, analysis_id, first_id, second_id = _enqueue_two(client, provider)
        provider.fail_image_id = second_id

        assert _run(client, provider) is True

        row = _row(client, analysis_id)
        stored = _stored(client, analysis_id)
        assert row.status == "failed"
        assert row.error_code == "VISION_TIMEOUT"
        assert row.pages_done == 1
        assert row.pages_total == 2
        assert _call_pairs(provider.calls) == [
            ("classify", first_id),
            ("extract", first_id),
            ("classify", second_id),
        ]
        assert [item["image_id"] for item in stored["classifications"]] == [first_id]
        extracts = stored["raw_extracts"]
        assert [item["image_id"] for item in extracts] == [first_id]
        loan = extracts[0]["loans"][0]
        assert loan["sanctioned_amount"]["value"] == "15000.00"
        assert loan["disbursed_amount"]["value"] is None
        assert second_id not in json.dumps(extracts)
        assert stored["lender_groups"] == []
        first = _image(client, first_id)
        second = _image(client, second_id)
        assert first.document_class == "loan_document"
        assert first.status == "classified"
        assert second.document_class is None
        fetched = client.get(f"/api/v1/analyses/{analysis_id}")
        assert fetched.status_code == 200
        assert "raw_extracts" not in fetched.text
        assert "15000" not in fetched.text
        assert fetched.json()["pages_done"] == 1
        assert fetched.json()["error"]["code"] == "VISION_TIMEOUT"
        assert _book_result(client, book_id) == _KEPT_BOOK

        again = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})
        assert again.status_code == 202
        assert again.json()["status"] == "queued"
        assert again.json()["pages_done"] == 1
        assert [item["image_id"] for item in _stored(client, analysis_id)["raw_extracts"]] == [first_id]
        before = len(provider.calls)

        assert _run(client, provider) is True

        later = provider.calls[before:]
        assert _call_pairs(later) == [
            ("classify", second_id),
            ("extract", second_id),
        ]
        finished = _row(client, analysis_id)
        assert finished.status in _TERMINAL
        assert finished.status != "accepted"
        assert finished.pages_done == 2
        assert finished.pages_total == 2
        done = _stored(client, analysis_id)
        assert [item["image_id"] for item in done["raw_extracts"]] == [first_id, second_id]
        assert _book_result(client, book_id) == _KEPT_BOOK


def test_retry_resumes_at_extract_when_classify_is_already_checkpointed(tmp_path: Path) -> None:
    with _client(tmp_path, fail_task="extract") as (client, provider):
        book_id, analysis_id, first_id, second_id = _enqueue_two(client, provider)
        provider.fail_image_id = second_id

        assert _run(client, provider) is True

        stored = _stored(client, analysis_id)
        assert _row(client, analysis_id).status == "failed"
        assert _row(client, analysis_id).pages_done == 1
        assert [item["image_id"] for item in stored["classifications"]] == [first_id, second_id]
        assert [item["image_id"] for item in stored["raw_extracts"]] == [first_id]
        assert _image(client, second_id).document_class == "loan_document"
        assert _call_pairs(provider.calls) == [
            ("classify", first_id),
            ("extract", first_id),
            ("classify", second_id),
            ("extract", second_id),
        ]
        assert _book_result(client, book_id) == _KEPT_BOOK

        again = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})
        assert again.status_code == 202
        before = len(provider.calls)

        assert _run(client, provider) is True

        assert _call_pairs(provider.calls[before:]) == [("extract", second_id)]
        finished = _row(client, analysis_id)
        assert finished.status in _TERMINAL
        assert finished.pages_done == 2
        assert [item["image_id"] for item in _stored(client, analysis_id)["raw_extracts"]] == [
            first_id,
            second_id,
        ]
        assert _book_result(client, book_id) == _KEPT_BOOK
