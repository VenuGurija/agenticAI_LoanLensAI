"""Book, draft, inbox, summary, correction, and screenshot endpoints (SPEC §14)."""

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy import func, select
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.ids import new_image_id
from app.services.retention import (
    commit_expired_draft_sweep,
    purge_draft_directory,
    purge_loan_directory,
)
from app.models.database import AnalysisRow, BookRow, ImageRow, get_session
from app.models.enums import AnalysisStatus, ImageStatus
from app.persistence import analyses as store
from app.schemas.analysis import (
    AnalysisErrorBody,
    AnalysisGet,
    BookCreated,
    BookGet,
    BookInbox,
    DraftAccepted,
    DraftCreated,
    EmptyRequest,
    ImageUploadResult,
    InboxBucket,
    InboxCounts,
    InboxDraft,
    UploadedImage,
)
from app.schemas.extraction import Confidence, DateField, FieldSource, MoneyField, StringField
from app.services.extraction import lender_groups_for_assignment
from app.services.grouping.group import lender_grouping_key
from app.services.summary import (
    build_analysis_summary,
    build_book_summary,
    display_sanctioned_short,
    summary_line,
)
from app.services.validation.money import calculate_fields
from app.services.validation.rules import attach_loan_validations
from app.services.images.upload import (
    book_image_path,
    draft_image_path,
    read_upload,
    remove_stored_file,
    resolve_tmp_dir,
)

router = APIRouter()


class SummaryLineBody(BaseModel):
    loan_internal_id: str
    text: str
    needs_review: bool


class SummaryGroupBody(BaseModel):
    header: str
    lines: list[SummaryLineBody]


class AnalysisSummaryBody(BaseModel):
    analysis_id: str
    status: str
    groups: list[SummaryGroupBody]


class BookTotalsBody(BaseModel):
    received: str
    still_to_pay: str
    extra: str
    incomplete: bool


class BookSummaryBody(BaseModel):
    book_id: str
    totals: BookTotalsBody
    groups: list[SummaryGroupBody]


_IMAGE_MUTABLE_STATUSES = frozenset(
    {AnalysisStatus.CREATED.value, AnalysisStatus.IMAGES_PENDING.value}
)
_PREVIEW_STATUSES = frozenset(
    {AnalysisStatus.COMPLETED.value, AnalysisStatus.COMPLETED_NEEDS_REVIEW.value}
)
_IN_PROGRESS_STATUSES = frozenset(
    {
        AnalysisStatus.QUEUED.value,
        AnalysisStatus.RUNNING.value,
        AnalysisStatus.PREPROCESSING.value,
        AnalysisStatus.CLASSIFYING.value,
        AnalysisStatus.EXTRACTING.value,
        AnalysisStatus.GROUPING.value,
        AnalysisStatus.VALIDATING.value,
    }
)
_MONEY_FIELDS = frozenset(
    {
        "sanctioned_amount",
        "disbursed_amount",
        "repayment_amount",
        "processing_fee",
        "gst_amount",
        "other_fees_total",
    }
)
_DATE_FIELDS = frozenset({"sanction_date", "disbursement_date", "repayment_date"})
_TEXT_FIELDS = frozenset({"lender_brand", "legal_lending_entity"})
_ASSIGNMENT_MESSAGE = "Each screenshot on a proposed loan must be assigned once."
# SPEC §14.12. Statuses outside this map (created, images_pending, accepted, expired)
# are not inbox rows.
_INBOX_BUCKET: dict[str, InboxBucket] = {
    AnalysisStatus.QUEUED.value: "queued",
    AnalysisStatus.RUNNING.value: "running",
    AnalysisStatus.PREPROCESSING.value: "running",
    AnalysisStatus.CLASSIFYING.value: "running",
    AnalysisStatus.EXTRACTING.value: "running",
    AnalysisStatus.GROUPING.value: "running",
    AnalysisStatus.VALIDATING.value: "running",
    AnalysisStatus.COMPLETED.value: "ready",
    AnalysisStatus.COMPLETED_NEEDS_REVIEW.value: "needs_review",
    AnalysisStatus.FAILED.value: "failed",
}
_INBOX_SORT = {
    "needs_review": 0,
    "ready": 1,
    "failed": 2,
    "running": 3,
    "queued": 4,
}


class LoanCorrection(BaseModel):
    """Partial replacement of amount, date, or lender fields. Omitted keys stay."""

    model_config = ConfigDict(extra="forbid")

    sanctioned_amount: str | None = None
    disbursed_amount: str | None = None
    repayment_amount: str | None = None
    processing_fee: str | None = None
    gst_amount: str | None = None
    other_fees_total: str | None = None
    sanction_date: str | None = None
    disbursement_date: str | None = None
    repayment_date: str | None = None
    lender_brand: str | None = None
    legal_lending_entity: str | None = None


class GroupingLoanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loan_internal_id: str
    image_ids: list[str]


class GroupingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loans: list[GroupingLoanBody]


@router.post("/books", status_code=201, response_model=BookCreated)
def create_book(
    session: Session = Depends(get_session),
    _body: EmptyRequest = Body(default_factory=EmptyRequest),
) -> BookCreated:
    row = store.create_book(session)
    return BookCreated(book_id=row.id, created_at=row.created_at)


@router.get("/books/{book_id}", response_model=BookGet)
def get_book(book_id: str, session: Session = Depends(get_session)) -> BookGet:
    row = store.get_book(session, book_id)
    if row is None:
        raise AppError("BOOK_NOT_FOUND")
    groups = _public_lender_groups(row.result_json)
    _attach_calculated(groups)
    return BookGet(book_id=row.id, created_at=row.created_at, lender_groups=groups)


@router.post("/books/{book_id}/drafts", status_code=201, response_model=DraftCreated)
def create_draft(
    book_id: str,
    request: Request,
    session: Session = Depends(get_session),
    _body: EmptyRequest = Body(default_factory=EmptyRequest),
) -> DraftCreated:
    if store.get_book(session, book_id) is None:
        raise AppError("BOOK_NOT_FOUND")
    ttl_hours = request.app.state.settings.loanlens_analysis_ttl_hours
    row = store.create_draft(session, book_id, ttl_hours)
    if row.expires_at is None:
        raise AppError("INTERNAL_ERROR")
    return DraftCreated(
        book_id=row.book_id,
        analysis_id=row.id,
        status=AnalysisStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


@router.post(
    "/books/{book_id}/drafts/{analysis_id}/accept",
    response_model=DraftAccepted,
)
def accept_draft(
    book_id: str,
    analysis_id: str,
    request: Request,
    session: Session = Depends(get_session),
    _body: EmptyRequest = Body(default_factory=EmptyRequest),
) -> DraftAccepted:
    """Copy preview loans into the book. A second accept does not duplicate them."""

    book = store.get_book(session, book_id)
    if book is None:
        raise AppError("BOOK_NOT_FOUND")
    row = _visible_draft(analysis_id, request, session)
    if row.book_id != book_id:
        raise AppError("ANALYSIS_NOT_FOUND")
    if row.status == AnalysisStatus.ACCEPTED.value:
        return DraftAccepted(
            book_id=book_id,
            loan_internal_ids=_draft_ids_still_on_book(book, row),
        )
    if row.status not in _PREVIEW_STATUSES:
        raise AppError("INVALID_STATUS")

    document = _load_document(row.result_json)
    incoming = document.get("lender_groups")
    if not isinstance(incoming, list):
        incoming = []
    book_document = _load_document(book.result_json)
    loan_ids = _append_preview_loans(book_document, incoming)
    book.result_json = json.dumps(book_document)
    _move_loan_images(
        session,
        row,
        book_id,
        incoming,
        resolve_tmp_dir(request.app.state.settings.loanlens_tmp_dir),
    )
    row.status = AnalysisStatus.ACCEPTED.value
    row.expires_at = None
    session.flush()
    return DraftAccepted(book_id=book_id, loan_internal_ids=loan_ids)


@router.get("/books/{book_id}/inbox", response_model=BookInbox)
def get_book_inbox(
    book_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> BookInbox:
    """Counts and rows for this book's drafts. No amounts, names, or summary lines."""

    if store.get_book(session, book_id) is None:
        raise AppError("BOOK_NOT_FOUND")
    _sweep(request, session)
    return _book_inbox(session, book_id)


@router.get("/books/{book_id}/summary", response_model=BookSummaryBody)
def get_book_summary(book_id: str, session: Session = Depends(get_session)) -> BookSummaryBody:
    row = store.get_book(session, book_id)
    if row is None:
        raise AppError("BOOK_NOT_FOUND")
    return BookSummaryBody.model_validate(
        build_book_summary(row.id, _public_lender_groups(row.result_json))
    )


@router.get("/analyses/{analysis_id}/summary", response_model=AnalysisSummaryBody)
def get_analysis_summary(
    analysis_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> AnalysisSummaryBody:
    row = _visible_draft(analysis_id, request, session)
    return AnalysisSummaryBody.model_validate(
        build_analysis_summary(row.id, row.status, _public_lender_groups(row.result_json))
    )


@router.get("/analyses/{analysis_id}", response_model=AnalysisGet)
def get_draft(
    analysis_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> AnalysisGet:
    row = _visible_draft(analysis_id, request, session)
    return _analysis_body(session, row)


@router.post(
    "/analyses/{analysis_id}/analyze",
    response_model=AnalysisGet,
    responses={202: {"model": AnalysisGet}},
)
def analyze_draft(
    analysis_id: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    _body: EmptyRequest = Body(default_factory=EmptyRequest),
) -> AnalysisGet:
    """Queue the draft. This request does not call the vision provider.

    A finished preview is returned as stored. The book is not changed.
    """

    row = _visible_draft(analysis_id, request, session)
    if row.status == AnalysisStatus.ACCEPTED.value:
        raise AppError("INVALID_STATUS")
    if row.status in _PREVIEW_STATUSES:
        return _analysis_body(session, row)
    if row.status in _IN_PROGRESS_STATUSES:
        response.status_code = 202
        return _analysis_body(session, row)
    if row.status == AnalysisStatus.FAILED.value and not store.failure_is_retryable(row):
        return _analysis_body(session, row)
    if row.status == AnalysisStatus.CREATED.value:
        raise AppError("NO_IMAGES")
    if row.status not in {AnalysisStatus.IMAGES_PENDING.value, AnalysisStatus.FAILED.value}:
        raise AppError("INVALID_STATUS")

    images = store.list_images(session, row.id)
    if not images:
        raise AppError("NO_IMAGES")
    store.mark_queued(session, row, pages_total=len(images))
    response.status_code = 202
    return _analysis_body(session, row)


@router.delete("/analyses/{analysis_id}", status_code=204)
def delete_draft(
    analysis_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    if store.get_analysis(session, analysis_id) is None or not store.delete_draft(session, analysis_id):
        raise AppError("ANALYSIS_NOT_FOUND")
    purge_draft_directory(resolve_tmp_dir(request.app.state.settings.loanlens_tmp_dir), analysis_id)
    return Response(status_code=204)


@router.post("/analyses/{analysis_id}/images", status_code=201, response_model=ImageUploadResult)
def upload_images(
    analysis_id: str,
    request: Request,
    session: Session = Depends(get_session),
    files: list[UploadFile] = File(),
) -> ImageUploadResult:
    analysis = _visible_draft(analysis_id, request, session)
    _require_image_mutable(analysis.status)
    if not files:
        raise AppError("NO_IMAGES")

    settings = request.app.state.settings
    existing = len(store.list_images(session, analysis.id))
    if existing + len(files) > settings.loanlens_max_images_per_analysis:
        raise AppError("TOO_MANY_IMAGES")

    pending = [read_upload(upload, settings.loanlens_max_image_bytes) for upload in files]
    tmp_dir = resolve_tmp_dir(settings.loanlens_tmp_dir)
    written: list[str] = []
    rows: list[ImageRow] = []
    try:
        for item in pending:
            image_id = new_image_id()
            path = draft_image_path(tmp_dir, analysis.id, image_id)
            written.append(str(path))
            path.write_bytes(item.data)
            rows.append(
                store.add_uploaded_image(
                    session,
                    image_id=image_id,
                    analysis_id=analysis.id,
                    filename=item.filename,
                    content_type=item.content_type,
                    byte_size=len(item.data),
                    storage_path=str(path),
                )
            )
        store.mark_images_pending(session, analysis)
    except Exception:
        for path in written:
            remove_stored_file(tmp_dir, path)
        raise

    return ImageUploadResult(
        analysis_id=analysis.id,
        status=AnalysisStatus(analysis.status),
        images=[_public_image(row) for row in rows],
    )


@router.delete("/analyses/{analysis_id}/images/{image_id}", status_code=204)
def delete_image(
    analysis_id: str,
    image_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    analysis = _visible_draft(analysis_id, request, session)
    _require_image_mutable(analysis.status)
    image = store.get_image(session, analysis.id, image_id)
    if image is None:
        raise HTTPException(status_code=404)
    storage_path = image.storage_path
    store.delete_image_row(session, image)
    remove_stored_file(resolve_tmp_dir(request.app.state.settings.loanlens_tmp_dir), storage_path)
    return Response(status_code=204)


@router.patch("/analyses/{analysis_id}/loans/{loan_internal_id}")
def patch_preview_loan(
    analysis_id: str,
    loan_internal_id: str,
    body: LoanCorrection,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Correct a draft loan. Calculated extra is recomputed. Vision is not called."""

    row = _visible_draft(analysis_id, request, session)
    if row.status not in _PREVIEW_STATUSES:
        raise AppError("INVALID_STATUS")
    return _patch_stored_loan(session, row, loan_internal_id, body)


@router.patch("/books/{book_id}/loans/{loan_internal_id}")
def patch_book_loan(
    book_id: str,
    loan_internal_id: str,
    body: LoanCorrection,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Correct a saved loan. Calculated extra is recomputed. Vision is not called."""

    row = store.get_book(session, book_id)
    if row is None:
        raise AppError("BOOK_NOT_FOUND")
    return _patch_stored_loan(session, row, loan_internal_id, body)


@router.delete("/books/{book_id}/loans/{loan_internal_id}", status_code=204)
def delete_loan(
    book_id: str,
    loan_internal_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> Response:
    """Remove one saved loan and its screenshot directory. The book stays."""

    book = store.get_book(session, book_id)
    if book is None:
        raise AppError("BOOK_NOT_FOUND")
    if not store.remove_book_loan(session, book, loan_internal_id):
        raise AppError("LOAN_NOT_FOUND")
    purge_loan_directory(
        resolve_tmp_dir(request.app.state.settings.loanlens_tmp_dir),
        book_id,
        loan_internal_id,
    )
    return Response(status_code=204)


@router.post("/analyses/{analysis_id}/grouping", response_model=AnalysisGet)
def regroup_draft(
    analysis_id: str,
    body: GroupingBody,
    request: Request,
    session: Session = Depends(get_session),
) -> AnalysisGet:
    """Move draft images between proposed loans and re-merge fields. Vision is not called."""

    row = _visible_draft(analysis_id, request, session)
    if row.status not in _PREVIEW_STATUSES:
        raise AppError("INVALID_STATUS")
    document = _load_document(row.result_json)
    raw_extracts = document.get("raw_extracts")
    if not isinstance(raw_extracts, list):
        raise AppError("INVALID_STATUS")
    groups = document.get("lender_groups")
    if not isinstance(groups, list):
        groups = []
    _require_complete_assignment(groups, body)
    assignment = [item.model_dump() for item in body.loans]
    try:
        merged = lender_groups_for_assignment(raw_extracts, assignment)
    except (TypeError, ValueError, ValidationError):
        raise AppError("INVALID_STATUS", _ASSIGNMENT_MESSAGE) from None
    attach_loan_validations(merged)
    _attach_calculated(merged)
    document["lender_groups"] = merged
    row.result_json = json.dumps(document)
    session.flush()
    return _analysis_body(session, row)


@router.get("/books/{book_id}/loans/{loan_internal_id}/images/{image_id}")
def get_loan_image(
    book_id: str,
    loan_internal_id: str,
    image_id: str,
    request: Request,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Return one accepted-loan screenshot. Draft-only images are not served here."""

    _sweep(request, session)
    book = store.get_book(session, book_id)
    if book is None:
        raise AppError("BOOK_NOT_FOUND")
    document = _load_document(book.result_json)
    groups = document.get("lender_groups")
    if not isinstance(groups, list):
        groups = []
    located = _locate_loan(groups, loan_internal_id)
    if located is None:
        raise AppError("LOAN_NOT_FOUND")
    _group, loan = located
    if image_id not in _image_ids(loan):
        raise AppError("LOAN_NOT_FOUND")
    image = session.get(ImageRow, image_id)
    if image is None:
        raise AppError("LOAN_NOT_FOUND")
    analysis = store.get_analysis(session, image.analysis_id)
    if analysis is None or analysis.book_id != book_id:
        raise AppError("LOAN_NOT_FOUND")
    path = _readable_image_path(request, image.storage_path)
    return FileResponse(path, media_type=image.content_type)


def _sweep(request: Request, session: Session) -> None:
    commit_expired_draft_sweep(
        request.app.state.session_factory,
        resolve_tmp_dir(request.app.state.settings.loanlens_tmp_dir),
    )
    session.expire_all()


def _visible_draft(analysis_id: str, request: Request, session: Session) -> AnalysisRow:
    """Return a draft that is still readable. Expired drafts are 404."""
    _sweep(request, session)
    row = store.get_analysis(session, analysis_id)
    if row is None or row.status == AnalysisStatus.EXPIRED.value:
        raise AppError("ANALYSIS_NOT_FOUND")
    return row


def _require_image_mutable(status: str) -> None:
    if status not in _IMAGE_MUTABLE_STATUSES:
        raise AppError("INVALID_STATUS")


def _book_inbox(session: Session, book_id: str) -> BookInbox:
    rows = list(session.scalars(select(AnalysisRow).where(AnalysisRow.book_id == book_id)))
    listed = [row for row in rows if row.status in _INBOX_BUCKET]
    listed.sort(key=lambda row: (_INBOX_SORT[_INBOX_BUCKET[row.status]], row.created_at, row.id))
    image_counts = _image_counts(session, [row.id for row in listed])
    drafts = [_inbox_draft(row, image_counts.get(row.id, 0)) for row in listed]
    totals = {bucket: 0 for bucket in _INBOX_SORT}
    for draft in drafts:
        totals[draft.inbox_bucket] += 1
    return BookInbox(
        book_id=book_id,
        counts=InboxCounts(
            queued=totals["queued"],
            running=totals["running"],
            ready=totals["ready"],
            needs_review=totals["needs_review"],
            failed=totals["failed"],
        ),
        drafts=drafts,
    )


def _inbox_draft(row: AnalysisRow, image_count: int) -> InboxDraft:
    error = None
    if row.error_code is not None:
        error = AnalysisErrorBody(code=row.error_code, message=row.error_message or "")
    return InboxDraft(
        analysis_id=row.id,
        status=AnalysisStatus(row.status),
        inbox_bucket=_INBOX_BUCKET[row.status],
        pages_done=row.pages_done,
        pages_total=row.pages_total,
        image_count=image_count,
        created_at=row.created_at,
        queued_at=row.queued_at,
        retryable=row.status == AnalysisStatus.FAILED.value and store.failure_is_retryable(row),
        error=error,
    )


def _image_counts(session: Session, analysis_ids: list[str]) -> dict[str, int]:
    if not analysis_ids:
        return {}
    counted = session.execute(
        select(ImageRow.analysis_id, func.count())
        .where(ImageRow.analysis_id.in_(analysis_ids))
        .group_by(ImageRow.analysis_id)
    )
    return {analysis_id: int(count) for analysis_id, count in counted}


def _analysis_body(session: Session, row: AnalysisRow) -> AnalysisGet:
    error = None
    if row.error_code is not None:
        error = AnalysisErrorBody(code=row.error_code, message=row.error_message or "")
    return AnalysisGet(
        analysis_id=row.id,
        status=AnalysisStatus(row.status),
        created_at=row.created_at,
        completed_at=row.completed_at,
        expires_at=row.expires_at,
        error=error,
        pages_done=row.pages_done,
        pages_total=row.pages_total,
        queued_at=row.queued_at,
        images=[_public_image(image) for image in store.list_images(session, row.id)],
        lender_groups=_public_lender_groups(row.result_json),
    )


def _public_lender_groups(result_json: str | None) -> list[dict[str, object]]:
    """Public draft JSON includes lender groups only. `raw_extracts` stays on the row."""

    if not result_json:
        return []
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    groups = data.get("lender_groups")
    if not isinstance(groups, list):
        return []
    return [item for item in groups if isinstance(item, dict)]


def _public_image(row: ImageRow) -> UploadedImage:
    return UploadedImage(
        image_id=row.id,
        filename=row.filename,
        content_type=row.content_type,
        byte_size=row.byte_size,
        status=ImageStatus(row.status),
    )


def _patch_stored_loan(
    session: Session,
    row: AnalysisRow | BookRow,
    loan_internal_id: str,
    body: LoanCorrection,
) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise RequestValidationError([])
    document = _load_document(row.result_json)
    groups = document.get("lender_groups")
    if not isinstance(groups, list):
        raise AppError("LOAN_NOT_FOUND")
    located = _locate_loan(groups, loan_internal_id)
    if located is None:
        raise AppError("LOAN_NOT_FOUND")
    group, loan = located
    fields = loan.get("fields")
    if not isinstance(fields, dict):
        fields = {}
        loan["fields"] = fields
    captured: list[tuple[str, str | None, str | None]] = []
    for name, value in changes.items():
        previous = fields.get(name)
        fields[name] = _corrected_field(name, value, previous)
        captured.append((name, _scalar_value(previous), _scalar_value(fields.get(name))))
    attach_loan_validations(groups)
    loan["calculated"] = _calculated_payload(fields)
    loan["summary_line"] = summary_line(loan)
    _sync_group_label(group, changes)
    document["lender_groups"] = groups
    row.result_json = json.dumps(document)
    book = row if isinstance(row, BookRow) else store.get_book(session, row.book_id)
    if book is None:
        raise AppError("BOOK_NOT_FOUND")
    lender_key = _loan_lender_key(group, loan)
    store.append_correction_hints(
        session,
        book,
        [
            {
                "lender_key": lender_key,
                "field_name": name,
                "previous_value": previous_value,
                "corrected_value": corrected_value,
            }
            for name, previous_value, corrected_value in captured
        ],
    )
    return loan


def _corrected_field(name: str, value: Any, previous: Any) -> dict[str, Any]:
    if isinstance(value, str) and value.strip() == "":
        raise RequestValidationError([])
    try:
        if name in _MONEY_FIELDS:
            return _money_correction(value, previous)
        if name in _DATE_FIELDS:
            return DateField(
                value=value,
                source=FieldSource.USER_CORRECTED,
                confidence=Confidence.HIGH,
                evidence=None,
            ).model_dump(mode="json")
        if name in _TEXT_FIELDS:
            return StringField(
                value=value,
                source=FieldSource.USER_CORRECTED,
                confidence=Confidence.HIGH,
                evidence=None,
            ).model_dump(mode="json")
    except ValidationError:
        raise RequestValidationError([]) from None
    raise RequestValidationError([])


def _money_correction(value: Any, previous: Any) -> dict[str, Any]:
    currency = None
    if value is not None:
        currency = "INR"
        if isinstance(previous, dict) and isinstance(previous.get("currency"), str):
            currency = previous["currency"]
    return MoneyField(
        value=value,
        currency=currency,
        source=FieldSource.USER_CORRECTED,
        confidence=Confidence.HIGH,
        evidence=None,
    ).model_dump(mode="json")


def _sync_group_label(group: dict[str, Any], changes: dict[str, Any]) -> None:
    loans = group.get("loans")
    if not isinstance(loans, list) or len(loans) != 1:
        return
    loan = loans[0]
    if not isinstance(loan, dict):
        return
    fields = loan.get("fields")
    if not isinstance(fields, dict):
        return
    if "lender_brand" in changes:
        group["lender_brand"] = _text_or_none(fields.get("lender_brand"))
    if "legal_lending_entity" in changes:
        group["legal_lending_entity"] = _text_or_none(fields.get("legal_lending_entity"))


def _loan_lender_key(group: dict[str, Any], loan: dict[str, Any]) -> str:
    fields = loan.get("fields")
    brand = _text_or_none(fields.get("lender_brand")) if isinstance(fields, dict) else None
    entity = _text_or_none(fields.get("legal_lending_entity")) if isinstance(fields, dict) else None
    if brand is None and entity is None:
        group_brand = group.get("lender_brand")
        group_entity = group.get("legal_lending_entity")
        brand = group_brand if isinstance(group_brand, str) else None
        entity = group_entity if isinstance(group_entity, str) else None
    return lender_grouping_key(brand, entity)


def _scalar_value(field: Any) -> str | None:
    if not isinstance(field, dict):
        return None
    value = field.get("value")
    if isinstance(value, str):
        return value
    return None


def _text_or_none(field: Any) -> str | None:
    if not isinstance(field, dict):
        return None
    value = field.get("value")
    if isinstance(value, str):
        return value
    return None


def _require_complete_assignment(groups: list[Any], body: GroupingBody) -> None:
    proposed = _proposed_image_ids(groups)
    submitted: list[str] = []
    for item in body.loans:
        for image_id in item.image_ids:
            if image_id in submitted:
                raise AppError("INVALID_STATUS", _ASSIGNMENT_MESSAGE)
            submitted.append(image_id)
    if set(submitted) != set(proposed):
        raise AppError("INVALID_STATUS", _ASSIGNMENT_MESSAGE)


def _proposed_image_ids(groups: list[Any]) -> list[str]:
    image_ids: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        loans = group.get("loans")
        if not isinstance(loans, list):
            continue
        for loan in loans:
            if isinstance(loan, dict):
                for image_id in _image_ids(loan):
                    if image_id not in image_ids:
                        image_ids.append(image_id)
    return image_ids


def _image_ids(loan: dict[str, Any]) -> list[str]:
    raw = loan.get("image_ids")
    if not isinstance(raw, list):
        return []
    return [image_id for image_id in raw if isinstance(image_id, str) and image_id != ""]


def _locate_loan(
    groups: list[Any],
    loan_internal_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for group in groups:
        if not isinstance(group, dict):
            continue
        loans = group.get("loans")
        if not isinstance(loans, list):
            continue
        for loan in loans:
            if isinstance(loan, dict) and loan.get("loan_internal_id") == loan_internal_id:
                return group, loan
    return None


def _load_document(result_json: str | None) -> dict[str, Any]:
    if not result_json:
        return {}
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _attach_calculated(groups: list[dict[str, Any]]) -> None:
    for group in groups:
        loans = group.get("loans")
        if not isinstance(loans, list):
            continue
        for loan in loans:
            if not isinstance(loan, dict):
                continue
            fields = loan.get("fields")
            loan["calculated"] = _calculated_payload(fields if isinstance(fields, dict) else None)


def _calculated_payload(fields: dict[str, Any] | None) -> dict[str, Any]:
    try:
        calculated = calculate_fields(fields)
    except (TypeError, ValueError):
        calculated = calculate_fields(None)
    short = _sanctioned_short(fields)
    calculated["display_sanctioned_short"] = {
        "value": short,
        "source": "calculated" if short is not None else "unknown",
    }
    return calculated


def _sanctioned_short(fields: dict[str, Any] | None) -> str | None:
    if not isinstance(fields, dict):
        return None
    field = fields.get("sanctioned_amount")
    if not isinstance(field, dict):
        return None
    if field.get("source") not in {"extracted", "user_corrected"}:
        return None
    try:
        return display_sanctioned_short(field.get("value"))
    except (TypeError, ValueError):
        return None


def _readable_image_path(request: Request, storage_path: str) -> Path:
    tmp_dir = resolve_tmp_dir(request.app.state.settings.loanlens_tmp_dir)
    try:
        path = Path(storage_path).resolve()
    except OSError:
        raise AppError("IMAGE_UNREADABLE") from None
    if not path.is_file() or not path.is_relative_to(tmp_dir):
        raise AppError("IMAGE_UNREADABLE")
    return path


def _draft_ids_still_on_book(book: BookRow, draft: AnalysisRow) -> list[str]:
    """Loan ids from this draft that are still saved. Deleted loans stay deleted."""

    saved = set(_loan_internal_ids(_stored_groups(book.result_json)))
    return [loan_id for loan_id in _loan_internal_ids(_stored_groups(draft.result_json)) if loan_id in saved]


def _append_preview_loans(document: dict[str, Any], incoming: list[Any]) -> list[str]:
    """Add preview loans onto the book. An id already stored is not copied again."""

    groups = document.get("lender_groups")
    if not isinstance(groups, list):
        groups = []
        document["lender_groups"] = groups
    by_key: dict[str, dict[str, Any]] = {}
    for group in groups:
        if isinstance(group, dict):
            by_key[_group_key(group)] = group
    present = set(_loan_internal_ids(groups))
    added: list[str] = []
    for incoming_group in incoming:
        if not isinstance(incoming_group, dict):
            continue
        key = _group_key(incoming_group)
        target = by_key.get(key)
        if target is None:
            target = {
                "lender_group_id": incoming_group.get("lender_group_id"),
                "lender_brand": incoming_group.get("lender_brand"),
                "legal_lending_entity": incoming_group.get("legal_lending_entity"),
                "loans": [],
            }
            groups.append(target)
            by_key[key] = target
        loans = target.get("loans")
        if not isinstance(loans, list):
            loans = []
            target["loans"] = loans
        raw_loans = incoming_group.get("loans")
        if not isinstance(raw_loans, list):
            continue
        for loan in raw_loans:
            if not isinstance(loan, dict):
                continue
            loan_id = loan.get("loan_internal_id")
            if not isinstance(loan_id, str) or loan_id == "":
                continue
            added.append(loan_id)
            if loan_id in present:
                continue
            loans.append(copy.deepcopy(loan))
            present.add(loan_id)
    return added


def _move_loan_images(
    session: Session,
    draft: AnalysisRow,
    book_id: str,
    groups: list[Any],
    tmp_dir: Path,
) -> None:
    """Move each preview screenshot under the book loan that owns it."""

    for group in groups:
        if not isinstance(group, dict):
            continue
        loans = group.get("loans")
        if not isinstance(loans, list):
            continue
        for loan in loans:
            if not isinstance(loan, dict):
                continue
            loan_id = loan.get("loan_internal_id")
            if not isinstance(loan_id, str) or loan_id == "":
                continue
            for image_id in _image_ids(loan):
                image = session.get(ImageRow, image_id)
                if image is None or image.analysis_id != draft.id:
                    raise AppError("IMAGE_UNREADABLE")
                image.storage_path = _relocate_image(tmp_dir, book_id, loan_id, image)


def _relocate_image(tmp_dir: Path, book_id: str, loan_id: str, image: ImageRow) -> str:
    destination = book_image_path(tmp_dir, book_id, loan_id, image.id)
    try:
        source = Path(image.storage_path).resolve()
        already_there = source == destination.resolve()
    except OSError:
        already_there = False
        source = Path(image.storage_path)
    if not already_there:
        if not source.is_file():
            raise AppError("IMAGE_UNREADABLE")
        shutil.move(str(source), str(destination))
        _remove_empty_parents(source.parent, tmp_dir.resolve())
    return str(destination)


def _remove_empty_parents(directory: Path, root: Path) -> None:
    current = directory
    while current != root and current.is_relative_to(root):
        if not current.is_dir() or any(current.iterdir()):
            return
        current.rmdir()
        current = current.parent


def _stored_groups(result_json: str | None) -> list[Any]:
    groups = _load_document(result_json).get("lender_groups")
    if isinstance(groups, list):
        return groups
    return []


def _loan_internal_ids(groups: list[Any]) -> list[str]:
    loan_ids: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        loans = group.get("loans")
        if not isinstance(loans, list):
            continue
        for loan in loans:
            if not isinstance(loan, dict):
                continue
            loan_id = loan.get("loan_internal_id")
            if isinstance(loan_id, str) and loan_id != "" and loan_id not in loan_ids:
                loan_ids.append(loan_id)
    return loan_ids


def _group_key(group: dict[str, Any]) -> str:
    brand = group.get("lender_brand")
    entity = group.get("legal_lending_entity")
    return lender_grouping_key(
        brand if isinstance(brand, str) else None,
        entity if isinstance(entity, str) else None,
    )
