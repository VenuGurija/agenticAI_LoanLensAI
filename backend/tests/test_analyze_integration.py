"""Book flow with FakeVisionProvider (SPEC §13, §14, §21).

Create a book, analyze a multi-lender draft, accept it, accept a second draft,
and delete one loan. Vision fixtures are local JSON. No vendor HTTP.
"""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from app.models.database import AnalysisRow
from app.persistence.analyses import utc_timestamp
from app.schemas.vision import VisionRequest, VisionResponse
from app.services.vision.fake import FakeVisionProvider

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "vision"
_FIRST_TOTALS = {
    "received": "37870.00",
    "still_to_pay": "48150.00",
    "extra": "10280.00",
    "incomplete": False,
}
_WITH_FIBE_TOTALS = {
    "received": "38870.00",
    "still_to_pay": "49250.00",
    "extra": "10380.00",
    "incomplete": False,
}


class SwitchingFakeVisionProvider:
    """Classify and extract fixtures from disk. This provider does not use HTTP."""

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
    settings = Settings(
        loanlens_env="test",
        loanlens_database_url=f"sqlite:///{(tmp_path / 'loanlens.db').as_posix()}",
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        loanlens_analysis_ttl_hours=24,
        vision_provider="fake",
        vision_api_key="",
        vision_base_url="",
        vision_model="",
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
def analyze_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, SwitchingFakeVisionProvider, Path, Path]]:
    async def refuse_vendor_http(self: object, *args: object, **kwargs: object) -> object:
        raise AssertionError("real vision HTTP is not allowed")

    monkeypatch.setattr(httpx.AsyncClient, "post", refuse_vendor_http)
    with _client(tmp_path) as resources:
        yield resources


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


def _draft(client: TestClient, book_id: str) -> str:
    draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert draft.status_code == 201
    return draft.json()["analysis_id"]


def _upload_fixtures(
    client: TestClient,
    analysis_id: str,
    classify_dir: Path,
    extract_dir: Path,
    pages: list[tuple[str, str]],
) -> list[str]:
    png = _page_png()
    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[("files", (name, png, "image/png")) for name, _template_name in pages],
    )
    assert uploaded.status_code == 201
    image_ids = [image["image_id"] for image in uploaded.json()["images"]]
    assert len(image_ids) == len(pages)
    for image_id, (_name, template_name) in zip(image_ids, pages, strict=True):
        _write_fixture(classify_dir, image_id, "classify_loan_document.json")
        _write_fixture(extract_dir, image_id, template_name)
    return image_ids


def _loans(body: dict) -> list[dict]:
    found: list[dict] = []
    for group in body["lender_groups"]:
        found.extend(group["loans"])
    return found


def _loan_by_external(body: dict, external_id: str) -> dict:
    matches = [loan for loan in _loans(body) if loan.get("external_loan_id") == external_id]
    assert len(matches) == 1
    return matches[0]


def _past() -> str:
    return utc_timestamp(datetime.now(timezone.utc) - timedelta(hours=25))


def _set_expires_at(client: TestClient, analysis_id: str, expires_at: str) -> None:
    session: Session = client.app.state.session_factory()
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        row.expires_at = expires_at
        session.commit()
    finally:
        session.close()


def test_fake_provider_analyze_accepts_into_the_book_and_delete_keeps_the_rest(analyze_app) -> None:
    client, provider, classify_dir, extract_dir = analyze_app
    assert provider.provider_name == "fake"
    assert provider.model_name == "fake"
    book = client.post("/api/v1/books", json={})
    assert book.status_code == 201
    book_id = book.json()["book_id"]
    analysis_id = _draft(client, book_id)
    image_ids = _upload_fixtures(
        client,
        analysis_id,
        classify_dir,
        extract_dir,
        [
            ("dhanrishi-sanction.png", "extract_dhanrishi_sanction.json"),
            ("dhanrishi-received.png", "extract_dhanrishi_received.json"),
            ("dhanrishi-second.png", "extract_dhanrishi_second.json"),
            ("snappaisa.png", "extract_snappaisa.json"),
        ],
    )

    preview = client.get(f"/api/v1/books/{book_id}/summary")
    assert preview.status_code == 200
    assert preview.json()["groups"] == []

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})
    assert analyzed.status_code == 202
    assert analyzed.json()["status"] == "queued"
    finished = _finished(client, analysis_id)
    assert finished.json()["status"] in {"completed", "completed_needs_review"}
    assert "api.openai.com" not in finished.text
    assert [call.task for call in provider.calls] == [
        "classify",
        "extract",
        "classify",
        "extract",
        "classify",
        "extract",
        "classify",
        "extract",
    ]
    calls_after_analyze = len(provider.calls)

    still_empty = client.get(f"/api/v1/books/{book_id}")
    assert still_empty.status_code == 200
    assert still_empty.json()["lender_groups"] == []

    summary = client.get(f"/api/v1/analyses/{analysis_id}/summary")
    assert summary.status_code == 200
    groups = summary.json()["groups"]
    assert len(groups) >= 1
    headers = [group["header"] for group in groups]
    assert "Dhanrishi (Gagandeep)" in headers
    assert "SnapPaisa" in headers
    dhanrishi_lines = next(group["lines"] for group in groups if group["header"] == "Dhanrishi (Gagandeep)")
    assert len(dhanrishi_lines) == 2
    assert any("extra 3720 (28.1%)" in line["text"] for line in dhanrishi_lines)

    accepted = client.post(f"/api/v1/books/{book_id}/drafts/{analysis_id}/accept", json={})
    assert accepted.status_code == 200
    first_ids = accepted.json()["loan_internal_ids"]
    assert len(first_ids) == 3
    assert len(provider.calls) == calls_after_analyze

    saved = client.get(f"/api/v1/books/{book_id}")
    assert saved.status_code == 200
    merged = _loan_by_external(saved.json(), "LN-100")
    assert merged["image_ids"] == image_ids[:2]
    assert set(merged) >= {"fields", "calculated", "validations"}
    assert merged["fields"]["disbursed_amount"]["value"] == "13230.00"
    assert merged["fields"]["disbursed_amount"]["source"] == "extracted"
    assert merged["fields"]["repayment_amount"]["value"] == "16950.00"
    assert merged["fields"]["repayment_amount"]["source"] == "extracted"
    assert merged["calculated"]["implied_cost"]["value"] == "3720.00"
    assert merged["calculated"]["extra_percent"]["value"] == "28.1"
    assert merged["validations"]
    assert {"id", "status", "message"} <= set(merged["validations"][0])
    brands = {group["lender_brand"] for group in saved.json()["lender_groups"]}
    assert brands == {"Dhanrishi", "SnapPaisa"}

    book_summary = client.get(f"/api/v1/books/{book_id}/summary")
    assert book_summary.status_code == 200
    assert len(book_summary.json()["groups"]) >= 1
    assert book_summary.json()["totals"] == _FIRST_TOTALS

    draft_body = client.get(f"/api/v1/analyses/{analysis_id}")
    assert draft_body.status_code == 200
    assert draft_body.json()["status"] == "accepted"
    assert draft_body.json()["expires_at"] is None

    again = client.post(f"/api/v1/books/{book_id}/drafts/{analysis_id}/accept", json={})
    assert again.status_code == 200
    assert again.json()["loan_internal_ids"] == first_ids
    assert len(_loans(client.get(f"/api/v1/books/{book_id}").json())) == 3
    assert len(provider.calls) == calls_after_analyze

    tmp = Path(client.app.state.settings.loanlens_tmp_dir)
    kept_image = image_ids[0]
    kept_loan = merged["loan_internal_id"]
    assert (tmp / "books" / book_id / kept_loan / kept_image).is_file()
    assert not (tmp / analysis_id / kept_image).exists()

    second_id = _draft(client, book_id)
    _upload_fixtures(
        client,
        second_id,
        classify_dir,
        extract_dir,
        [("fibe.png", "extract_fibe.json")],
    )
    second_analyzed = client.post(f"/api/v1/analyses/{second_id}/analyze", json={})
    assert second_analyzed.status_code == 202
    assert second_analyzed.json()["status"] == "queued"
    assert _finished(client, second_id).json()["status"] in {"completed", "completed_needs_review"}
    calls_after_second = len(provider.calls)
    assert calls_after_second > calls_after_analyze

    second_accept = client.post(f"/api/v1/books/{book_id}/drafts/{second_id}/accept", json={})
    assert second_accept.status_code == 200
    added_ids = second_accept.json()["loan_internal_ids"]
    assert len(added_ids) == 1
    assert added_ids[0] not in first_ids
    assert len(provider.calls) == calls_after_second

    combined = client.get(f"/api/v1/books/{book_id}")
    assert combined.status_code == 200
    combined_ids = {loan["loan_internal_id"] for loan in _loans(combined.json())}
    assert set(first_ids) <= combined_ids
    assert added_ids[0] in combined_ids
    assert _loan_by_external(combined.json(), "LN-100")["loan_internal_id"] in first_ids
    with_fibe = client.get(f"/api/v1/books/{book_id}/summary")
    assert with_fibe.json()["totals"] == _WITH_FIBE_TOTALS

    _set_expires_at(client, analysis_id, _past())
    _set_expires_at(client, second_id, _past())
    expired_clock = client.get(f"/api/v1/books/{book_id}")
    assert expired_clock.status_code == 200
    assert {loan["loan_internal_id"] for loan in _loans(expired_clock.json())} == combined_ids
    screenshot = client.get(f"/api/v1/books/{book_id}/loans/{kept_loan}/images/{kept_image}")
    assert screenshot.status_code == 200
    assert screenshot.content.startswith(b"\x89PNG")
    assert (tmp / "books" / book_id / kept_loan / kept_image).is_file()

    deleted = client.delete(f"/api/v1/books/{book_id}/loans/{added_ids[0]}")
    assert deleted.status_code == 204
    remaining = client.get(f"/api/v1/books/{book_id}")
    assert remaining.status_code == 200
    remaining_ids = {loan["loan_internal_id"] for loan in _loans(remaining.json())}
    assert remaining_ids == set(first_ids)
    assert added_ids[0] not in remaining_ids
    assert client.get(f"/api/v1/books/{book_id}/summary").json()["totals"] == _FIRST_TOTALS
    assert (tmp / "books" / book_id / kept_loan / kept_image).is_file()
    assert not (tmp / "books" / book_id / added_ids[0]).exists()


def test_conflicting_high_amounts_stay_null_instead_of_an_average(analyze_app) -> None:
    client, provider, classify_dir, extract_dir = analyze_app
    book = client.post("/api/v1/books", json={})
    book_id = book.json()["book_id"]
    analysis_id = _draft(client, book_id)
    image_ids = _upload_fixtures(
        client,
        analysis_id,
        classify_dir,
        extract_dir,
        [
            ("conflict-a.png", "extract_conflict_a.json"),
            ("conflict-b.png", "extract_conflict_b.json"),
        ],
    )

    analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})
    assert analyzed.status_code == 202
    assert analyzed.json()["status"] == "queued"
    finished = _finished(client, analysis_id)
    assert provider.provider_name == "fake"
    assert "api.openai.com" not in finished.text
    assert [call.task for call in provider.calls] == ["classify", "extract", "classify", "extract"]

    body = finished.json()
    assert len(_loans(body)) == 1
    loan = _loans(body)[0]
    assert loan["image_ids"] == image_ids
    sanctioned = loan["fields"]["sanctioned_amount"]
    assert sanctioned["value"] is None
    assert sanctioned["source"] == "unknown"
    assert loan["fields"]["disbursed_amount"]["value"] == "10000.00"
    assert "16500.00" not in finished.text
    assert "33000.00" not in finished.text
    assert "16500" not in finished.text
