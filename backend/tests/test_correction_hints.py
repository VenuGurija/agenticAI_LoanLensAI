"""Same-lender correction hints (SPEC §12.4, §14.11).

A saved correction is stored on the book and included only in the extract
prompt for that lender. Other lenders do not receive it. Nothing is trained.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from app.models.database import AnalysisRow, BookRow
from app.persistence.analyses import read_correction_hints
from app.schemas.extraction import ImageExtractResult
from app.services.extraction import lender_groups_from_extracts
from app.services.vision.prompts import prompt_for

_DHANRISHI = {
    "lender_key": "dhanrishi",
    "field_name": "disbursed_amount",
    "previous_value": "15137.00",
    "corrected_value": "15130.00",
}
_SNAPPAISA = {
    "lender_key": "snappaisa",
    "field_name": "repayment_amount",
    "previous_value": "1000.00",
    "corrected_value": "88001.00",
}


def test_same_lender_prompt_contains_the_corrected_value_only() -> None:
    hints = [_DHANRISHI, _SNAPPAISA]
    same = prompt_for("extract", hints=hints, lender_key="dhanrishi")
    other = prompt_for("extract", hints=hints, lender_key="snappaisa")
    unknown = prompt_for("extract", hints=hints, lender_key=None)
    still_unknown = prompt_for("extract", hints=hints, lender_key="_unknown_lender")

    assert "15130.00" in same
    assert "15137.00" in same
    assert "88001.00" not in same
    assert "snappaisa" not in same
    assert "88001.00" in other
    assert "15130.00" not in other
    assert "15137.00" not in other
    assert "dhanrishi" not in other
    assert "15130.00" not in unknown
    assert "88001.00" not in unknown
    assert still_unknown == prompt_for("extract")
    assert prompt_for("classify", hints=hints, lender_key="dhanrishi") == prompt_for("classify")

    lowered = same.lower()
    assert "do not run tools" in lowered
    assert "calculate" not in lowered
    assert "fine-tune" not in lowered
    assert "embedding" not in lowered


def test_patch_stores_user_corrected_and_a_book_hint(client: TestClient) -> None:
    book_id, analysis_id, loan_id = _preview(client)

    patched = client.patch(
        f"/api/v1/books/{book_id}/loans/{loan_id}",
        json={"disbursed_amount": "15130.00"},
    )

    assert patched.status_code == 200
    loan = patched.json()
    assert loan["fields"]["disbursed_amount"]["source"] == "user_corrected"
    assert loan["fields"]["disbursed_amount"]["value"] == "15130.00"
    assert loan["fields"]["sanctioned_amount"]["source"] == "extracted"

    session: Session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        stored = json.loads(book.result_json)
        hints = read_correction_hints(book.result_json)
    finally:
        session.close()

    assert hints == [_DHANRISHI]
    assert stored["lender_groups"][0]["loans"][0]["loan_internal_id"] == loan_id
    assert stored["correction_hints"] == [_DHANRISHI]

    same = prompt_for("extract", hints=hints, lender_key="dhanrishi")
    other = prompt_for("extract", hints=hints, lender_key="snappaisa")
    assert "15130.00" in same
    assert "15130.00" not in other

    book_body = client.get(f"/api/v1/books/{book_id}")
    summary = client.get(f"/api/v1/books/{book_id}/summary")
    assert book_body.status_code == 200
    assert summary.status_code == 200
    assert "correction_hints" not in book_body.text
    assert "correction_hints" not in summary.text
    assert "15137.00" not in book_body.text
    assert "15137.00" not in summary.text

    again = client.patch(
        f"/api/v1/books/{book_id}/loans/{loan_id}",
        json={"repayment_amount": "17000.00"},
    )
    assert again.status_code == 200
    session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        hints = read_correction_hints(book.result_json)
        stored = json.loads(book.result_json)
    finally:
        session.close()
    assert hints == [
        _DHANRISHI,
        {
            "lender_key": "dhanrishi",
            "field_name": "repayment_amount",
            "previous_value": "16950.00",
            "corrected_value": "17000.00",
        },
    ]
    assert stored["lender_groups"][0]["loans"][0]["fields"]["disbursed_amount"]["value"] == "15130.00"


def test_preview_hint_stays_on_the_book(client: TestClient) -> None:
    book_id, analysis_id, loan_id = _preview(client, copy_to_book=False)

    patched = client.patch(
        f"/api/v1/analyses/{analysis_id}/loans/{loan_id}",
        json={"disbursed_amount": "15130.00"},
    )

    assert patched.status_code == 200
    assert patched.json()["fields"]["disbursed_amount"]["source"] == "user_corrected"
    session: Session = client.app.state.session_factory()
    try:
        draft = session.get(AnalysisRow, analysis_id)
        book = session.get(BookRow, book_id)
        assert draft is not None and book is not None
        draft_document = json.loads(draft.result_json)
        hints = read_correction_hints(book.result_json)
    finally:
        session.close()
    assert "correction_hints" not in draft_document
    assert hints == [_DHANRISHI]
    shown = client.get(f"/api/v1/analyses/{analysis_id}")
    assert shown.status_code == 200
    assert "correction_hints" not in shown.text


def _preview(client: TestClient, *, copy_to_book: bool = True) -> tuple[str, str, str]:
    book = client.post("/api/v1/books", json={})
    assert book.status_code == 201
    book_id = book.json()["book_id"]
    draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert draft.status_code == 201
    analysis_id = draft.json()["analysis_id"]
    page = _page(
        "img_page",
        sanctioned_amount=_money("15000.00"),
        disbursed_amount=_money("15137.00"),
        repayment_amount=_money("16950.00"),
    )
    groups = lender_groups_from_extracts([page])
    session: Session = client.app.state.session_factory()
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        row.status = "completed"
        row.result_json = json.dumps({"raw_extracts": [page], "lender_groups": groups})
        if copy_to_book:
            stored_book = session.get(BookRow, book_id)
            assert stored_book is not None
            stored_book.result_json = row.result_json
        session.commit()
    finally:
        session.close()
    return book_id, analysis_id, groups[0]["loans"][0]["loan_internal_id"]


def _money(value: str) -> dict[str, str]:
    return {"value": value, "currency": "INR", "confidence": "high"}


def _page(image_id: str, **fields: object) -> dict:
    loan = {
        "lender_brand": {"value": "Dhanrishi", "confidence": "high"},
        "loan_id": {"value": "LN-1", "confidence": "high"},
    }
    loan.update(fields)
    return ImageExtractResult.model_validate({"image_id": image_id, "loans": [loan]}).model_dump(mode="json")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
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
