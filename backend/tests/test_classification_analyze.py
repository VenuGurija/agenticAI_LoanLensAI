"""Analyze outcomes for classify fixtures (SPEC §8.2, §13, §18)."""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from app.models.database import AnalysisRow, BookRow, ImageRow
from app.schemas.vision import VisionRequest, VisionResponse
from app.services.vision.fake import FakeVisionProvider

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "vision"
_PUBLIC_KEYS = {
    "analysis_id",
    "status",
    "created_at",
    "completed_at",
    "expires_at",
    "error",
    "pages_done",
    "pages_total",
    "queued_at",
    "images",
    "lender_groups",
}


_TERMINAL = {
    "completed",
    "completed_needs_review",
    "failed",
    "accepted",
    "expired",
}


def _finished(client: TestClient, analysis_id: str):
    """Wait until the in-process worker leaves the draft in a terminal status."""

    deadline = time.monotonic() + 5
    last_status = None
    fetched = None
    while time.monotonic() < deadline:
        fetched = client.get(f"/api/v1/analyses/{analysis_id}")
        assert fetched.status_code == 200
        last_status = fetched.json()["status"]
        if last_status in _TERMINAL:
            return fetched
        time.sleep(0.05)
    raise AssertionError(f"draft stayed {last_status}")


class SwitchingFakeVisionProvider:
    """Fake fixtures chosen by task. Counts every provider call."""

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
def _client(tmp_path: Path) -> Iterator[tuple[TestClient, SwitchingFakeVisionProvider, Path, Path]]:
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
        _env_file=None,
    )
    application = create_app(settings)
    application.state.vision_provider = provider
    try:
        with TestClient(application) as test_client:
            yield test_client, provider, classify_dir, extract_dir
    finally:
        application.state.engine.dispose()


@pytest.fixture
def analyze_app(tmp_path: Path) -> Iterator[tuple[TestClient, SwitchingFakeVisionProvider, Path, Path]]:
    with _client(tmp_path) as resources:
        yield resources


def _draft(client: TestClient) -> tuple[str, str]:
    book = client.post("/api/v1/books", json={})
    assert book.status_code == 201
    book_id = book.json()["book_id"]
    draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert draft.status_code == 201
    return book_id, draft.json()["analysis_id"]


def _upload(client: TestClient, analysis_id: str, names: list[str]) -> list[str]:
    png = _page_png()
    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[("files", (name, png, "image/png")) for name in names],
    )
    assert uploaded.status_code == 201
    return [image["image_id"] for image in uploaded.json()["images"]]


def _tasks(provider: SwitchingFakeVisionProvider) -> list[str]:
    return [call.task for call in provider.calls]


def _stored(client: TestClient, analysis_id: str) -> dict:
    session: Session = client.app.state.session_factory()
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        assert row.result_json is not None
        return json.loads(row.result_json)
    finally:
        session.close()


def _keep_book(client: TestClient, book_id: str) -> None:
    session: Session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        book.result_json = '{"lender_groups":[{"loans":[{"loan_internal_id":"lon_kept"}]}]}'
        session.commit()
    finally:
        session.close()


def _book_result(client: TestClient, book_id: str) -> str | None:
    session: Session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        return book.result_json
    finally:
        session.close()


def test_analyze_without_images_is_no_images(analyze_app) -> None:
    client, provider, _classify_dir, _extract_dir = analyze_app
    _book_id, analysis_id = _draft(client)

    rejected = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "NO_IMAGES"
    assert rejected.json()["error"]["retryable"] is False
    assert provider.calls == []
    fetched = client.get(f"/api/v1/analyses/{analysis_id}")
    assert fetched.json()["status"] == "created"
    assert fetched.json()["images"] == []


def test_missing_analysis_is_not_found(analyze_app) -> None:
    client, provider, _classify_dir, _extract_dir = analyze_app

    missing = client.post("/api/v1/analyses/anl_missing/analyze", json={})

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ANALYSIS_NOT_FOUND"
    assert provider.calls == []


def test_all_not_loan_documents_fail_without_extract(analyze_app) -> None:
    client, provider, classify_dir, _extract_dir = analyze_app
    assert isinstance(provider.classify_provider, FakeVisionProvider)
    book_id, analysis_id = _draft(client)
    image_id = _upload(client, analysis_id, ["receipt.png"])[0]
    _write_fixture(classify_dir, image_id, "classify_not_loan_document.json")
    _keep_book(client, book_id)

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 202
    assert analyzed.json()["status"] == "queued"
    finished = _finished(client, analysis_id)
    body = finished.json()
    assert set(body) == _PUBLIC_KEYS
    assert body["status"] == "failed"
    assert body["error"] == {
        "code": "NO_LOAN_DOCUMENTS",
        "message": "No loan documents were found.",
    }
    assert body["lender_groups"] == []
    assert body["completed_at"] is not None
    assert "raw_extracts" not in finished.text
    assert _tasks(provider) == ["classify"]
    assert provider.calls[0].response_schema_name == "classify"
    assert provider.calls[0].hint is None
    assert provider.calls[0].analysis_id == analysis_id
    stored = _stored(client, analysis_id)
    assert stored["lender_groups"] == []
    assert "raw_extracts" not in stored
    assert stored["classifications"][0]["document_class"] == "not_loan_document"
    assert _book_result(client, book_id) == (
        '{"lender_groups":[{"loans":[{"loan_internal_id":"lon_kept"}]}]}'
    )

    again = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert again.status_code == 200
    assert again.json() == body
    assert _tasks(provider) == ["classify"]


def test_only_unclear_needs_review_and_skips_extract(analyze_app) -> None:
    client, provider, classify_dir, _extract_dir = analyze_app
    _book_id, analysis_id = _draft(client)
    image_id = _upload(client, analysis_id, ["blur.png"])[0]
    _write_fixture(classify_dir, image_id, "classify_only_unclear.json")

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 202
    assert analyzed.json()["status"] == "queued"
    finished = _finished(client, analysis_id)
    body = finished.json()
    assert body["status"] == "completed_needs_review"
    assert body["error"] is None
    assert body["lender_groups"] == []
    assert body["images"][0]["status"] == "classified"
    assert "raw_extracts" not in finished.text
    assert _tasks(provider) == ["classify"]
    stored = _stored(client, analysis_id)
    assert stored["lender_groups"] == []
    assert "raw_extracts" not in stored
    assert stored["classifications"][0]["document_class"] == "unclear"

    session: Session = client.app.state.session_factory()
    try:
        image = session.get(ImageRow, image_id)
        assert image is not None
        assert image.document_class == "unclear"
        assert image.status == "classified"
    finally:
        session.close()

    again = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})
    assert again.json() == body
    assert _tasks(provider) == ["classify"]


def test_loan_document_stores_raw_extract_and_is_idempotent(analyze_app) -> None:
    client, provider, classify_dir, extract_dir = analyze_app
    assert isinstance(provider.extract_provider, FakeVisionProvider)
    book_id, analysis_id = _draft(client)
    image_id = _upload(client, analysis_id, ["sanction.png"])[0]
    _write_fixture(classify_dir, image_id, "classify_loan_document.json")
    _write_fixture(extract_dir, image_id, "extract_loan_document.json")
    _keep_book(client, book_id)

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 202
    assert analyzed.json()["status"] == "queued"
    finished = _finished(client, analysis_id)
    body = finished.json()
    assert body["status"] == "completed_needs_review"
    assert body["error"] is None
    assert body["completed_at"] is not None
    assert "raw_extracts" not in body
    assert "implied_fees_total" not in finished.text
    groups = body["lender_groups"]
    assert len(groups) == 1
    assert groups[0]["lender_brand"] == "Dhanrishi"
    preview = groups[0]["loans"][0]
    assert preview["image_ids"] == [image_id]
    assert preview["fields"]["sanctioned_amount"]["value"] == "15000.00"
    assert preview["fields"]["disbursed_amount"]["value"] is None
    assert "calculated" not in preview
    assert "implied_fees_total" not in preview
    assert _tasks(provider) == ["classify", "extract"]
    assert provider.calls[1].response_schema_name == "extract"
    assert provider.calls[1].hint is None
    assert [image.image_id for image in provider.calls[1].images] == [image_id]

    stored = _stored(client, analysis_id)
    assert stored["lender_groups"] == body["lender_groups"]
    extract = stored["raw_extracts"]
    assert len(extract) == 1
    assert extract[0]["image_id"] == image_id
    loan = extract[0]["loans"][0]
    assert loan["lender_brand"]["value"] == "Dhanrishi"
    assert loan["lender_brand"]["source"] == "extracted"
    assert loan["sanctioned_amount"]["value"] == "15000.00"
    assert loan["sanctioned_amount"]["currency"] == "INR"
    assert loan["sanctioned_amount"]["source"] == "extracted"
    assert loan["disbursed_amount"]["value"] is None
    assert loan["disbursed_amount"]["source"] == "unknown"
    assert loan["repayment_amount"]["value"] is None
    assert "implied_fees_total" not in loan
    assert "implied_cost" not in loan
    assert "extra_percent" not in loan
    assert _book_result(client, book_id) == (
        '{"lender_groups":[{"loans":[{"loan_internal_id":"lon_kept"}]}]}'
    )

    session: Session = client.app.state.session_factory()
    try:
        image = session.get(ImageRow, image_id)
        assert image is not None
        assert image.document_class == "loan_document"
        assert image.status == "classified"
        assert image.width == 200
        assert image.height == 200
        assert image.checksum_sha256
    finally:
        session.close()

    fetched = client.get(f"/api/v1/analyses/{analysis_id}")
    assert fetched.status_code == 200
    assert fetched.json() == body

    again = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})
    assert again.status_code == 200
    assert again.json() == body
    assert _tasks(provider) == ["classify", "extract"]


def test_unclear_image_is_not_extracted_beside_a_loan_document(analyze_app) -> None:
    client, provider, classify_dir, extract_dir = analyze_app
    _book_id, analysis_id = _draft(client)
    loan_id, unclear_id = _upload(client, analysis_id, ["sanction.png", "blur.png"])
    _write_fixture(classify_dir, loan_id, "classify_loan_document.json")
    _write_fixture(classify_dir, unclear_id, "classify_only_unclear.json")
    _write_fixture(extract_dir, loan_id, "extract_loan_document.json")

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

    assert analyzed.status_code == 202
    assert analyzed.json()["status"] == "queued"
    finished = _finished(client, analysis_id)
    body = finished.json()
    assert body["status"] == "completed_needs_review"
    assert len(body["lender_groups"]) == 1
    assert body["lender_groups"][0]["loans"][0]["image_ids"] == [loan_id]
    assert _tasks(provider) == ["classify", "extract", "classify"]
    extracted_ids = [image.image_id for call in provider.calls if call.task == "extract" for image in call.images]
    assert extracted_ids == [loan_id]
    stored = _stored(client, analysis_id)
    assert [item["image_id"] for item in stored["raw_extracts"]] == [loan_id]
    assert stored["lender_groups"] == body["lender_groups"]

    session: Session = client.app.state.session_factory()
    try:
        unclear = session.get(ImageRow, unclear_id)
        assert unclear is not None
        assert unclear.document_class == "unclear"
        assert unclear.status == "classified"
    finally:
        session.close()
