"""Retention, purge, and secret redaction (SPEC §15.3, §16)."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.logging import LOGGER_NAME, StructuredFormatter, configure_logging, log_event
from app.main import create_app
from app.models.database import AnalysisRow, BookRow
from app.models.enums import AnalysisStatus
from app.persistence.analyses import add_uploaded_image, read_correction_hints, utc_timestamp
from app.services.retention import purge_draft_directory, purge_loan_directory, sweep_expired_drafts

_SECRET = "sk-test-loanlens-vision-key-13-1"
_HINT = {
    "lender_key": "dhanrishi",
    "field_name": "disbursed_amount",
    "previous_value": "1.00",
    "corrected_value": "2.00",
}


def _application(tmp_path: Path, **overrides: object):
    settings = Settings(
        loanlens_database_url=f"sqlite:///{(tmp_path / 'loanlens.db').as_posix()}",
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        _env_file=None,
        **overrides,
    )
    return create_app(settings)


def _past() -> str:
    return utc_timestamp(datetime.now(timezone.utc) - timedelta(hours=25))


def _seed_book_with_expired_draft(client: TestClient) -> dict[str, object]:
    """One accepted loan file, one unaccepted draft past TTL, and one fresh draft."""
    book = client.post("/api/v1/books", json={})
    assert book.status_code == 201
    book_id = book.json()["book_id"]
    kept = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    stale = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    fresh = client.post(f"/api/v1/books/{book_id}/drafts", json={})
    assert kept.status_code == 201
    assert stale.status_code == 201
    assert fresh.status_code == 201
    kept_id = kept.json()["analysis_id"]
    stale_id = stale.json()["analysis_id"]
    fresh_id = fresh.json()["analysis_id"]

    tmp = Path(client.app.state.settings.loanlens_tmp_dir)
    loan_id = "lon_kept"
    image_id = "img_kept"
    loan_file = tmp / "books" / book_id / loan_id / image_id
    loan_file.parent.mkdir(parents=True)
    loan_file.write_bytes(b"kept-screenshot")

    stale_dir = tmp / stale_id
    stale_dir.mkdir(parents=True)
    (stale_dir / "img_stale").write_bytes(b"stale-screenshot")
    (stale_dir / "sidecar").write_bytes(b"extra")

    session = client.app.state.session_factory()
    try:
        kept_row = session.get(AnalysisRow, kept_id)
        assert kept_row is not None
        kept_row.status = AnalysisStatus.ACCEPTED.value
        kept_row.expires_at = _past()
        add_uploaded_image(
            session,
            image_id=image_id,
            analysis_id=kept_id,
            filename="kept.png",
            content_type="image/png",
            byte_size=len(b"kept-screenshot"),
            storage_path=str(loan_file.resolve()),
        )
        book_row = session.get(BookRow, book_id)
        assert book_row is not None
        book_row.result_json = json.dumps(
            {
                "correction_hints": [_HINT],
                "lender_groups": [
                    {"loans": [{"loan_internal_id": loan_id, "image_ids": [image_id]}]}
                ],
            }
        )
        stale_row = session.get(AnalysisRow, stale_id)
        assert stale_row is not None
        stale_row.expires_at = _past()
        session.commit()
    finally:
        session.close()

    return {
        "book_id": book_id,
        "kept_id": kept_id,
        "stale_id": stale_id,
        "fresh_id": fresh_id,
        "loan_id": loan_id,
        "image_id": image_id,
        "loan_file": loan_file,
        "stale_dir": stale_dir,
    }


def test_get_expires_unaccepted_draft_and_keeps_accepted_loan(tmp_path: Path) -> None:
    application = _application(tmp_path)
    try:
        with TestClient(application) as client:
            seeded = _seed_book_with_expired_draft(client)
            stale_dir = seeded["stale_dir"]
            loan_file = seeded["loan_file"]
            assert isinstance(stale_dir, Path)
            assert isinstance(loan_file, Path)
            assert stale_dir.is_dir()
            assert loan_file.is_file()

            missing = client.get(f"/api/v1/analyses/{seeded['stale_id']}")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "ANALYSIS_NOT_FOUND"
            assert missing.json()["error"]["retryable"] is False
            assert not stale_dir.exists()

            again = client.get(f"/api/v1/analyses/{seeded['stale_id']}")
            assert again.status_code == 404

            session = application.state.session_factory()
            try:
                expired = session.get(AnalysisRow, seeded["stale_id"])
                assert expired is not None
                assert expired.status == AnalysisStatus.EXPIRED.value
            finally:
                session.close()

            kept = client.get(f"/api/v1/analyses/{seeded['kept_id']}")
            assert kept.status_code == 200
            assert kept.json()["status"] == "accepted"

            fresh = client.get(f"/api/v1/analyses/{seeded['fresh_id']}")
            assert fresh.status_code == 200
            assert fresh.json()["status"] == "created"

            book = client.get(f"/api/v1/books/{seeded['book_id']}")
            assert book.status_code == 200
            assert book.json()["book_id"] == seeded["book_id"]
            assert seeded["loan_id"] in book.text
            assert loan_file.read_bytes() == b"kept-screenshot"

            image = client.get(
                f"/api/v1/books/{seeded['book_id']}/loans/{seeded['loan_id']}/images/{seeded['image_id']}"
            )
            assert image.status_code == 200
            assert image.content == b"kept-screenshot"
    finally:
        application.state.engine.dispose()


def test_startup_sweep_removes_expired_draft_files_only(tmp_path: Path) -> None:
    application = _application(tmp_path)
    try:
        with TestClient(application) as client:
            seeded = _seed_book_with_expired_draft(client)
    finally:
        application.state.engine.dispose()

    restarted = _application(tmp_path)
    try:
        stale_dir = seeded["stale_dir"]
        loan_file = seeded["loan_file"]
        assert isinstance(stale_dir, Path)
        assert isinstance(loan_file, Path)
        assert not stale_dir.exists()
        assert loan_file.read_bytes() == b"kept-screenshot"

        session = restarted.state.session_factory()
        try:
            expired = session.get(AnalysisRow, seeded["stale_id"])
            assert expired is not None
            assert expired.status == AnalysisStatus.EXPIRED.value
            kept = session.get(AnalysisRow, seeded["kept_id"])
            assert kept is not None
            assert kept.status == AnalysisStatus.ACCEPTED.value
        finally:
            session.close()

        with TestClient(restarted) as client:
            missing = client.get(f"/api/v1/analyses/{seeded['stale_id']}")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "ANALYSIS_NOT_FOUND"
            book = client.get(f"/api/v1/books/{seeded['book_id']}")
            assert book.status_code == 200
            assert seeded["loan_id"] in book.text
            assert loan_file.read_bytes() == b"kept-screenshot"
    finally:
        restarted.state.engine.dispose()


def test_delete_draft_removes_analysis_directory_and_keeps_book_files(tmp_path: Path) -> None:
    application = _application(tmp_path)
    try:
        with TestClient(application) as client:
            book = client.post("/api/v1/books", json={})
            book_id = book.json()["book_id"]
            draft = client.post(f"/api/v1/books/{book_id}/drafts", json={})
            analysis_id = draft.json()["analysis_id"]
            tmp = Path(client.app.state.settings.loanlens_tmp_dir)
            draft_dir = tmp / analysis_id
            draft_dir.mkdir(parents=True)
            (draft_dir / "img_a").write_bytes(b"draft")
            nested = draft_dir / "nested"
            nested.mkdir()
            (nested / "copy").write_bytes(b"copy")
            book_file = tmp / "books" / book_id / "lon_saved" / "img_saved"
            book_file.parent.mkdir(parents=True)
            book_file.write_bytes(b"saved")

            deleted = client.delete(f"/api/v1/analyses/{analysis_id}")
            assert deleted.status_code == 204
            assert deleted.content == b""
            assert not draft_dir.exists()
            assert book_file.read_bytes() == b"saved"

            missing = client.get(f"/api/v1/analyses/{analysis_id}")
            assert missing.status_code == 404
            still = client.get(f"/api/v1/books/{book_id}")
            assert still.status_code == 200
            assert still.json()["book_id"] == book_id
    finally:
        application.state.engine.dispose()


def test_delete_loan_removes_that_loan_and_keeps_the_book(tmp_path: Path) -> None:
    application = _application(tmp_path)
    try:
        with TestClient(application) as client:
            book = client.post("/api/v1/books", json={})
            book_id = book.json()["book_id"]
            tmp = Path(client.app.state.settings.loanlens_tmp_dir)
            drop_file = tmp / "books" / book_id / "lon_drop" / "img_drop"
            keep_file = tmp / "books" / book_id / "lon_keep" / "img_keep"
            drop_file.parent.mkdir(parents=True)
            keep_file.parent.mkdir(parents=True)
            drop_file.write_bytes(b"drop")
            keep_file.write_bytes(b"keep")

            session = application.state.session_factory()
            try:
                row = session.get(BookRow, book_id)
                assert row is not None
                row.result_json = json.dumps(
                    {
                        "correction_hints": [_HINT],
                        "lender_groups": [
                            {
                                "lender_group_id": "ldg_1",
                                "loans": [
                                    {"loan_internal_id": "lon_drop", "image_ids": ["img_drop"]},
                                    {"loan_internal_id": "lon_keep", "image_ids": ["img_keep"]},
                                ],
                            }
                        ],
                    }
                )
                session.commit()
            finally:
                session.close()

            unknown_book = client.delete("/api/v1/books/bok_missing/loans/lon_drop")
            assert unknown_book.status_code == 404
            assert unknown_book.json()["error"]["code"] == "BOOK_NOT_FOUND"

            unknown_loan = client.delete(f"/api/v1/books/{book_id}/loans/lon_missing")
            assert unknown_loan.status_code == 404
            assert unknown_loan.json()["error"]["code"] == "LOAN_NOT_FOUND"
            assert drop_file.is_file()

            deleted = client.delete(f"/api/v1/books/{book_id}/loans/lon_drop")
            assert deleted.status_code == 204
            assert deleted.content == b""
            assert not drop_file.exists()
            assert not drop_file.parent.exists()
            assert keep_file.read_bytes() == b"keep"

            loaded = client.get(f"/api/v1/books/{book_id}")
            assert loaded.status_code == 200
            assert loaded.json()["book_id"] == book_id
            assert "lon_drop" not in loaded.text
            assert "lon_keep" in loaded.text

            session = application.state.session_factory()
            try:
                stored = session.get(BookRow, book_id)
                assert stored is not None
                assert read_correction_hints(stored.result_json) == [_HINT]
            finally:
                session.close()
    finally:
        application.state.engine.dispose()


def test_purge_helpers_leave_the_book_tree_in_place(tmp_path: Path) -> None:
    book_file = tmp_path / "books" / "bok_1" / "lon_1" / "img_1"
    book_file.parent.mkdir(parents=True)
    book_file.write_bytes(b"keep")
    draft = tmp_path / "anl_1"
    draft.mkdir()
    (draft / "img").write_bytes(b"draft")

    purge_draft_directory(tmp_path, "books")
    purge_draft_directory(tmp_path, "..")
    purge_draft_directory(tmp_path, "anl_1")
    purge_loan_directory(tmp_path, "..", "lon_1")
    purge_loan_directory(tmp_path, "bok_1", "..")

    assert not draft.exists()
    assert book_file.read_bytes() == b"keep"

    purge_loan_directory(tmp_path, "bok_1", "lon_1")
    assert not book_file.exists()
    assert (tmp_path / "books").is_dir()


def test_health_and_error_json_omit_vision_api_key(tmp_path: Path) -> None:
    application = _application(tmp_path, vision_api_key=_SECRET)
    try:
        with TestClient(application) as client:
            health = client.get("/api/v1/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}
            assert _SECRET not in health.text

            missing = client.get("/api/v1/analyses/anl_missing")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "ANALYSIS_NOT_FOUND"
            assert _SECRET not in missing.text

            rejected = client.post("/api/v1/books", json={"note": _SECRET})
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "REQUEST_INVALID"
            assert _SECRET not in rejected.text

            unknown = client.get(f"/api/v1/{_SECRET}")
            assert unknown.status_code == 404
            assert _SECRET not in unknown.text
    finally:
        configure_logging("INFO", secrets=())
        application.state.engine.dispose()


def test_logs_redact_vision_api_key(tmp_path: Path) -> None:
    application = _application(tmp_path, vision_api_key=_SECRET)
    try:
        logger = logging.getLogger(LOGGER_NAME)
        formatter = next(
            handler.formatter
            for handler in logger.handlers
            if isinstance(handler.formatter, StructuredFormatter)
        )
        lines: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                lines.append(formatter.format(record))

        capture = _Capture()
        logger.addHandler(capture)
        try:
            log_event(
                f"provider {_SECRET}",
                analysis_id=f"anl_{_SECRET}",
                status="completed",
                filename="borrower.png",
                borrower_name="Hidden",
                loan_id="LN-9",
                amount=15000,
            )
        finally:
            logger.removeHandler(capture)

        assert lines
        assert _SECRET not in lines[0]
        assert "[REDACTED]" in lines[0]
        assert "borrower.png" not in lines[0]
        assert "Hidden" not in lines[0]
        assert "LN-9" not in lines[0]
        assert "15000" not in lines[0]
        assert "filename" not in lines[0]
    finally:
        configure_logging("INFO", secrets=())
        application.state.engine.dispose()


def test_locked_draft_directory_does_not_stop_the_sweep(tmp_path: Path, monkeypatch) -> None:
    application = _application(tmp_path)
    client = TestClient(application)
    try:
        seeded = _seed_book_with_expired_draft(client)
        stale_id = seeded["stale_id"]
        assert isinstance(stale_id, str)

        def locked(_tmp_dir: Path, _analysis_id: str) -> None:
            raise PermissionError("denied")

        monkeypatch.setattr("app.services.retention.purge_draft_directory", locked)
        session = application.state.session_factory()
        try:
            expired = sweep_expired_drafts(session, Path(application.state.settings.loanlens_tmp_dir))
            session.commit()
            row = session.get(AnalysisRow, stale_id)
        finally:
            session.close()
        assert expired == []
        assert row is not None
        assert row.status != AnalysisStatus.EXPIRED.value
    finally:
        application.state.engine.dispose()


def test_env_file_is_gitignored_and_example_key_is_blank() -> None:
    root = Path(__file__).resolve().parents[2]
    ignored = [line.strip() for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert ".env" in ignored

    example = (root / ".env.example").read_text(encoding="utf-8")
    key_lines = [line for line in example.splitlines() if line.startswith("VISION_API_KEY=")]
    assert key_lines == ["VISION_API_KEY="]
    assert _SECRET not in example
