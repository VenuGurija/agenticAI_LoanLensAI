"""Review flags and the terminal analysis status (SPEC §12, §18).

Flags are recorded on each loan. Field values are left as they were.
``completed`` is used only when the draft has at least one loan and none of
those loans need review.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from app.models.enums import AnalysisStatus
from app.services.summary import AMBIGUOUS_DATE, display_date_short, display_sanctioned_short, resolve_date
from app.services.vision.contracts import DocumentClass

LOW_CONFIDENCE_FIELD = "LOW_CONFIDENCE_FIELD"
FIELD_CONFLICT = "FIELD_CONFLICT"
GROUPING_CONFLICT = "GROUPING_CONFLICT"
CURRENCY_UNKNOWN = "CURRENCY_UNKNOWN"
NOT_LOAN_DOCUMENT = "NOT_LOAN_DOCUMENT"
UNCLEAR_DOCUMENT = "UNCLEAR_DOCUMENT"
NO_LENDER = "NO_LENDER"
EXTRACTION_INVALID = "EXTRACTION_INVALID"
VALIDATION_FAILED_PREFIX = "VALIDATION_FAILED:"

CORE_DISPLAY_FIELDS = (
    "sanctioned_amount",
    "disbursed_amount",
    "repayment_amount",
    "disbursement_date",
    "repayment_date",
    "lender_brand",
)

_DATE_FIELDS = ("sanction_date", "disbursement_date", "repayment_date")
_MONEY_FIELDS = (
    "sanctioned_amount",
    "disbursed_amount",
    "processing_fee",
    "gst_amount",
    "other_fees_total",
    "repayment_amount",
)
_USABLE_SOURCES = frozenset({"extracted", "user_corrected"})
_LOW_CONFIDENCE = frozenset({"low", "unknown"})
_PRESERVED = frozenset({FIELD_CONFLICT, GROUPING_CONFLICT, CURRENCY_UNKNOWN})
_FAILED_SUMMARY = frozenset({"failed", "incomplete"})


@dataclass(frozen=True)
class ReviewResult:
    """Status written on the draft, plus image flags that are not a loan field."""

    status: str
    image_flags: list[str]


def apply_review(
    lender_groups: list[dict[str, Any]],
    *,
    images: list[Mapping[str, Any]] | None = None,
    created_at: date | datetime | str | None = None,
) -> ReviewResult:
    """Set ``review_flags`` and ``needs_review`` on every loan.

    ``images`` carries each screenshot's ``document_class`` and, when vision
    JSON failed for that image, ``extraction_invalid``. A partial date is
    flagged only when the year cannot be inferred from ``created_at``.
    """

    listed_images = [image for image in images or [] if isinstance(image, Mapping)]
    loans = _loans(lender_groups)
    image_flags = _image_flags(listed_images, loans)
    for loan in loans:
        loan["review_flags"] = _loan_flags(loan, image_flags=image_flags, created_at=created_at)
        loan["needs_review"] = _needs_review(loan)
    return ReviewResult(
        status=_terminal_status(loans, listed_images),
        image_flags=image_flags,
    )


def _terminal_status(loans: list[dict[str, Any]], images: list[Mapping[str, Any]]) -> str:
    if any(loan.get("needs_review") for loan in loans):
        return AnalysisStatus.COMPLETED_NEEDS_REVIEW.value
    if not loans and _has_class(images, DocumentClass.UNCLEAR.value):
        return AnalysisStatus.COMPLETED_NEEDS_REVIEW.value
    if loans and all(loan.get("needs_review") is False for loan in loans):
        return AnalysisStatus.COMPLETED.value
    return AnalysisStatus.COMPLETED_NEEDS_REVIEW.value


def _loan_flags(
    loan: Mapping[str, Any],
    *,
    image_flags: list[str],
    created_at: date | datetime | str | None,
) -> list[str]:
    existing = loan.get("review_flags")
    flags: list[str] = []
    if _low_confidence_display_field(loan):
        flags.append(LOW_CONFIDENCE_FIELD)
    if _preserved(existing, FIELD_CONFLICT):
        flags.append(FIELD_CONFLICT)
    if _preserved(existing, GROUPING_CONFLICT):
        flags.append(GROUPING_CONFLICT)
    if _ambiguous_date(loan, created_at):
        flags.append(AMBIGUOUS_DATE)
    if _preserved(existing, CURRENCY_UNKNOWN) or _amount_missing_currency(loan):
        flags.append(CURRENCY_UNKNOWN)
    for code in (NOT_LOAN_DOCUMENT, UNCLEAR_DOCUMENT, EXTRACTION_INVALID):
        if code in image_flags:
            flags.append(code)
    flags.extend(_validation_failed(loan, existing))
    if _no_lender(loan):
        flags.append(NO_LENDER)
    return flags


def _needs_review(loan: Mapping[str, Any]) -> bool:
    flags = loan.get("review_flags")
    if isinstance(flags, list) and flags:
        return True
    if any(_core_is_null(loan, name) for name in CORE_DISPLAY_FIELDS):
        return True
    return loan.get("validation_summary") in _FAILED_SUMMARY


def _image_flags(images: list[Mapping[str, Any]], loans: list[dict[str, Any]]) -> list[str]:
    flags: list[str] = []
    if _has_class(images, DocumentClass.NOT_LOAN_DOCUMENT.value):
        flags.append(NOT_LOAN_DOCUMENT)
    if _has_class(images, DocumentClass.UNCLEAR.value):
        flags.append(UNCLEAR_DOCUMENT)
    owned = _loan_image_ids(loans)
    invalid = any(
        image.get("extraction_invalid") is True and image.get("image_id") not in owned
        for image in images
    )
    if invalid:
        flags.append(EXTRACTION_INVALID)
    return flags


def _low_confidence_display_field(loan: Mapping[str, Any]) -> bool:
    for name in CORE_DISPLAY_FIELDS:
        if not _shown_in_summary(loan, name):
            continue
        field = _field(loan, name)
        if field is not None and _confidence_is_low(field):
            return True
    return False


def _shown_in_summary(loan: Mapping[str, Any], name: str) -> bool:
    if name == "lender_brand":
        return _text_present(_field(loan, name))
    if name in {"sanctioned_amount", "disbursed_amount", "repayment_amount"}:
        return _amount_is_shown(loan, name)
    if name == "disbursement_date":
        return _amount_is_shown(loan, "disbursed_amount") and _date_is_shown(loan, name)
    if name == "repayment_date":
        return _amount_is_shown(loan, "repayment_amount") and _date_is_shown(loan, name)
    return False


def _ambiguous_date(loan: Mapping[str, Any], created_at: date | datetime | str | None) -> bool:
    for name in _DATE_FIELDS:
        field = _field(loan, name)
        if field is None:
            continue
        if field.get("review_flag") == AMBIGUOUS_DATE:
            return True
        value = field.get("value")
        if value is None:
            continue
        try:
            resolved = resolve_date(value, created_at=created_at)
        except (TypeError, ValueError):
            return True
        if resolved.review_flag == AMBIGUOUS_DATE:
            return True
    return False


def _amount_missing_currency(loan: Mapping[str, Any]) -> bool:
    fields = _fields(loan)
    for name in _MONEY_FIELDS:
        if _money_missing_currency(fields.get(name)):
            return True
    penalties = fields.get("penalties")
    if not isinstance(penalties, list):
        return False
    return any(
        isinstance(penalty, Mapping) and _money_missing_currency(penalty.get("amount"))
        for penalty in penalties
    )


def _money_missing_currency(field: Any) -> bool:
    payload = _mapping(field)
    if payload is None or not _value_present(payload.get("value")):
        return False
    currency = payload.get("currency")
    return not isinstance(currency, str) or currency.strip() == ""


def _no_lender(loan: Mapping[str, Any]) -> bool:
    return not _text_present(_field(loan, "lender_brand")) and not _text_present(
        _field(loan, "legal_lending_entity")
    )


def _validation_failed(loan: Mapping[str, Any], existing: Any) -> list[str]:
    validations = loan.get("validations")
    if isinstance(validations, list):
        flags: list[str] = []
        for item in validations:
            payload = _mapping(item)
            if payload is None or payload.get("status") != "failed":
                continue
            check_id = payload.get("id")
            if isinstance(check_id, str) and check_id:
                flags.append(f"{VALIDATION_FAILED_PREFIX}{check_id}")
        return _dedupe(flags)
    if not isinstance(existing, list):
        return []
    return [
        flag
        for flag in existing
        if isinstance(flag, str) and flag.startswith(VALIDATION_FAILED_PREFIX)
    ]


def _core_is_null(loan: Mapping[str, Any], name: str) -> bool:
    field = _field(loan, name)
    if field is None:
        return True
    return not _value_present(field.get("value"))


def _amount_is_shown(loan: Mapping[str, Any], name: str) -> bool:
    field = _field(loan, name)
    if not _usable(field):
        return False
    return display_sanctioned_short(field.get("value")) is not None


def _date_is_shown(loan: Mapping[str, Any], name: str) -> bool:
    field = _field(loan, name)
    if not _usable(field):
        return False
    value = field.get("value")
    if not isinstance(value, str):
        return False
    try:
        return display_date_short(value) is not None
    except (TypeError, ValueError):
        return False


def _confidence_is_low(field: Mapping[str, Any]) -> bool:
    confidence = field.get("confidence")
    if confidence is None:
        return True
    return isinstance(confidence, str) and confidence in _LOW_CONFIDENCE


def _usable(field: Mapping[str, Any] | None) -> bool:
    if field is None or not _value_present(field.get("value")):
        return False
    source = field.get("source")
    if source is None:
        return True
    return source in _USABLE_SOURCES


def _text_present(field: Mapping[str, Any] | None) -> bool:
    if field is None:
        return False
    value = field.get("value")
    return isinstance(value, str) and value.strip() != ""


def _value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _preserved(existing: Any, code: str) -> bool:
    return isinstance(existing, list) and code in existing and code in _PRESERVED


def _has_class(images: list[Mapping[str, Any]], document_class: str) -> bool:
    return any(str(image.get("document_class")) == document_class for image in images)


def _loan_image_ids(loans: list[dict[str, Any]]) -> set[str]:
    image_ids: set[str] = set()
    for loan in loans:
        raw = loan.get("image_ids")
        if not isinstance(raw, list):
            continue
        for image_id in raw:
            if isinstance(image_id, str):
                image_ids.add(image_id)
    return image_ids


def _loans(lender_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    loans: list[dict[str, Any]] = []
    for group in lender_groups:
        if not isinstance(group, dict):
            continue
        grouped = group.get("loans")
        if not isinstance(grouped, list):
            continue
        for loan in grouped:
            if isinstance(loan, dict):
                loans.append(loan)
    return loans


def _field(loan: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    return _mapping(_fields(loan).get(name))


def _fields(loan: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = _mapping(loan.get("fields"))
    if fields is None:
        return {}
    return fields


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _dedupe(flags: list[str]) -> list[str]:
    unique: list[str] = []
    for flag in flags:
        if flag not in unique:
            unique.append(flag)
    return unique
