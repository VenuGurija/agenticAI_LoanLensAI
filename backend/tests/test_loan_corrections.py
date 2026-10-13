"""Preview corrections, grouping, and the saved-loan screenshot (SPEC §10.6, §12.3, §14.10)."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from app.models.database import AnalysisRow, BookRow
from app.schemas.extraction import ImageExtractResult
from app.services.extraction import lender_groups_from_extracts
from tests.test_image_upload import tiny_png


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "loanlens.db"
    settings = Settings(
        loanlens_database_url=f"sqlite:///{db_path.as_posix()}",
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        loanlens_analysis_ttl_hours=24,
        _env_file=None,
    )
    application = create_app(settings)
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        application.state.engine.dispose()


def test_preview_correction_sets_user_corrected_and_returns_extra(client: TestClient) -> None:
    book_id, analysis_id, loan_id = _preview_with_loan(client)

    patched = client.patch(
        f"/api/v1/analyses/{analysis_id}/loans/{loan_id}",
        json={"disbursed_amount": "13230.00"},
    )

    assert patched.status_code == 200
    loan = patched.json()
    assert loan["fields"]["disbursed_amount"]["value"] == "13230.00"
    assert loan["fields"]["disbursed_amount"]["source"] == "user_corrected"
    assert loan["fields"]["sanctioned_amount"]["value"] == "15000.00"
    assert loan["fields"]["sanctioned_amount"]["source"] == "extracted"
    assert loan["fields"]["gst_amount"]["value"] is None
    assert loan["calculated"]["implied_cost"]["value"] == "3720.00"
    assert loan["calculated"]["extra_percent"]["value"] == "28.1"
    assert "0" != loan["fields"]["gst_amount"]["value"]

    session: Session = client.app.state.session_factory()
    try:
        stored = json.loads(session.get(AnalysisRow, analysis_id).result_json)
        book = session.get(BookRow, book_id)
    finally:
        session.close()
    saved = stored["lender_groups"][0]["loans"][0]
    assert saved["fields"]["disbursed_amount"]["source"] == "user_corrected"
    assert saved["fields"]["gst_amount"]["value"] is None
    assert book is not None
    assert json.loads(book.result_json)["correction_hints"] == [
        {
            "lender_key": "dhanrishi",
            "field_name": "disbursed_amount",
            "previous_value": None,
            "corrected_value": "13230.00",
        }
    ]


def test_grouping_moves_each_screenshot_once(client: TestClient) -> None:
    _book_id, analysis_id, loan_ids, image_ids = _preview_with_two_loans(client)

    moved = client.post(
        f"/api/v1/analyses/{analysis_id}/grouping",
        json={
            "loans": [
                {"loan_internal_id": loan_ids[0], "image_ids": image_ids},
                {"loan_internal_id": loan_ids[1], "image_ids": []},
            ]
        },
    )

    assert moved.status_code == 200
    loans = [loan for group in moved.json()["lender_groups"] for loan in group["loans"]]
    assert any(loan["image_ids"] == image_ids or set(loan["image_ids"]) == set(image_ids) for loan in loans)

    missing = client.post(
        f"/api/v1/analyses/{analysis_id}/grouping",
        json={"loans": [{"loan_internal_id": loan_ids[0], "image_ids": [image_ids[0]]}]},
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "INVALID_STATUS"


def test_screenshot_stays_hidden_from_the_route_until_the_book_owns_the_loan(client: TestClient) -> None:
    book_id, analysis_id = _draft(client)
    uploaded = client.post(
        f"/api/v1/analyses/{analysis_id}/images",
        files=[("files", ("page.png", tiny_png(), "image/png"))],
    )
    assert uploaded.status_code == 201
    image_id = uploaded.json()["images"][0]["image_id"]

    hidden = client.get(f"/api/v1/books/{book_id}/loans/lon_saved/images/{image_id}")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "LOAN_NOT_FOUND"

    session: Session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        book.result_json = json.dumps(
            {"lender_groups": [{"loans": [{"loan_internal_id": "lon_saved", "image_ids": [image_id]}]}]}
        )
        session.commit()
    finally:
        session.close()

    shown = client.get(f"/api/v1/books/{book_id}/loans/lon_saved/images/{image_id}")
    assert shown.status_code == 200
    assert shown.headers["content-type"].startswith("image/png")
    assert shown.content.startswith(b"\x89PNG")

    other_loan = client.get(f"/api/v1/books/{book_id}/loans/lon_other/images/{image_id}")
    assert other_loan.status_code == 404
    assert other_loan.json()["error"]["code"] == "LOAN_NOT_FOUND"


def _preview_with_loan(client: TestClient) -> tuple[str, str, str]:
    book_id, analysis_id = _draft(client)
    page = _page(
        "img_page",
        sanctioned_amount=_money("15000.00"),
        disbursed_amount=_money(None, "unknown"),
        repayment_amount=_money("16950.00"),
    )
    groups = lender_groups_from_extracts([page])
    _store_preview(client, analysis_id, [page], groups)
    return book_id, analysis_id, groups[0]["loans"][0]["loan_internal_id"]


def _preview_with_two_loans(client: TestClient) -> tuple[str, str, list[str], list[str]]:
    _book_id, analysis_id = _draft(client)
    pages = [
        _page("img_a", loan_id=_text("LN-1"), sanctioned_amount=_money("15000.00")),
        _page("img_b", loan_id=_text("LN-2"), sanctioned_amount=_money("20000.00")),
    ]
    groups = lender_groups_from_extracts(pages)
    _store_preview(client, analysis_id, pages, groups)
    loan_ids = [loan["loan_internal_id"] for group in groups for loan in group["loans"]]
    image_ids = ["img_a", "img_b"]
    return _book_id, analysis_id, loan_ids, image_ids


def _store_preview(client: TestClient, analysis_id: str, pages: list[dict], groups: list[dict]) -> None:
    session: Session = client.app.state.session_factory()
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        row.status = "completed_needs_review"
        row.result_json = json.dumps({"raw_extracts": pages, "lender_groups": groups})
        session.commit()
    finally:
        session.close()


def _draft(client: TestClient) -> tuple[str, str]:
    book = client.post("/api/v1/books", json={})
    assert book.status_code == 201
    book_id = book.json()["book_id"]
    draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert draft.status_code == 201
    return book_id, draft.json()["analysis_id"]


def _money(value: str | None, confidence: str = "high") -> dict:
    return {"value": value, "currency": None if value is None else "INR", "confidence": confidence}


def _text(value: str | None, confidence: str = "high") -> dict:
    return {"value": value, "confidence": confidence}


def _page(image_id: str, **fields: object) -> dict:
    loan = {"lender_brand": _text("Dhanrishi"), "loan_id": _text("LN-1")}
    loan.update(fields)
    return ImageExtractResult.model_validate({"image_id": image_id, "loans": [loan]}).model_dump(mode="json")
