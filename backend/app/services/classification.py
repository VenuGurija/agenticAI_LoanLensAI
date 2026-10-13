"""Classify draft images, extract loan documents, group them, and validate.

The HTTP analyze request does not call this module. The in-process worker
does. Each vision call sends one image, and the database session is closed
before the call. The checkpoint is committed after the call. A later image
failure keeps earlier pages. The next run resumes at the first image that
still lacks a classify or extract checkpoint. This does not copy loans into
the book.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Literal, NoReturn

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import ERROR_SPECS, AppError, vision_invalid_message
from app.core.logging import (
    image_log_markers,
    log_event,
    log_vision_response_rejected,
    model_answer_text,
    validation_failure_items,
    vision_log_environment,
    vision_log_secrets,
)
from app.models.database import AnalysisRow, ImageRow, run_in_session
from app.models.enums import AnalysisStatus, ImageStatus
from app.persistence import analyses as store
from app.schemas.extraction import ImageExtractResult
from app.schemas.vision import VisionImage, VisionRequest
from app.services.extraction import (
    extract_image,
    extracted_image_ids,
    lender_groups_from_extracts,
    raw_extract_items,
    with_extract_checkpoint,
)
from app.services.images.preprocess import preprocess_image
from app.services.images.upload import resolve_tmp_dir
from app.services.review import apply_review
from app.services.validation.rules import attach_loan_validations
from app.services.vision.base import VisionProvider, VisionResponseInvalidError
from app.services.vision.contracts import (
    CLASSIFY_SCHEMA_NAME,
    ClassifyContract,
    DocumentClass,
    ImageClassification,
    validate_contract,
)
from app.services.vision.fake import UnknownVisionFixtureError

_VISION_FAILURE_CODES = frozenset(
    {"VISION_UNAVAILABLE", "VISION_TIMEOUT", "VISION_INVALID_RESPONSE"}
)
_IMAGE_FAILURE_CODES = frozenset(
    {
        "IMAGE_TOO_SMALL",
        "IMAGE_TOO_LARGE",
        "IMAGE_UNREADABLE",
        "IMAGE_UNSUPPORTED_TYPE",
        "NO_IMAGES",
    }
)
_CLASSIFY_MISMATCH = "Vision response did not match the classify contract."
_START_STATUSES = frozenset(
    {
        AnalysisStatus.CREATED.value,
        AnalysisStatus.IMAGES_PENDING.value,
        AnalysisStatus.FAILED.value,
        AnalysisStatus.RUNNING.value,
    }
)
_STORED_STATUSES = frozenset(
    {
        AnalysisStatus.COMPLETED.value,
        AnalysisStatus.COMPLETED_NEEDS_REVIEW.value,
        AnalysisStatus.ACCEPTED.value,
    }
)

Outcome = Literal["no_loan_documents", "unclear_only", "extract"]
VisionKind = Literal["classify", "extract"]
_CLASSIFICATIONS = "classifications"


@dataclass(frozen=True)
class PreparedDraft:
    """Plain image bytes for vision. Not attached to a database session."""

    analysis_id: str
    created_at: str
    pages: list[VisionImage]


@dataclass(frozen=True)
class ClassifiedPage:
    """One classify result plus the bytes needed for a later extract call."""

    image_id: str
    document_class: DocumentClass
    vision: VisionImage


def classification_outcome(classes: list[DocumentClass]) -> Outcome:
    """SPEC §8.2. Unclear with no loan document does not call extract."""

    if classes and all(item == DocumentClass.NOT_LOAN_DOCUMENT for item in classes):
        return "no_loan_documents"
    if all(item != DocumentClass.LOAN_DOCUMENT for item in classes):
        return "unclear_only"
    return "extract"


def parse_classification(
    raw: Any,
    image_id: str,
    *,
    analysis_id: str | None = None,
    attempt: int | None = None,
    environment: str | None = None,
    image_bytes: Sequence[bytes] = (),
) -> ImageClassification:
    """Validate one image's classify payload."""

    def reject(failures: list[dict[str, str]], exc: ValidationError | None = None) -> NoReturn:
        log_vision_response_rejected(
            analysis_id=analysis_id,
            task="classify",
            attempt=attempt,
            schema_name=CLASSIFY_SCHEMA_NAME,
            failures=failures,
            model_answer=model_answer_text(raw),
            environment=vision_log_environment(environment),
            secrets=vision_log_secrets(),
            image_markers=image_log_markers(image_bytes),
        )
        if exc is None:
            raise VisionResponseInvalidError(_CLASSIFY_MISMATCH)
        raise VisionResponseInvalidError(_CLASSIFY_MISMATCH) from exc

    if not isinstance(raw, dict):
        reject([{"loc": "", "type": "dict_type"}])
    try:
        if "results" in raw:
            parsed = validate_contract(CLASSIFY_SCHEMA_NAME, raw)
            if not isinstance(parsed, ClassifyContract):
                reject([{"loc": "", "type": "dict_type"}])
            if len(parsed.results) != 1:
                reject([{"loc": "results", "type": "list_length"}])
            return parsed.results[0].model_copy(update={"image_id": image_id})
        if "document_class" in raw:
            return ImageClassification.model_validate({**raw, "image_id": image_id})
    except ValidationError as exc:
        reject(validation_failure_items(exc), exc)
    reject([{"loc": "", "type": "missing"}])


async def classify_image(
    provider: VisionProvider,
    *,
    analysis_id: str,
    image: VisionImage,
) -> ImageClassification:
    """Classify this one image.

    Invalid JSON is retried once inside the provider for this call. One image
    per call keeps that retry on this image.
    """

    response = await provider.analyze_images(
        VisionRequest(
            analysis_id=analysis_id,
            task="classify",
            images=[image],
            response_schema_name=CLASSIFY_SCHEMA_NAME,
            hint=None,
        )
    )
    return parse_classification(
        response.raw_json,
        image.image_id,
        analysis_id=analysis_id,
        image_bytes=(image.bytes,),
    )


async def run_analysis(
    session_factory: sessionmaker[Session],
    analysis_id: str,
    *,
    settings: Settings,
    provider: VisionProvider,
) -> None:
    """Run preprocess, then one classify or extract call per unfinished image.

    The database session is closed before each vision call and the checkpoint
    is committed after it. A stored terminal preview is left unchanged. This
    does not copy loans into the book. The HTTP request must not call this.
    """

    try:
        prepared = run_in_session(
            session_factory,
            lambda session: _prepare_draft(session, analysis_id, settings),
        )
    except AppError as exc:
        if exc.code in _IMAGE_FAILURE_CODES:
            _record_image_failure(session_factory, analysis_id, exc.code, provider)
            return
        raise
    if prepared is None:
        return

    try:
        await _checkpoint_pages(session_factory, provider, prepared)
    except Exception as exc:
        _record_vision_failure(session_factory, analysis_id, exc, provider)
        return

    run_in_session(
        session_factory,
        lambda session: _finish_from_checkpoints(session, prepared),
    )
    _log_finished(session_factory, analysis_id, provider)


def _prepare_draft(session: Session, analysis_id: str, settings: Settings) -> PreparedDraft | None:
    """Preprocess images. Checkpoints already stored stay. Caller closes the session."""

    analysis = _require_analysis(session, analysis_id)
    if _use_stored_result(analysis):
        return None
    if analysis.status not in _START_STATUSES:
        raise AppError("INVALID_STATUS")

    images = store.list_images(session, analysis.id)
    if not images:
        raise AppError("NO_IMAGES")

    analysis.error_code = None
    analysis.error_message = None
    analysis.completed_at = None
    analysis.status = AnalysisStatus.PREPROCESSING.value
    tmp_dir = resolve_tmp_dir(settings.loanlens_tmp_dir)
    pages = [_preprocess_row(session, row, analysis.id, tmp_dir, settings) for row in images]
    document = _checkpoint_document(analysis.result_json)
    _restore_classified_rows(session, analysis.id, document)
    prepared = PreparedDraft(analysis_id=analysis.id, created_at=analysis.created_at, pages=pages)
    _write_progress(analysis, prepared, document)
    session.flush()
    return prepared


async def _checkpoint_pages(
    session_factory: sessionmaker[Session],
    provider: VisionProvider,
    prepared: PreparedDraft,
) -> None:
    """Classify and extract one image at a time, committing after each call."""

    while True:
        step = run_in_session(
            session_factory,
            lambda session: _claim_vision_step(session, prepared),
        )
        if step is None:
            return
        image_id, kind = step
        image = _vision_image(prepared, image_id)
        if kind == "classify":
            result = await classify_image(
                provider,
                analysis_id=prepared.analysis_id,
                image=image,
            )
            run_in_session(
                session_factory,
                lambda session, result=result: _store_classification(session, prepared, result),
            )
            continue
        extracted = await extract_image(
            provider,
            analysis_id=prepared.analysis_id,
            image=image,
        )
        run_in_session(
            session_factory,
            lambda session, extracted=extracted: _store_extract(session, prepared, extracted),
        )


def _claim_vision_step(
    session: Session,
    prepared: PreparedDraft,
) -> tuple[str, VisionKind] | None:
    """Mark the next unfinished image and return. Caller closes the session."""

    analysis = _require_analysis(session, prepared.analysis_id)
    document = _checkpoint_document(analysis.result_json)
    step = _next_vision_step(document, prepared.pages)
    if step is None:
        return None
    image_id, kind = step
    analysis.status = (
        AnalysisStatus.CLASSIFYING.value
        if kind == "classify"
        else AnalysisStatus.EXTRACTING.value
    )
    session.flush()
    return image_id, kind


def _next_vision_step(
    document: dict[str, Any],
    pages: list[VisionImage],
) -> tuple[str, VisionKind] | None:
    """First image that still needs a classify call or a loan-document extract."""

    classes = _class_by_id(document)
    extracted = extracted_image_ids(document)
    for page in pages:
        doc_class = classes.get(page.image_id)
        if doc_class is None:
            return page.image_id, "classify"
        if doc_class == DocumentClass.LOAN_DOCUMENT and page.image_id not in extracted:
            return page.image_id, "extract"
    return None


def _store_classification(
    session: Session,
    prepared: PreparedDraft,
    result: ImageClassification,
) -> None:
    analysis = _require_analysis(session, prepared.analysis_id)
    document = _checkpoint_document(analysis.result_json)
    items = [
        item
        for item in _classification_items(document)
        if item.get("image_id") != result.image_id
    ]
    items.append(result.model_dump(mode="json"))
    document[_CLASSIFICATIONS] = items
    document.setdefault("lender_groups", [])
    analysis.result_json = json.dumps(document)
    _mark_image_classified(session, prepared.analysis_id, result.image_id, result.document_class)
    _write_progress(analysis, prepared, document)
    session.flush()


def _store_extract(
    session: Session,
    prepared: PreparedDraft,
    extracted: ImageExtractResult,
) -> None:
    analysis = _require_analysis(session, prepared.analysis_id)
    document = with_extract_checkpoint(_checkpoint_document(analysis.result_json), extracted)
    analysis.result_json = json.dumps(document)
    _write_progress(analysis, prepared, document)
    session.flush()


def _finish_from_checkpoints(session: Session, prepared: PreparedDraft) -> None:
    """Group and validate after every image has a checkpoint. No vision call."""

    analysis = _require_analysis(session, prepared.analysis_id)
    document = _checkpoint_document(analysis.result_json)
    classified = _classified_pages(document, prepared.pages)
    outcome = classification_outcome([page.document_class for page in classified])
    if outcome == "no_loan_documents":
        _finish_failed(analysis, "NO_LOAN_DOCUMENTS")
        _mark_all_pages_done(analysis, prepared)
        session.flush()
        return
    if outcome == "unclear_only":
        reviewed = apply_review([], images=_classified_review_images(classified))
        _finish_terminal(analysis, reviewed.status, raw_extracts=None)
        _mark_all_pages_done(analysis, prepared)
        session.flush()
        return
    _finish_extracted(session, prepared, classified, _ordered_extracts(document, classified))
    _mark_all_pages_done(analysis, prepared)
    session.flush()


def _finish_extracted(
    session: Session,
    prepared: PreparedDraft,
    classified: list[ClassifiedPage],
    raw_extracts: list[dict[str, Any]],
) -> None:
    analysis = _require_analysis(session, prepared.analysis_id)
    analysis.status = AnalysisStatus.GROUPING.value
    lender_groups = lender_groups_from_extracts(raw_extracts)
    analysis.status = AnalysisStatus.VALIDATING.value
    attach_loan_validations(lender_groups)
    reviewed = apply_review(
        lender_groups,
        images=_classified_review_images(classified),
        created_at=prepared.created_at,
    )
    _finish_terminal(
        analysis,
        reviewed.status,
        raw_extracts=raw_extracts,
        lender_groups=lender_groups,
    )
    session.flush()


def _record_image_failure(
    session_factory: sessionmaker[Session],
    analysis_id: str,
    code: str,
    provider: VisionProvider,
) -> None:
    """Fail the draft when preprocess cannot read an image. The draft row stays."""

    def fail(session: Session) -> None:
        analysis = _require_analysis(session, analysis_id)
        _finish_failed(analysis, code)
        for row in store.list_images(session, analysis.id):
            if row.status == ImageStatus.UPLOADED.value:
                row.status = ImageStatus.FAILED.value

    run_in_session(session_factory, fail)
    _log_finished(session_factory, analysis_id, provider)


def _record_vision_failure(
    session_factory: sessionmaker[Session],
    analysis_id: str,
    exc: Exception,
    provider: VisionProvider,
) -> None:
    code = _vision_failure_code(exc)
    if code is None:
        raise exc

    failures = getattr(exc, "failures", None)
    stored = failures if isinstance(failures, list) else None

    def fail(session: Session) -> None:
        _finish_failed(_require_analysis(session, analysis_id), code, failures=stored)

    run_in_session(session_factory, fail)
    _log_finished(session_factory, analysis_id, provider)


def _log_finished(
    session_factory: sessionmaker[Session],
    analysis_id: str,
    provider: VisionProvider,
) -> None:
    def read(session: Session) -> tuple[str, str | None] | None:
        analysis = store.get_analysis(session, analysis_id)
        if analysis is None:
            return None
        return analysis.status, analysis.error_code

    loaded = run_in_session(session_factory, read)
    if loaded is None:
        return
    status, error_code = loaded
    log_event(
        "analysis_finished",
        analysis_id=analysis_id,
        status=status,
        error_code=error_code,
        provider=_provider_label(provider, "provider_name"),
        model=_provider_label(provider, "model_name"),
    )


def _require_analysis(session: Session, analysis_id: str) -> AnalysisRow:
    analysis = store.get_analysis(session, analysis_id)
    if analysis is None:
        raise AppError("ANALYSIS_NOT_FOUND")
    return analysis


def _use_stored_result(analysis: AnalysisRow) -> bool:
    if analysis.status in _STORED_STATUSES:
        return True
    return analysis.status == AnalysisStatus.FAILED.value and not store.failure_is_retryable(analysis)


def _preprocess_row(
    session: Session,
    row: ImageRow,
    analysis_id: str,
    tmp_dir: Path,
    settings: Settings,
) -> VisionImage:
    stored = Path(row.storage_path)
    if not stored.is_file():
        raise AppError("IMAGE_UNREADABLE")
    prepared = preprocess_image(
        stored.read_bytes(),
        tmp_dir=tmp_dir,
        analysis_id=analysis_id,
        image_id=row.id,
        content_type=row.content_type,
        max_bytes=settings.loanlens_max_image_bytes,
    )
    store.record_preprocessed_image(
        session,
        row,
        checksum_sha256=prepared.checksum_sha256,
        width=prepared.width,
        height=prepared.height,
        storage_path=prepared.storage_path,
        content_type=prepared.content_type,
        byte_size=prepared.byte_size,
    )
    return VisionImage(
        image_id=row.id,
        media_type=prepared.content_type,
        bytes=Path(prepared.storage_path).read_bytes(),
    )


def _classified_review_images(classified: list[ClassifiedPage]) -> list[dict[str, Any]]:
    return [
        {"image_id": page.image_id, "document_class": page.document_class.value}
        for page in classified
    ]


def _finish_terminal(
    analysis: AnalysisRow,
    status: str,
    raw_extracts: list[dict[str, Any]] | None,
    lender_groups: list[dict[str, Any]] | None = None,
) -> None:
    document = _checkpoint_document(analysis.result_json)
    document["lender_groups"] = [] if lender_groups is None else lender_groups
    if raw_extracts is not None:
        document["raw_extracts"] = raw_extracts
    analysis.result_json = json.dumps(document)
    analysis.status = status
    analysis.error_code = None
    analysis.error_message = None
    analysis.completed_at = store.utc_timestamp()


def _finish_failed(
    analysis: AnalysisRow,
    code: str,
    failures: list[dict[str, str]] | None = None,
) -> None:
    """Mark the draft failed and keep classify/extract checkpoints already stored."""

    spec = ERROR_SPECS[code]
    document = _checkpoint_document(analysis.result_json)
    document.setdefault("lender_groups", [])
    analysis.result_json = json.dumps(document)
    analysis.status = AnalysisStatus.FAILED.value
    analysis.error_code = code
    if code == "VISION_INVALID_RESPONSE":
        analysis.error_message = vision_invalid_message(failures)
    else:
        analysis.error_message = spec.message
    analysis.completed_at = store.utc_timestamp()


def _checkpoint_document(result_json: str | None) -> dict[str, Any]:
    if not result_json:
        return {}
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _classification_items(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw = document.get(_CLASSIFICATIONS)
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("image_id"), str):
            items.append(item)
    return items


def _class_by_id(document: dict[str, Any]) -> dict[str, DocumentClass]:
    found: dict[str, DocumentClass] = {}
    for item in _classification_items(document):
        try:
            found[item["image_id"]] = DocumentClass(item["document_class"])
        except (KeyError, ValueError):
            continue
    return found


def _completed_count(document: dict[str, Any], pages: list[VisionImage]) -> int:
    classes = _class_by_id(document)
    extracted = extracted_image_ids(document)
    done = 0
    for page in pages:
        doc_class = classes.get(page.image_id)
        if doc_class is None:
            continue
        if doc_class == DocumentClass.LOAN_DOCUMENT:
            if page.image_id in extracted:
                done += 1
            continue
        done += 1
    return done


def _write_progress(
    analysis: AnalysisRow,
    prepared: PreparedDraft,
    document: dict[str, Any],
) -> None:
    analysis.pages_total = len(prepared.pages)
    analysis.pages_done = _completed_count(document, prepared.pages)


def _mark_all_pages_done(analysis: AnalysisRow, prepared: PreparedDraft) -> None:
    analysis.pages_done = len(prepared.pages)
    analysis.pages_total = len(prepared.pages)


def _vision_image(prepared: PreparedDraft, image_id: str) -> VisionImage:
    for page in prepared.pages:
        if page.image_id == image_id:
            return page
    raise AppError("IMAGE_UNREADABLE")


def _mark_image_classified(
    session: Session,
    analysis_id: str,
    image_id: str,
    document_class: DocumentClass,
) -> None:
    row = session.get(ImageRow, image_id)
    if row is None or row.analysis_id != analysis_id:
        raise AppError("IMAGE_UNREADABLE")
    row.document_class = document_class.value
    row.status = ImageStatus.CLASSIFIED.value
    row.error_code = None


def _restore_classified_rows(session: Session, analysis_id: str, document: dict[str, Any]) -> None:
    """Put checkpointed classes back on image rows after preprocess."""

    for image_id, doc_class in _class_by_id(document).items():
        row = session.get(ImageRow, image_id)
        if row is None or row.analysis_id != analysis_id:
            continue
        row.document_class = doc_class.value
        row.status = ImageStatus.CLASSIFIED.value
        row.error_code = None


def _classified_pages(document: dict[str, Any], pages: list[VisionImage]) -> list[ClassifiedPage]:
    classes = _class_by_id(document)
    classified: list[ClassifiedPage] = []
    for page in pages:
        doc_class = classes.get(page.image_id)
        if doc_class is None:
            raise AppError("IMAGE_UNREADABLE")
        classified.append(
            ClassifiedPage(
                image_id=page.image_id,
                document_class=doc_class,
                vision=page,
            )
        )
    return classified


def _ordered_extracts(
    document: dict[str, Any],
    classified: list[ClassifiedPage],
) -> list[dict[str, Any]]:
    """Loan-document extracts in page order. A missing page is not filled in."""

    by_id = {item["image_id"]: item for item in raw_extract_items(document)}
    ordered: list[dict[str, Any]] = []
    for page in classified:
        if page.document_class != DocumentClass.LOAN_DOCUMENT:
            continue
        item = by_id.get(page.image_id)
        if item is None:
            raise AppError("IMAGE_UNREADABLE")
        ordered.append(item)
    return ordered


def _vision_failure_code(exc: Exception) -> str | None:
    if isinstance(exc, (VisionResponseInvalidError, UnknownVisionFixtureError)):
        return "VISION_INVALID_RESPONSE"
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _VISION_FAILURE_CODES:
        return code
    return None


def _provider_label(provider: VisionProvider, name: str) -> str | None:
    value = getattr(provider, name, None)
    if isinstance(value, str):
        return value
    return None
