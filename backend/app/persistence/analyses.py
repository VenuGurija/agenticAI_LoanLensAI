"""Create, read, and delete books, drafts, and draft images (SPEC §14.3, §14.4, §14.11, §15)."""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import ERROR_SPECS
from app.core.ids import new_analysis_id, new_book_id
from app.models.database import AnalysisRow, BookRow, ImageRow
from app.models.enums import AnalysisStatus, ImageStatus


def utc_timestamp(moment: datetime | None = None) -> str:
    current = moment if moment is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc).replace(microsecond=0)
    return current.strftime("%Y-%m-%dT%H:%M:%SZ")


def create_book(session: Session) -> BookRow:
    row = BookRow(id=new_book_id(), created_at=utc_timestamp(), result_json=None)
    session.add(row)
    session.flush()
    return row


def get_book(session: Session, book_id: str) -> BookRow | None:
    return session.get(BookRow, book_id)


def create_draft(session: Session, book_id: str, ttl_hours: int) -> AnalysisRow:
    created = datetime.now(timezone.utc).replace(microsecond=0)
    row = AnalysisRow(
        id=new_analysis_id(),
        book_id=book_id,
        status=AnalysisStatus.CREATED.value,
        created_at=utc_timestamp(created),
        completed_at=None,
        expires_at=utc_timestamp(created + timedelta(hours=ttl_hours)),
        error_code=None,
        error_message=None,
        queued_at=None,
        pages_done=0,
        pages_total=0,
        result_json=None,
    )
    session.add(row)
    session.flush()
    return row


def get_analysis(session: Session, analysis_id: str) -> AnalysisRow | None:
    return session.get(AnalysisRow, analysis_id)


def delete_draft(session: Session, analysis_id: str) -> bool:
    """Remove one draft row. Book loans in `books.result_json` stay in place."""
    row = session.get(AnalysisRow, analysis_id)
    if row is None:
        return False
    session.execute(delete(ImageRow).where(ImageRow.analysis_id == analysis_id))
    session.delete(row)
    session.flush()
    return True


def list_drafts_past_expiry(session: Session, now: datetime) -> list[AnalysisRow]:
    """Unaccepted drafts whose `expires_at` is set and earlier than `now`."""
    cutoff = utc_timestamp(now)
    statement = select(AnalysisRow).where(
        AnalysisRow.expires_at.is_not(None),
        AnalysisRow.expires_at < cutoff,
        AnalysisRow.status != AnalysisStatus.ACCEPTED.value,
        AnalysisRow.status != AnalysisStatus.EXPIRED.value,
    )
    return list(session.scalars(statement))


def expire_draft(session: Session, row: AnalysisRow) -> None:
    """Mark a draft expired and drop its image rows. Book loans stay."""
    session.execute(delete(ImageRow).where(ImageRow.analysis_id == row.id))
    row.status = AnalysisStatus.EXPIRED.value
    session.flush()


def remove_book_loan(session: Session, book: BookRow, loan_internal_id: str) -> bool:
    """Drop one loan from the book document. Other loans and correction hints stay."""
    document = _book_document(book.result_json)
    groups = document.get("lender_groups")
    if not isinstance(groups, list):
        return False
    found = False
    kept_groups: list[Any] = []
    for group in groups:
        if not isinstance(group, dict):
            kept_groups.append(group)
            continue
        loans = group.get("loans")
        if not isinstance(loans, list):
            kept_groups.append(group)
            continue
        kept_loans: list[Any] = []
        for loan in loans:
            if isinstance(loan, dict) and loan.get("loan_internal_id") == loan_internal_id:
                found = True
                continue
            kept_loans.append(loan)
        if not kept_loans:
            continue
        updated = dict(group)
        updated["loans"] = kept_loans
        kept_groups.append(updated)
    if not found:
        return False
    document["lender_groups"] = kept_groups
    book.result_json = json.dumps(document)
    session.flush()
    return True


def list_images(session: Session, analysis_id: str) -> list[ImageRow]:
    statement = (
        select(ImageRow)
        .where(ImageRow.analysis_id == analysis_id)
        .order_by(ImageRow.created_at, ImageRow.id)
    )
    return list(session.scalars(statement))


def add_uploaded_image(
    session: Session,
    *,
    image_id: str,
    analysis_id: str,
    filename: str,
    content_type: str,
    byte_size: int,
    storage_path: str,
) -> ImageRow:
    row = ImageRow(
        id=image_id,
        analysis_id=analysis_id,
        filename=filename,
        content_type=content_type,
        byte_size=byte_size,
        checksum_sha256=None,
        width=None,
        height=None,
        storage_path=storage_path,
        status=ImageStatus.UPLOADED.value,
        error_code=None,
        document_class=None,
        created_at=utc_timestamp(),
    )
    session.add(row)
    session.flush()
    return row


def record_preprocessed_image(
    session: Session,
    row: ImageRow,
    *,
    checksum_sha256: str,
    width: int,
    height: int,
    storage_path: str,
    content_type: str,
    byte_size: int,
) -> ImageRow:
    """Store path, checksum, and dimensions. Never store image bytes."""
    row.checksum_sha256 = checksum_sha256
    row.width = width
    row.height = height
    row.storage_path = storage_path
    row.content_type = content_type
    row.byte_size = byte_size
    row.status = ImageStatus.PREPROCESSED.value
    row.error_code = None
    session.flush()
    return row


def get_image(session: Session, analysis_id: str, image_id: str) -> ImageRow | None:
    row = session.get(ImageRow, image_id)
    if row is None or row.analysis_id != analysis_id:
        return None
    return row


def delete_image_row(session: Session, row: ImageRow) -> None:
    session.delete(row)
    session.flush()


def mark_images_pending(session: Session, analysis: AnalysisRow) -> None:
    """First accepted upload moves `created` to `images_pending` (SPEC §18)."""
    if analysis.status == AnalysisStatus.CREATED.value:
        analysis.status = AnalysisStatus.IMAGES_PENDING.value
        session.flush()


def mark_queued(
    session: Session,
    analysis: AnalysisRow,
    *,
    pages_total: int,
) -> None:
    """Queue this draft. Does not call vision or copy loans into the book.

    A fresh upload starts progress at zero. A retryable failure keeps
    `pages_done` and `result_json` so a later worker can resume.
    """
    fresh = analysis.status == AnalysisStatus.IMAGES_PENDING.value
    analysis.status = AnalysisStatus.QUEUED.value
    analysis.queued_at = utc_timestamp()
    analysis.pages_total = pages_total
    if fresh:
        analysis.pages_done = 0
    analysis.error_code = None
    analysis.error_message = None
    analysis.completed_at = None
    session.flush()


def failure_is_retryable(analysis: AnalysisRow) -> bool:
    """True when SPEC §14.9 marks this draft's error as retryable."""
    if analysis.error_code is None:
        return False
    spec = ERROR_SPECS.get(analysis.error_code)
    return spec is not None and spec.retryable


def append_correction_hints(
    session: Session,
    book: BookRow,
    hints: Sequence[Mapping[str, Any]],
) -> None:
    """Append same-lender correction records on the book. Loans already stored stay."""

    document = _book_document(book.result_json)
    records = read_correction_hints(book.result_json)
    for hint in hints:
        records.append(_correction_hint(hint))
    document["correction_hints"] = records
    book.result_json = json.dumps(document)
    session.flush()


def read_correction_hints(result_json: str | None) -> list[dict[str, Any]]:
    """Hint records stored on a book. Invalid entries are omitted."""

    stored = _book_document(result_json).get("correction_hints")
    if not isinstance(stored, list):
        return []
    hints: list[dict[str, Any]] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        try:
            hints.append(_correction_hint(item))
        except ValueError:
            continue
    return hints


def _book_document(result_json: str | None) -> dict[str, Any]:
    if not result_json:
        return {}
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _correction_hint(hint: Mapping[str, Any]) -> dict[str, Any]:
    lender_key = hint.get("lender_key")
    field_name = hint.get("field_name")
    if not isinstance(lender_key, str) or lender_key == "":
        raise ValueError("correction hint requires lender_key")
    if not isinstance(field_name, str) or field_name == "":
        raise ValueError("correction hint requires field_name")
    return {
        "lender_key": lender_key,
        "field_name": field_name,
        "previous_value": _hint_value(hint.get("previous_value")),
        "corrected_value": _hint_value(hint.get("corrected_value")),
    }


def _hint_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ValueError("correction hint values must be strings or null")
