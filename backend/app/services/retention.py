"""Draft TTL sweep and file purge (SPEC §15.3, §16)."""

import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.models.enums import AnalysisStatus
from app.persistence import analyses as store


def sweep_expired_drafts(
    session: Session,
    tmp_dir: Path,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Expire unaccepted drafts past `expires_at` and delete only their files.

    Accepted loans under `{tmp}/books/` are left in place.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    expired: list[str] = []
    for row in store.list_drafts_past_expiry(session, moment):
        try:
            purge_draft_directory(tmp_dir, row.id)
        except OSError:
            log_event(
                "draft_purge_failed",
                analysis_id=row.id,
                error_code="DRAFT_PURGE_FAILED",
            )
            continue
        store.expire_draft(session, row)
        log_event(
            "draft_expired",
            analysis_id=row.id,
            status=AnalysisStatus.EXPIRED.value,
        )
        expired.append(row.id)
    return expired


def commit_expired_draft_sweep(open_session: Callable[[], Session], tmp_dir: Path) -> None:
    """Run the TTL sweep in its own transaction so a later 404 cannot roll it back."""
    session = open_session()
    try:
        sweep_expired_drafts(session, tmp_dir)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def purge_draft_directory(tmp_dir: Path, analysis_id: str) -> None:
    """Delete `{tmp}/{analysis_id}/`. Never delete `{tmp}/books/`."""
    if analysis_id == "books":
        return
    directory = _joined(tmp_dir, analysis_id)
    _remove_tree(directory)


def purge_loan_directory(tmp_dir: Path, book_id: str, loan_id: str) -> None:
    """Delete `{tmp}/books/{book_id}/{loan_id}/` and an empty book folder."""
    loan_dir = _joined(tmp_dir, "books", book_id, loan_id)
    _remove_tree(loan_dir)
    book_dir = _joined(tmp_dir, "books", book_id)
    if book_dir is not None and book_dir.is_dir() and not any(book_dir.iterdir()):
        book_dir.rmdir()


def _remove_tree(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    elif path.is_file():
        path.unlink()


def _joined(root: Path, *segments: str) -> Path | None:
    if not segments or any(not _safe_segment(segment) for segment in segments):
        return None
    base = root.resolve()
    path = base
    for segment in segments:
        path = (path / segment).resolve()
        if path == base or not path.is_relative_to(base):
            return None
    return path


def _safe_segment(segment: str) -> bool:
    if segment in {"", ".", ".."}:
        return False
    if "/" in segment or "\\" in segment:
        return False
    return Path(segment).name == segment
