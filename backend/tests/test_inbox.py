"""GET /books/{book_id}/inbox counts and rows for that book only (SPEC §14.12)."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.ids import new_image_id
from app.main import create_app
from app.models.database import AnalysisRow, BookRow, ImageRow

_DRAFT_KEYS = {
    "analysis_id",
    "status",
    "inbox_bucket",
    "pages_done",
    "pages_total",
    "image_count",
    "created_at",
    "queued_at",
    "retryable",
    "error",
}
_HIDDEN = (
    "Priya Nair",
    "Book Borrower",
    "45000.00",
    "888888.00",
    "lon_draftsecret",
    "lon_booksecret",
    "LN-7781",
    "Extra rupees 1200",
    "You pay 99999 extra",
    "Priya-Nair-statement.png",
    "raw_extracts",
    "lender_groups",
    "summary_line",
    "borrower_name",
    "sanctioned_amount",
)
_SENSITIVE_DRAFT = json.dumps(
    {
        "raw_extracts": [{"borrower_name": "Priya Nair", "sanctioned_amount": "45000.00"}],
        "lender_groups": [
            {
                "lines": [{"text": "You pay 99999 extra"}],
                "loans": [
                    {
                        "loan_internal_id": "lon_draftsecret",
                        "external_loan_id": "LN-7781",
                        "borrower_name": "Priya Nair",
                        "sanctioned_amount": "45000.00",
                        "summary_line": "You pay 99999 extra",
                    }
                ],
            }
        ],
    }
)
_SENSITIVE_BOOK = json.dumps(
    {
        "lender_groups": [
            {
                "loans": [
                    {
                        "loan_internal_id": "lon_booksecret",
                        "borrower_name": "Book Borrower",
                        "sanctioned_amount": {"amount": "888888.00"},
                        "summary_line": "Extra rupees 1200 (8%)",
                    }
                ]
            }
        ]
    }
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "loanlens.db"
    settings = Settings(
        loanlens_env="test",
        loanlens_database_url=f"sqlite:///{db_path.as_posix()}",
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        loanlens_analysis_ttl_hours=24,
        vision_provider="fake",
        loanlens_worker_enabled=False,
        _env_file=None,
    )
    application = create_app(settings)
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        application.state.engine.dispose()


def _session(client: TestClient) -> Session:
    return client.app.state.session_factory()


def _book(client: TestClient) -> str:
    created = client.post("/api/v1/books", json={})
    assert created.status_code == 201
    return created.json()["book_id"]


def _draft(client: TestClient, book_id: str) -> str:
    created = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert created.status_code == 201
    return created.json()["analysis_id"]


def _set_draft(client: TestClient, analysis_id: str, **fields: object) -> None:
    session = _session(client)
    try:
        row = session.get(AnalysisRow, analysis_id)
        assert row is not None
        for name, value in fields.items():
            setattr(row, name, value)
        session.commit()
    finally:
        session.close()


def _add_images(client: TestClient, analysis_id: str, count: int) -> None:
    session = _session(client)
    try:
        for _ in range(count):
            session.add(
                ImageRow(
                    id=new_image_id(),
                    analysis_id=analysis_id,
                    filename="Priya-Nair-statement.png",
                    content_type="image/png",
                    byte_size=100,
                    checksum_sha256=None,
                    width=200,
                    height=200,
                    storage_path=f"{analysis_id}/page.png",
                    status="preprocessed",
                    error_code=None,
                    document_class=None,
                    created_at="2026-09-25T10:00:00Z",
                )
            )
        session.commit()
    finally:
        session.close()


def _set_book_result(client: TestClient, book_id: str, result_json: str) -> None:
    session = _session(client)
    try:
        book = session.get(BookRow, book_id)
        assert book is not None
        book.result_json = result_json
        session.commit()
    finally:
        session.close()


def test_unknown_book_is_not_found(client: TestClient) -> None:
    missing = client.get("/api/v1/books/bok_missing/inbox")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "BOOK_NOT_FOUND"
    assert missing.json()["error"]["retryable"] is False


def test_empty_inbox_has_zero_counts(client: TestClient) -> None:
    book_id = _book(client)
    body = client.get(f"/api/v1/books/{book_id}/inbox")
    assert body.status_code == 200
    payload = body.json()
    assert set(payload) == {"book_id", "counts", "drafts"}
    assert payload["book_id"] == book_id
    assert payload["counts"] == {
        "queued": 0,
        "running": 0,
        "ready": 0,
        "needs_review": 0,
        "failed": 0,
    }
    assert payload["drafts"] == []


def test_inbox_counts_sort_and_hide_other_books(client: TestClient) -> None:
    book_id = _book(client)
    other_id = _book(client)
    _set_book_result(client, book_id, _SENSITIVE_BOOK)

    needs_old = _draft(client, book_id)
    needs_new = _draft(client, book_id)
    ready = _draft(client, book_id)
    failed_retry = _draft(client, book_id)
    failed_stop = _draft(client, book_id)
    running = _draft(client, book_id)
    classifying = _draft(client, book_id)
    queued_new = _draft(client, book_id)
    queued_old = _draft(client, book_id)
    accepted = _draft(client, book_id)
    expired = _draft(client, book_id)
    created = _draft(client, book_id)
    pending = _draft(client, book_id)
    other = _draft(client, other_id)

    _set_draft(
        client,
        needs_old,
        status="completed_needs_review",
        created_at="2026-09-25T08:00:00Z",
        pages_done=2,
        pages_total=2,
        result_json=_SENSITIVE_DRAFT,
    )
    _set_draft(
        client,
        needs_new,
        status="completed_needs_review",
        created_at="2026-09-25T08:30:00Z",
        pages_done=1,
        pages_total=1,
    )
    _set_draft(
        client,
        ready,
        status="completed",
        created_at="2026-09-25T07:00:00Z",
        queued_at="2026-09-25T07:05:00Z",
        pages_done=4,
        pages_total=4,
        result_json=_SENSITIVE_DRAFT,
    )
    _set_draft(
        client,
        failed_retry,
        status="failed",
        created_at="2026-09-25T06:00:00Z",
        error_code="VISION_TIMEOUT",
        error_message="The vision service timed out.",
        pages_done=1,
        pages_total=4,
    )
    _set_draft(
        client,
        failed_stop,
        status="failed",
        created_at="2026-09-25T06:30:00Z",
        error_code="NO_LOAN_DOCUMENTS",
        error_message="No loan documents were found.",
        pages_done=4,
        pages_total=4,
    )
    _set_draft(
        client,
        running,
        status="running",
        created_at="2026-09-25T11:00:00Z",
        queued_at="2026-09-25T10:50:00Z",
        pages_done=0,
        pages_total=8,
    )
    _set_draft(
        client,
        classifying,
        status="classifying",
        created_at="2026-09-25T10:00:00Z",
        queued_at="2026-09-25T09:50:00Z",
        pages_done=3,
        pages_total=8,
    )
    _set_draft(
        client,
        queued_new,
        status="queued",
        created_at="2026-09-25T12:00:00Z",
        queued_at="2026-09-25T12:01:00Z",
        pages_done=0,
        pages_total=1,
    )
    _set_draft(
        client,
        queued_old,
        status="queued",
        created_at="2026-09-25T09:00:00Z",
        queued_at="2026-09-25T09:01:00Z",
        pages_done=0,
        pages_total=4,
    )
    _set_draft(client, accepted, status="accepted", result_json=_SENSITIVE_DRAFT, expires_at=None)
    _set_draft(client, expired, status="expired", expires_at="2020-01-01T00:00:00Z")
    _set_draft(client, pending, status="images_pending")
    _set_draft(
        client,
        other,
        status="completed",
        created_at="2026-09-25T01:00:00Z",
        result_json=_SENSITIVE_DRAFT,
    )
    _add_images(client, queued_old, 4)
    _add_images(client, classifying, 8)

    inbox = client.get(f"/api/v1/books/{book_id}/inbox")
    assert inbox.status_code == 200
    payload = inbox.json()
    assert payload["counts"] == {
        "queued": 2,
        "running": 2,
        "ready": 1,
        "needs_review": 2,
        "failed": 2,
    }
    drafts = payload["drafts"]
    assert [row["analysis_id"] for row in drafts] == [
        needs_old,
        needs_new,
        ready,
        failed_retry,
        failed_stop,
        classifying,
        running,
        queued_old,
        queued_new,
    ]
    assert [row["inbox_bucket"] for row in drafts] == [
        "needs_review",
        "needs_review",
        "ready",
        "failed",
        "failed",
        "running",
        "running",
        "queued",
        "queued",
    ]
    by_id = {row["analysis_id"]: row for row in drafts}
    assert by_id[classifying]["status"] == "classifying"
    assert by_id[classifying]["pages_done"] == 3
    assert by_id[classifying]["pages_total"] == 8
    assert by_id[classifying]["image_count"] == 8
    assert by_id[queued_old]["image_count"] == 4
    assert by_id[queued_old]["pages_total"] == 4
    assert by_id[ready]["queued_at"] == "2026-09-25T07:05:00Z"
    assert by_id[needs_old]["queued_at"] is None
    assert by_id[failed_retry]["retryable"] is True
    assert by_id[failed_retry]["error"] == {
        "code": "VISION_TIMEOUT",
        "message": "The vision service timed out.",
    }
    assert by_id[failed_stop]["retryable"] is False
    assert by_id[failed_stop]["error"] == {
        "code": "NO_LOAN_DOCUMENTS",
        "message": "No loan documents were found.",
    }
    assert by_id[ready]["error"] is None
    assert by_id[ready]["retryable"] is False
    hidden_ids = {accepted, expired, created, pending, other}
    assert hidden_ids.isdisjoint(by_id)
    for row in drafts:
        assert set(row) == _DRAFT_KEYS
        if row["error"] is not None:
            assert set(row["error"]) == {"code", "message"}

    rendered = json.dumps(payload)
    for secret in _HIDDEN:
        assert secret not in rendered

    other_inbox = client.get(f"/api/v1/books/{other_id}/inbox")
    assert other_inbox.status_code == 200
    assert other_inbox.json()["counts"]["ready"] == 1
    assert [row["analysis_id"] for row in other_inbox.json()["drafts"]] == [other]
    assert book_id not in json.dumps(other_inbox.json()["drafts"])


def test_running_bucket_sorts_pipeline_statuses_by_created_at(client: TestClient) -> None:
    book_id = _book(client)
    ordered = [
        ("validating", "2026-09-25T10:00:00Z"),
        ("preprocessing", "2026-09-25T10:01:00Z"),
        ("extracting", "2026-09-25T10:02:00Z"),
        ("grouping", "2026-09-25T10:03:00Z"),
        ("running", "2026-09-25T10:04:00Z"),
        ("classifying", "2026-09-25T10:05:00Z"),
    ]
    ids: list[str] = []
    for status, created_at in ordered:
        analysis_id = _draft(client, book_id)
        _set_draft(client, analysis_id, status=status, created_at=created_at, pages_total=2)
        ids.append(analysis_id)

    payload = client.get(f"/api/v1/books/{book_id}/inbox").json()
    assert payload["counts"]["running"] == 6
    assert payload["counts"]["queued"] == 0
    rows = payload["drafts"]
    assert [row["analysis_id"] for row in rows] == ids
    assert [row["status"] for row in rows] == [status for status, _ in ordered]
    assert {row["inbox_bucket"] for row in rows} == {"running"}


def test_overdue_draft_is_absent_after_sweep(client: TestClient) -> None:
    book_id = _book(client)
    overdue = _draft(client, book_id)
    kept = _draft(client, book_id)
    _set_draft(
        client,
        overdue,
        status="completed",
        created_at="2026-09-25T08:00:00Z",
        expires_at="2020-01-01T00:00:00Z",
        result_json=_SENSITIVE_DRAFT,
    )
    _set_draft(client, kept, status="queued", created_at="2026-09-25T09:00:00Z", pages_total=1)

    payload = client.get(f"/api/v1/books/{book_id}/inbox").json()
    assert [row["analysis_id"] for row in payload["drafts"]] == [kept]
    assert payload["counts"]["ready"] == 0
    assert payload["counts"]["queued"] == 1
    assert "45000.00" not in json.dumps(payload)
