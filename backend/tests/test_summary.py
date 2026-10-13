"""Summary lines and summary APIs (SPEC §4.2, §14.7, §14.10).

The lender names below are unit examples of the display contract. They are not
a requirement to special-case those brands.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from app.models.database import AnalysisRow, BookRow
from app.services.summary import book_totals, group_header, summary_line

_TODAY = date(2026, 9, 24)
_DHANRISHI_LINE = "15k, got 13230 on jun 17, pay 16950 on jun 30, extra 3720 (28.1%)"
_SNAPPAISA_LINE = "20k, got 17640, pay 22800 on jul 1, extra 5160 (29.3%)"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        loanlens_database_url=f"sqlite:///{(tmp_path / 'loanlens.db').as_posix()}",
        loanlens_analysis_ttl_hours=24,
        _env_file=None,
    )
    application = create_app(settings)
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        application.state.engine.dispose()


def _money(value: str | None, currency: str | None = "INR", source: str = "extracted") -> dict:
    if value is None:
        return {"value": None, "currency": None, "source": "unknown"}
    return {"value": value, "currency": currency, "source": source}


def _when(value: str | None) -> dict:
    if value is None:
        return {"value": None, "source": "unknown"}
    return {"value": value, "source": "extracted"}


def _loan(
    loan_id: str,
    *,
    sanctioned: str | None = None,
    disbursed: str | None = None,
    repayment: str | None = None,
    disbursed_on: str | None = None,
    repayment_on: str | None = None,
    currency: str | None = "INR",
    source: str = "extracted",
    needs_review: bool = False,
) -> dict:
    return {
        "loan_internal_id": loan_id,
        "needs_review": needs_review,
        "fields": {
            "sanctioned_amount": _money(sanctioned, currency, source),
            "disbursed_amount": _money(disbursed, currency, source),
            "repayment_amount": _money(repayment, currency, source),
            "disbursement_date": _when(disbursed_on),
            "repayment_date": _when(repayment_on),
        },
    }


def _dhanrishi_loan() -> dict:
    return _loan(
        "lon_dhanrishi",
        sanctioned="15000.00",
        disbursed="13230.00",
        repayment="16950.00",
        disbursed_on="2026-06-17",
        repayment_on="2026-06-30",
    )


def _snappaisa_loan() -> dict:
    return _loan(
        "lon_snappaisa",
        sanctioned="20000.00",
        disbursed="17640.00",
        repayment="22800.00",
        repayment_on="2026-07-01",
    )


def _groups(*items: tuple[str | None, str | None, list[dict]]) -> list[dict]:
    groups = []
    for brand, legal, loans in items:
        groups.append(
            {
                "lender_brand": brand,
                "legal_lending_entity": legal,
                "loans": loans,
            }
        )
    return groups


def test_illustrative_lines_include_extra_when_both_amounts_exist() -> None:
    assert summary_line(_dhanrishi_loan(), today=_TODAY) == _DHANRISHI_LINE
    assert summary_line(_snappaisa_loan(), today=_TODAY) == _SNAPPAISA_LINE
    assert "null" not in _DHANRISHI_LINE
    assert "null" not in _SNAPPAISA_LINE


def test_missing_disbursed_omits_got_and_extra() -> None:
    loan = _loan(
        "lon_missing_got",
        sanctioned="15000.00",
        repayment="16950.00",
        repayment_on="2026-06-30",
    )

    text = summary_line(loan, today=_TODAY)

    assert text == "15k, pay 16950 on jun 30"
    assert "got" not in text
    assert "extra" not in text
    assert "null" not in text


def test_unknown_sanctioned_starts_with_amount_unknown() -> None:
    loan = _loan(
        "lon_unknown_sanctioned",
        disbursed="13230.00",
        repayment="16950.00",
        disbursed_on="2026-06-17",
        repayment_on="2026-06-30",
    )

    assert summary_line(loan, today=_TODAY) == (
        "amount unknown, got 13230 on jun 17, pay 16950 on jun 30, extra 3720 (28.1%)"
    )


def test_unsummarizable_loan_is_needs_review() -> None:
    empty = _loan("lon_empty")
    dates_only = _loan(
        "lon_dates",
        disbursed_on="2026-06-17",
        repayment_on="2026-06-30",
    )

    assert summary_line(empty, today=_TODAY) == "Needs review"
    assert summary_line(dates_only, today=_TODAY) == "Needs review"
    assert "null" not in summary_line(empty, today=_TODAY)


def test_header_includes_legal_name_when_known() -> None:
    assert group_header("Dhanrishi", "Gagandeep") == "Dhanrishi (Gagandeep)"
    assert group_header("snappaisa", "empire finance") == "snappaisa (empire finance)"
    assert group_header("snappaisa", None) == "snappaisa"
    assert group_header(None, None) == ""


def test_zero_disbursed_omits_the_extra_clause() -> None:
    loan = _loan(
        "lon_zero",
        sanctioned="15000.00",
        disbursed="0.00",
        repayment="16950.00",
    )

    text = summary_line(loan, today=_TODAY)

    assert text == "15k, got 0, pay 16950"
    assert "extra" not in text


def test_user_corrected_amounts_use_the_same_line() -> None:
    loan = _dhanrishi_loan()
    for name in ("sanctioned_amount", "disbursed_amount", "repayment_amount"):
        loan["fields"][name]["source"] = "user_corrected"

    assert summary_line(loan, today=_TODAY) == _DHANRISHI_LINE


def test_book_totals_sum_only_known_inr_amounts() -> None:
    complete = [_dhanrishi_loan(), _snappaisa_loan()]
    assert book_totals(complete) == {
        "received": "30870.00",
        "still_to_pay": "39750.00",
        "extra": "8880.00",
        "incomplete": False,
    }

    missing_disbursed = _loan(
        "lon_partial",
        sanctioned="5000.00",
        repayment="1000.00",
    )
    partial = book_totals([*complete, missing_disbursed])
    assert partial["received"] == "30870.00"
    assert partial["still_to_pay"] == "40750.00"
    assert partial["extra"] == "8880.00"
    assert partial["incomplete"] is True

    other_currency = _loan(
        "lon_usd",
        sanctioned="15000.00",
        disbursed="13230.00",
        repayment="16950.00",
        currency="USD",
    )
    mixed = book_totals([*complete, other_currency])
    assert mixed["received"] == "30870.00"
    assert mixed["still_to_pay"] == "39750.00"
    assert mixed["extra"] == "8880.00"
    assert mixed["incomplete"] is True

    assert book_totals([]) == {
        "received": "0.00",
        "still_to_pay": "0.00",
        "extra": "0.00",
        "incomplete": False,
    }


def test_summary_endpoints_return_grouped_lines_and_book_totals(client: TestClient) -> None:
    created = client.post("/api/v1/books", json={})
    assert created.status_code == 201
    book_id = created.json()["book_id"]
    draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert draft.status_code == 201
    analysis_id = draft.json()["analysis_id"]

    groups = _groups(
        ("Dhanrishi", "Gagandeep", [_dhanrishi_loan()]),
        ("snappaisa", "empire finance", [_snappaisa_loan()]),
        ("", None, [_loan("lon_review")]),
    )
    _store_result(client, book_id, analysis_id, groups)

    analysis = client.get(f"/api/v1/analyses/{analysis_id}/summary")
    assert analysis.status_code == 200
    analysis_body = analysis.json()
    assert set(analysis_body) == {"analysis_id", "status", "groups"}
    assert analysis_body["analysis_id"] == analysis_id
    assert analysis_body["status"] == "created"
    assert [group["header"] for group in analysis_body["groups"]] == [
        "Dhanrishi (Gagandeep)",
        "snappaisa (empire finance)",
        "",
    ]
    assert analysis_body["groups"][0]["lines"] == [
        {
            "loan_internal_id": "lon_dhanrishi",
            "text": summary_line(_dhanrishi_loan()),
            "needs_review": False,
        }
    ]
    assert analysis_body["groups"][1]["lines"][0]["text"] == summary_line(_snappaisa_loan())
    assert analysis_body["groups"][2]["lines"][0]["text"] == "Needs review"

    book = client.get(f"/api/v1/books/{book_id}/summary")
    assert book.status_code == 200
    book_body = book.json()
    assert set(book_body) == {"book_id", "totals", "groups"}
    assert book_body["book_id"] == book_id
    assert book_body["totals"] == {
        "received": "30870.00",
        "still_to_pay": "39750.00",
        "extra": "8880.00",
        "incomplete": True,
    }
    assert book_body["groups"][0]["lines"][0]["text"] == summary_line(_dhanrishi_loan())
    assert "3720 (28.1%)" in book_body["groups"][0]["lines"][0]["text"]


def test_summary_missing_ids_use_the_not_found_envelope(client: TestClient) -> None:
    missing_book = client.get("/api/v1/books/bok_MISSING/summary")
    assert missing_book.status_code == 404
    assert missing_book.json()["error"]["code"] == "BOOK_NOT_FOUND"

    missing_analysis = client.get("/api/v1/analyses/anl_MISSING/summary")
    assert missing_analysis.status_code == 404
    assert missing_analysis.json()["error"]["code"] == "ANALYSIS_NOT_FOUND"


def _store_result(
    client: TestClient,
    book_id: str,
    analysis_id: str,
    groups: list[dict],
) -> None:
    payload = json.dumps({"lender_groups": groups})
    session: Session = client.app.state.session_factory()
    try:
        book = session.get(BookRow, book_id)
        analysis = session.get(AnalysisRow, analysis_id)
        assert book is not None and analysis is not None
        book.result_json = payload
        analysis.result_json = payload
        session.commit()
    finally:
        session.close()
