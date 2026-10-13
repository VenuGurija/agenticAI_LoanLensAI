from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from app.models.database import BookRow
from app.models.enums import AnalysisStatus, ImageStatus

_ANALYSIS_STATUSES = {
    "created",
    "images_pending",
    "queued",
    "running",
    "preprocessing",
    "classifying",
    "extracting",
    "grouping",
    "validating",
    "completed",
    "completed_needs_review",
    "accepted",
    "failed",
    "expired",
}
_IMAGE_STATUSES = {"uploaded", "rejected", "preprocessed", "classified", "failed"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_path = tmp_path / "loanlens.db"
    settings = Settings(
        loanlens_database_url=f"sqlite:///{db_path.as_posix()}",
        loanlens_analysis_ttl_hours=24,
        _env_file=None,
    )
    application = create_app(settings)
    try:
        with TestClient(application) as test_client:
            yield test_client
    finally:
        application.state.engine.dispose()


def _error_code(response) -> str:
    return response.json()["error"]["code"]


def test_create_book_and_draft_then_delete_draft(client: TestClient) -> None:
    created = client.post("/api/v1/books", json={})
    assert created.status_code == 201
    book = created.json()
    assert set(book) == {"book_id", "created_at"}
    assert book["book_id"].startswith("bok_")
    assert book["created_at"].endswith("Z")

    draft = client.post(f"/api/v1/books/{book['book_id']}/drafts", json={})
    assert draft.status_code == 201
    draft_body = draft.json()
    assert set(draft_body) == {"book_id", "analysis_id", "status", "created_at", "expires_at"}
    assert draft_body["book_id"] == book["book_id"]
    assert draft_body["analysis_id"].startswith("anl_")
    assert draft_body["status"] == "created"
    created_at = datetime.strptime(draft_body["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    expires_at = datetime.strptime(draft_body["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    assert expires_at - created_at == timedelta(hours=24)

    got_book = client.get(f"/api/v1/books/{book['book_id']}")
    assert got_book.status_code == 200
    assert got_book.json()["book_id"] == book["book_id"]
    assert got_book.json()["created_at"] == book["created_at"]
    assert got_book.json()["lender_groups"] == []
    assert "expires_at" not in got_book.json()

    analysis_id = draft_body["analysis_id"]
    got_draft = client.get(f"/api/v1/analyses/{analysis_id}")
    assert got_draft.status_code == 200
    assert got_draft.json()["analysis_id"] == analysis_id
    assert got_draft.json()["status"] == "created"
    assert got_draft.json()["expires_at"] == draft_body["expires_at"]
    assert got_draft.json()["completed_at"] is None
    assert got_draft.json()["error"] is None
    assert got_draft.json()["images"] == []
    assert got_draft.json()["lender_groups"] == []

    deleted = client.delete(f"/api/v1/analyses/{analysis_id}")
    assert deleted.status_code == 204
    assert deleted.content == b""

    missing = client.get(f"/api/v1/analyses/{analysis_id}")
    assert missing.status_code == 404
    assert _error_code(missing) == "ANALYSIS_NOT_FOUND"
    assert missing.json()["error"]["retryable"] is False

    still_there = client.get(f"/api/v1/books/{book['book_id']}")
    assert still_there.status_code == 200
    assert still_there.json()["book_id"] == book["book_id"]

    session: Session = client.app.state.session_factory()
    try:
        stored = session.get(BookRow, book["book_id"])
        assert stored is not None
        assert stored.result_json is None
    finally:
        session.close()


def test_unknown_book_and_draft_are_not_found(client: TestClient) -> None:
    unknown_book = client.get("/api/v1/books/bok_MISSING")
    assert unknown_book.status_code == 404
    assert _error_code(unknown_book) == "BOOK_NOT_FOUND"

    draft_on_missing_book = client.post("/api/v1/books/bok_MISSING/drafts", json={})
    assert draft_on_missing_book.status_code == 404
    assert _error_code(draft_on_missing_book) == "BOOK_NOT_FOUND"

    unknown_draft = client.get("/api/v1/analyses/anl_MISSING")
    assert unknown_draft.status_code == 404
    assert _error_code(unknown_draft) == "ANALYSIS_NOT_FOUND"

    delete_missing = client.delete("/api/v1/analyses/anl_MISSING")
    assert delete_missing.status_code == 404
    assert _error_code(delete_missing) == "ANALYSIS_NOT_FOUND"


def test_no_list_all_books_or_analyses_route(client: TestClient) -> None:
    books = client.get("/api/v1/books")
    analyses = client.get("/api/v1/analyses")
    assert books.status_code == 405
    assert analyses.status_code == 404
    assert books.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert analyses.json()["error"]["code"] == "NOT_FOUND"
    assert isinstance(books.json(), dict)
    assert isinstance(analyses.json(), dict)

    methods_by_path: dict[str, set[str]] = {}
    for route in client.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            methods_by_path.setdefault(path, set()).update(methods)

    assert "GET" not in methods_by_path.get("/api/v1/books", set())
    assert "GET" not in methods_by_path.get("/api/v1/analyses", set())
    assert "POST" not in methods_by_path.get("/api/v1/analyses", set())


def test_sqlite_tables_match_spec(client: TestClient) -> None:
    columns = {
        name: {column["name"] for column in inspect(client.app.state.engine).get_columns(name)}
        for name in ("books", "analyses", "images")
    }
    assert columns["books"] == {"id", "created_at", "result_json"}
    assert "expires_at" not in columns["books"]
    assert columns["analyses"] == {
        "id",
        "book_id",
        "status",
        "created_at",
        "completed_at",
        "expires_at",
        "error_code",
        "error_message",
        "queued_at",
        "pages_done",
        "pages_total",
        "result_json",
    }
    assert columns["images"] == {
        "id",
        "analysis_id",
        "filename",
        "content_type",
        "byte_size",
        "checksum_sha256",
        "width",
        "height",
        "storage_path",
        "status",
        "error_code",
        "document_class",
        "created_at",
    }


def test_status_enums_include_accepted_and_image_states() -> None:
    assert {status.value for status in AnalysisStatus} == _ANALYSIS_STATUSES
    assert AnalysisStatus.ACCEPTED.value == "accepted"
    assert AnalysisStatus.CREATED.value == "created"
    assert {status.value for status in ImageStatus} == _IMAGE_STATUSES
