"""Parse a vision extract response and merge pages into the draft preview.

Parsing does not calculate fees. Missing money stays null. Grouping turns
per-image extracts into ``lender_groups`` for the analysis GET body.
"""

import copy
import re
from collections.abc import Sequence
from typing import Any, NoReturn

from pydantic import ValidationError

from app.core.logging import (
    image_log_markers,
    log_vision_response_rejected,
    model_answer_text,
    validation_failure_items,
    vision_log_environment,
    vision_log_secrets,
)
from app.schemas.extraction import ImageExtractResult, MoneyField, RateField
from app.schemas.vision import VisionImage, VisionRequest
from app.services.grouping.group import apply_image_assignment, group_raw_extracts
from app.services.summary import resolve_date
from app.services.vision.base import VisionProvider, VisionResponseInvalidError
from app.services.vision.contracts import EXTRACT_SCHEMA_NAME, ExtractContract, validate_contract

_EXTRACT_MISMATCH = "Vision response did not match the extract contract."
_DATE_FIELDS = ("sanction_date", "disbursement_date", "repayment_date")
_STRING_FIELDS = ("lender_brand", "legal_lending_entity", "borrower_name", "loan_id")
_MONEY_FIELDS = (
    "sanctioned_amount",
    "disbursed_amount",
    "processing_fee",
    "gst_amount",
    "other_fees_total",
    "repayment_amount",
)
_RATE_FIELDS = ("interest_rate", "apr")
_TENURE_TEXT = re.compile(r"^(\d+)\s+(day|days|month|months)$", re.IGNORECASE)
_RATE_PERCENT = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
_PLAIN_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_FREQUENCY_VALUES = frozenset({"one_time", "weekly", "biweekly", "monthly", "unknown"})
_FREQUENCY_ALIASES = {
    "bullet": "one_time",
    "bullet repayment": "one_time",
    "single": "one_time",
    "single instalment": "one_time",
    "single installment": "one_time",
    "one instalment": "one_time",
    "one installment": "one_time",
    "one time": "one_time",
    "onetime": "one_time",
    "once": "one_time",
}
_PLACEHOLDER_TOKENS = frozenset({"", "null", "none"})
_UNUSABLE_NAME = frozenset({"", "null", "none", "na", "n/a", "n.a", "n.a."})


def normalize_printed_extract(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn printed extract shape into the vision contract. Do not invent amounts."""

    cloned = copy.deepcopy(payload)
    _normalize_extract_node(cloned)
    return cloned


def _normalize_extract_node(node: dict[str, Any]) -> None:
    loans = node.get("loans")
    if isinstance(loans, list):
        for loan in loans:
            if isinstance(loan, dict):
                _normalize_loan(loan)
    results = node.get("results")
    if isinstance(results, list):
        kept: list[Any] = []
        for item in results:
            if isinstance(item, dict):
                _normalize_extract_node(item)
                if _is_empty_result(item):
                    continue
            kept.append(item)
        node["results"] = kept


def _is_empty_result(item: dict[str, Any]) -> bool:
    """An extra result with no image id and no loans is not a loan page."""

    image_id = item.get("image_id")
    has_image_id = isinstance(image_id, str) and image_id != ""
    loans = item.get("loans") if "loans" in item else None
    loans_empty = "loans" not in item or loans is None or loans == []
    return not has_image_id and loans_empty


def _normalize_loan(loan: dict[str, Any]) -> None:
    for name in _STRING_FIELDS:
        if name in loan:
            loan[name] = _string_field(loan[name])
    for name in _MONEY_FIELDS:
        if name in loan:
            loan[name] = _money_field(loan[name])
    for name in _RATE_FIELDS:
        if name in loan:
            loan[name] = _rate_field(loan[name])
    if "tenure" in loan:
        loan["tenure"] = _tenure_field(loan["tenure"])
    for name in _DATE_FIELDS:
        if name in loan:
            loan[name] = _date_field(loan[name])
    if "repayment_frequency" in loan:
        loan["repayment_frequency"] = _frequency_field(loan["repayment_frequency"])
    if "penalties" in loan:
        loan["penalties"] = _penalties_field(loan["penalties"])


def _penalties_field(field: Any) -> list[Any]:
    """Drop a penalty that has no printed name. Do not invent a name or a fee.

    An empty list means none extracted. The rest of the loan stays.
    """

    if not isinstance(field, list):
        return []
    kept: list[Any] = []
    for penalty in field:
        item = _penalty_item(penalty)
        if item is not None:
            kept.append(item)
    return kept


def _penalty_item(penalty: Any) -> dict[str, Any] | None:
    if not isinstance(penalty, dict):
        return None
    name = _penalty_name(penalty.get("name"))
    if name is None:
        return None
    item = dict(penalty)
    item["name"] = name
    if "amount" not in item and ("value" in item or "currency" in item):
        item["amount"] = {"value": item.get("value"), "currency": item.get("currency")}
    item.pop("value", None)
    item.pop("currency", None)
    if "amount" in item:
        item["amount"] = _readable_penalty_money(item["amount"])
    if "rate" in item:
        item["rate"] = _readable_penalty_rate(item["rate"])
    description = item.get("description")
    if "description" in item and description is not None and not isinstance(description, str):
        item["description"] = None
    return item


def _penalty_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if name.casefold() in _UNUSABLE_NAME:
        return None
    return name


def _readable_penalty_money(field: Any) -> Any:
    """A penalty amount that cannot be read is unknown. It does not drop the loan."""

    normalized = _money_field(field)
    if not isinstance(normalized, dict):
        return {"value": None}
    try:
        MoneyField.model_validate(normalized)
    except ValidationError:
        return {"value": None}
    return normalized


def _readable_penalty_rate(field: Any) -> Any:
    normalized = _rate_field(field)
    if not isinstance(normalized, dict):
        return {"value": None}
    try:
        RateField.model_validate(normalized)
    except ValidationError:
        return {"value": None}
    return normalized


def _string_field(field: Any) -> Any:
    if field is None or isinstance(field, str):
        return {"value": field}
    return field


def _money_field(field: Any) -> Any:
    if field is None or isinstance(field, bool):
        return {"value": None}
    if isinstance(field, (int, float)):
        return {"value": _number_text(field)}
    if isinstance(field, str):
        return {"value": _money_text(field)}
    if isinstance(field, dict):
        value = field.get("value")
        if isinstance(value, bool):
            field["value"] = None
        elif isinstance(value, (int, float)):
            field["value"] = _number_text(value)
        elif isinstance(value, str):
            field["value"] = _money_text(value)
        _normalize_currency(field)
        return field
    return field


def _normalize_currency(field: dict[str, Any]) -> None:
    """Keep a 3-letter code. A symbol or word is not a code, so it stays unknown."""

    if "currency" not in field:
        return
    currency = field.get("currency")
    if isinstance(currency, str) and len(currency.strip()) == 3 and currency.strip().isalpha():
        field["currency"] = currency.strip().upper()
        return
    field["currency"] = None


def _number_text(value: int | float) -> str:
    """Digit text for a JSON number. The numeric value is not changed."""

    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _money_text(value: str) -> str:
    """Drop spaces and grouping commas. Leave the digits and the decimal point."""

    compact = re.sub(r"(?<=\d)\s+(?=\d)", "", value.strip().replace("\u00a0", " "))
    if "." in compact or re.fullmatch(r"-?\d{1,3}(,\d{3})+", compact):
        compact = re.sub(r"(?<=\d),(?=\d)", "", compact)
    return compact


def _rate_field(field: Any) -> Any:
    if field is None:
        return {"value": None}
    if isinstance(field, str):
        return _rate_from_text(field)
    if isinstance(field, bool):
        return {"value": None}
    if isinstance(field, (int, float)):
        return {"value": _number_text(field)}
    if not isinstance(field, dict):
        return field
    value = field.get("value")
    if isinstance(value, bool):
        field["value"] = None
    elif isinstance(value, (int, float)):
        field["value"] = _number_text(value)
    elif isinstance(value, str):
        parsed = _rate_from_text(value)
        if parsed["value"] is None and not value.strip().endswith("%"):
            field["value"] = None
        else:
            field["value"] = parsed["value"]
            if "unit" in parsed:
                field["unit"] = parsed["unit"]
    _clear_placeholder(field, "unit")
    return field


def _rate_from_text(text: str) -> dict[str, Any]:
    """Keep the printed number next to a percent sign. Do not calculate a rate."""

    stripped = text.strip()
    if stripped.endswith("%") and _RATE_PERCENT.fullmatch(stripped):
        number = stripped[:-1].strip().replace(" ", "")
        return {"value": number, "unit": "percent"}
    found = _RATE_PERCENT.search(stripped)
    if found is not None:
        return {"value": found.group(1), "unit": "percent"}
    plain = stripped.replace(" ", "")
    if _PLAIN_NUMBER.fullmatch(plain):
        return {"value": plain}
    return {"value": None}


def _tenure_field(field: Any) -> Any:
    if field is None:
        return {"value": None}
    if isinstance(field, str):
        field = {"value": field}
    if not isinstance(field, dict):
        return field
    value = field.get("value")
    if isinstance(value, bool) or not isinstance(value, (str, int)) or value is None:
        if value is None:
            _clear_placeholder(field, "unit")
            return field
        field["value"] = None
        _clear_placeholder(field, "unit")
        return field
    if isinstance(value, int):
        _clear_placeholder(field, "unit")
        return field
    match = _TENURE_TEXT.fullmatch(" ".join(value.split()))
    if match is None:
        field["value"] = None
        _clear_placeholder(field, "unit")
        return field
    unit_word = match.group(2).lower()
    field["value"] = int(match.group(1))
    field["unit"] = "days" if unit_word.startswith("day") else "months"
    _clear_placeholder(field, "unit")
    return field


def _frequency_field(field: Any) -> Any:
    if field is None:
        return {"value": None}
    if isinstance(field, str):
        return {"value": _frequency_token(field)}
    if isinstance(field, dict):
        _clear_placeholder(field, "value")
        if isinstance(field.get("value"), str):
            field["value"] = _frequency_token(field["value"])
    return field


def _frequency_token(value: str) -> str | None:
    """Fit a printed frequency word to the enum. Do not invent one."""

    if _is_placeholder(value):
        return None
    key = " ".join(value.strip().casefold().replace("-", " ").split())
    if key in _FREQUENCY_ALIASES:
        return _FREQUENCY_ALIASES[key]
    if key in _FREQUENCY_VALUES:
        return key
    return None


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold() in _PLACEHOLDER_TOKENS


def _clear_placeholder(field: dict[str, Any], key: str) -> None:
    """A printed word null, none, or a blank is empty. Do not invent a unit."""

    if _is_placeholder(field.get(key)):
        field[key] = None


def _date_field(field: Any) -> Any:
    if field is None or isinstance(field, str):
        stored = None if field is None else resolve_date(field).value
        return {"value": stored}
    if isinstance(field, dict):
        field["value"] = resolve_date(field.get("value")).value
    return field


def parse_extract_response(
    raw: Any,
    image_id: str,
    *,
    analysis_id: str | None = None,
    attempt: int | None = None,
    environment: str | None = None,
    image_bytes: Sequence[bytes] = (),
) -> ImageExtractResult:
    """Validate one image's extract payload. Calculated fields are rejected by the schema."""

    def reject(failures: list[dict[str, str]], exc: ValidationError | None = None) -> NoReturn:
        log_vision_response_rejected(
            analysis_id=analysis_id,
            task="extract",
            attempt=attempt,
            schema_name=EXTRACT_SCHEMA_NAME,
            failures=failures,
            model_answer=model_answer_text(raw),
            environment=vision_log_environment(environment),
            secrets=vision_log_secrets(),
            image_markers=image_log_markers(image_bytes),
        )
        if exc is None:
            raise VisionResponseInvalidError(_EXTRACT_MISMATCH, failures=failures)
        raise VisionResponseInvalidError(_EXTRACT_MISMATCH, failures=failures) from exc

    if not isinstance(raw, dict):
        reject([{"loc": "", "type": "dict_type"}])
    normalized = normalize_printed_extract(raw)
    try:
        if "results" in normalized:
            parsed = validate_contract(EXTRACT_SCHEMA_NAME, normalized)
            if not isinstance(parsed, ExtractContract):
                reject([{"loc": "", "type": "dict_type"}])
            if len(parsed.results) != 1:
                reject([{"loc": "results", "type": "list_length"}])
            stamped = parsed.results[0].model_copy(update={"image_id": image_id})
            return ImageExtractResult.model_validate(stamped.model_dump())
        if "loans" in normalized:
            return ImageExtractResult.model_validate({**normalized, "image_id": image_id})
    except ValidationError as exc:
        reject(validation_failure_items(exc), exc)
    reject([{"loc": "", "type": "missing"}])


async def extract_image(
    provider: VisionProvider,
    *,
    analysis_id: str,
    image: VisionImage,
) -> ImageExtractResult:
    """Extract this one loan-document image.

    Invalid JSON is retried once inside the provider for this call. One image
    per call keeps that retry on this image. Missing fields stay null.
    """

    response = await provider.analyze_images(
        VisionRequest(
            analysis_id=analysis_id,
            task="extract",
            images=[image],
            response_schema_name=EXTRACT_SCHEMA_NAME,
            hint=None,
        )
    )
    return parse_extract_response(
        response.raw_json,
        image.image_id,
        analysis_id=analysis_id,
        image_bytes=(image.bytes,),
    )


def raw_extract_items(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract checkpoints already stored. A page that has not finished is absent."""

    raw = document.get("raw_extracts")
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("image_id"), str):
            items.append(item)
    return items


def extracted_image_ids(document: dict[str, Any]) -> set[str]:
    return {item["image_id"] for item in raw_extract_items(document)}


def with_extract_checkpoint(
    document: dict[str, Any],
    extracted: ImageExtractResult,
) -> dict[str, Any]:
    """Store this image beside earlier pages. Do not invent a loan for a later page."""

    updated = dict(document)
    image_id = extracted.image_id
    kept = [item for item in raw_extract_items(updated) if item.get("image_id") != image_id]
    kept.append(extracted.model_dump(mode="json"))
    updated["raw_extracts"] = kept
    updated.setdefault("lender_groups", [])
    return updated


def lender_groups_from_extracts(raw_extracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge per-image extracts into the draft GET ``lender_groups`` list.

    Calculated fields, validations, and summary lines are not added here.
    """

    return group_raw_extracts(raw_extracts)


def lender_groups_for_assignment(
    raw_extracts: list[dict[str, Any]],
    assignment: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Re-merge a user image-to-loan assignment into the same GET shape."""

    return apply_image_assignment(raw_extracts, assignment)
