"""Deterministic loan checks (SPEC §11).

Checks read extracted or user-corrected inputs and return passed, failed, or
skipped. They do not rewrite amounts, dates, or rates. A ₹1.00 tolerance
applies only to fee reconciliation. Other amount comparisons are exact.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app.services.validation.money import parse_decimal_money

CHECK_IDS = (
    "V_POSITIVE_SANCTIONED",
    "V_POSITIVE_DISBURSED",
    "V_POSITIVE_REPAYMENT",
    "V_DISBURSED_LTE_SANCTIONED",
    "V_REPAYMENT_GTE_DISBURSED",
    "V_FEES_RECONCILE",
    "V_DATES_ORDER",
    "V_TENURE_VS_DATES",
    "V_RATE_RANGE",
)

_SUMMARY_REQUIRED = frozenset(
    {
        "V_POSITIVE_SANCTIONED",
        "V_POSITIVE_DISBURSED",
        "V_POSITIVE_REPAYMENT",
    }
)
_USABLE_SOURCES = frozenset({"extracted", "user_corrected"})
_FEE_TOLERANCE = Decimal("1.00")
_RATE_LIMIT = Decimal("1000")
_DATE_FIELDS = ("sanction_date", "disbursement_date", "repayment_date")
CURRENCY_UNKNOWN = "CURRENCY_UNKNOWN"


class _NotInDays:
    """Tenure is present, but not measured in days."""


def validate_loan_fields(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    """Run every check. The input mapping is not modified."""

    validations = [
        _positive("V_POSITIVE_SANCTIONED", "Sanctioned amount", _amount(fields, "sanctioned_amount")),
        _positive("V_POSITIVE_DISBURSED", "Disbursed amount", _amount(fields, "disbursed_amount")),
        _positive("V_POSITIVE_REPAYMENT", "Repayment amount", _amount(fields, "repayment_amount")),
        *_amount_comparisons(fields),
        _dates_order(fields),
        _tenure_vs_dates(fields),
        _rate_range(fields),
    ]
    currency_unknown = any(item.get("currency_unknown") for item in validations)
    public = [_public_check(item) for item in validations]
    return {
        "validations": public,
        "validation_summary": _summary(public),
        "review_flags": _review_flags(public, currency_unknown=currency_unknown),
    }


def attach_loan_validations(lender_groups: list[dict[str, Any]]) -> None:
    """Set `validations` and `validation_summary` on each grouped loan.

    Existing grouping flags stay. Validation flags are replaced so a second
    pass does not keep a stale failure. Extracted field values stay as they are.
    """

    for group in lender_groups:
        loans = group.get("loans")
        if not isinstance(loans, list):
            continue
        for loan in loans:
            if not isinstance(loan, dict):
                continue
            fields = loan.get("fields")
            snapshot = _snapshot_fields(fields)
            result = validate_loan_fields(fields if isinstance(fields, Mapping) else None)
            if _snapshot_fields(fields) != snapshot:
                raise RuntimeError("validation rewrote extracted fields")
            loan["validations"] = result["validations"]
            loan["validation_summary"] = result["validation_summary"]
            loan["review_flags"] = _merged_flags(loan.get("review_flags"), result["review_flags"])


def _amount_comparisons(fields: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    sanctioned = _amount(fields, "sanctioned_amount")
    disbursed = _amount(fields, "disbursed_amount")
    repayment = _amount(fields, "repayment_amount")
    processing_fee = _amount(fields, "processing_fee")
    gst_amount = _amount(fields, "gst_amount")
    return [
        _disbursed_lte_sanctioned(sanctioned, disbursed),
        _repayment_gte_disbursed(repayment, disbursed),
        _fees_reconcile(sanctioned, disbursed, processing_fee, gst_amount),
    ]


def _positive(check_id: str, label: str, amount: tuple[Decimal, str | None] | None) -> dict[str, Any]:
    if amount is None:
        return _check(check_id, "skipped", f"{label} is missing.")
    if amount[0] > 0:
        return _check(check_id, "passed", f"{label} is greater than zero.")
    return _check(check_id, "failed", f"{label} is not greater than zero.")


def _disbursed_lte_sanctioned(
    sanctioned: tuple[Decimal, str | None] | None,
    disbursed: tuple[Decimal, str | None] | None,
) -> dict[str, Any]:
    check_id = "V_DISBURSED_LTE_SANCTIONED"
    ready = _ready_to_compare((sanctioned, disbursed), "Sanctioned amount or disbursed amount is missing.")
    if ready is not None:
        return _check(check_id, "skipped", ready[0], currency_unknown=ready[1])
    assert sanctioned is not None and disbursed is not None
    if disbursed[0] > sanctioned[0]:
        return _check(check_id, "failed", "Disbursed amount is greater than sanctioned amount.")
    return _check(
        check_id,
        "passed",
        "Disbursed amount is less than or equal to sanctioned amount.",
    )


def _repayment_gte_disbursed(
    repayment: tuple[Decimal, str | None] | None,
    disbursed: tuple[Decimal, str | None] | None,
) -> dict[str, Any]:
    check_id = "V_REPAYMENT_GTE_DISBURSED"
    ready = _ready_to_compare((repayment, disbursed), "Repayment amount or disbursed amount is missing.")
    if ready is not None:
        return _check(check_id, "skipped", ready[0], currency_unknown=ready[1])
    assert repayment is not None and disbursed is not None
    if repayment[0] < disbursed[0]:
        return _check(check_id, "failed", "Repayment amount is less than disbursed amount.")
    return _check(
        check_id,
        "passed",
        "Repayment amount is greater than or equal to disbursed amount.",
    )


def _fees_reconcile(
    sanctioned: tuple[Decimal, str | None] | None,
    disbursed: tuple[Decimal, str | None] | None,
    processing_fee: tuple[Decimal, str | None] | None,
    gst_amount: tuple[Decimal, str | None] | None,
) -> dict[str, Any]:
    check_id = "V_FEES_RECONCILE"
    parts = (sanctioned, disbursed, processing_fee, gst_amount)
    ready = _ready_to_compare(
        parts,
        "Sanctioned amount, disbursed amount, processing fee, or GST is missing.",
    )
    if ready is not None:
        return _check(check_id, "skipped", ready[0], currency_unknown=ready[1])
    assert sanctioned is not None and disbursed is not None
    assert processing_fee is not None and gst_amount is not None
    difference = abs(sanctioned[0] - disbursed[0] - processing_fee[0] - gst_amount[0])
    if difference <= _FEE_TOLERANCE:
        return _check(
            check_id,
            "passed",
            "Sanctioned amount matches disbursed amount plus fees within 1.00.",
        )
    return _check(
        check_id,
        "failed",
        "Sanctioned amount does not match disbursed amount plus fees within 1.00.",
    )


def _dates_order(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    check_id = "V_DATES_ORDER"
    present: list[date] = []
    for name in _DATE_FIELDS:
        value = _date_value(fields, name)
        if value is not None:
            present.append(value)
    if len(present) < 2:
        return _check(check_id, "skipped", "Fewer than two loan dates are present.")
    dates = present
    if any(dates[left] > dates[right] for left in range(len(dates)) for right in range(left + 1, len(dates))):
        return _check(check_id, "failed", "Loan dates are out of order.")
    return _check(check_id, "passed", "Loan dates are in order.")


def _tenure_vs_dates(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    check_id = "V_TENURE_VS_DATES"
    tenure = _tenure_days(fields)
    disbursement = _date_value(fields, "disbursement_date")
    repayment = _date_value(fields, "repayment_date")
    if isinstance(tenure, _NotInDays):
        return _check(check_id, "skipped", "Tenure is not measured in days.")
    if tenure is None or disbursement is None or repayment is None:
        return _check(
            check_id,
            "skipped",
            "Disbursement date, repayment date, or tenure in days is missing.",
        )
    actual_days = (repayment - disbursement).days
    if abs(actual_days - tenure) <= 1:
        return _check(check_id, "passed", "Tenure matches the disbursement and repayment dates.")
    return _check(check_id, "failed", "Tenure does not match the disbursement and repayment dates.")


def _rate_range(fields: Mapping[str, Any] | None) -> dict[str, Any]:
    check_id = "V_RATE_RANGE"
    rates = [rate for rate in (_rate_value(fields, "interest_rate"), _rate_value(fields, "apr")) if rate is not None]
    if not rates:
        return _check(check_id, "skipped", "Interest rate and APR are missing.")
    if any(rate <= 0 or rate >= _RATE_LIMIT for rate in rates):
        return _check(check_id, "failed", "Interest rate or APR is outside 0 to 1000 percent.")
    return _check(check_id, "passed", "Interest rate and APR are inside 0 to 1000 percent.")


def _ready_to_compare(
    parts: tuple[tuple[Decimal, str | None] | None, ...],
    missing_message: str,
) -> tuple[str, bool] | None:
    """None when every amount is present in one known currency.

    The boolean is true when the skip is caused by mixed or unknown currency.
    """

    if any(part is None for part in parts):
        return missing_message, False
    currencies = [part[1] for part in parts if part is not None]
    if len(set(currencies)) == 1 and isinstance(currencies[0], str) and currencies[0] != "":
        return None
    return "Amounts are not in the same known currency.", True


def _check(
    check_id: str,
    status: str,
    message: str,
    *,
    currency_unknown: bool = False,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "message": message,
        "currency_unknown": currency_unknown,
    }


def _public_check(item: Mapping[str, Any]) -> dict[str, str]:
    return {"id": str(item["id"]), "status": str(item["status"]), "message": str(item["message"])}


def _summary(validations: list[dict[str, str]]) -> str:
    if any(item["status"] == "failed" for item in validations):
        return "failed"
    required_skipped = any(
        item["id"] in _SUMMARY_REQUIRED and item["status"] == "skipped" for item in validations
    )
    if required_skipped:
        return "incomplete"
    return "ok"


def _review_flags(validations: list[dict[str, str]], *, currency_unknown: bool) -> list[str]:
    flags = [f"VALIDATION_FAILED:{item['id']}" for item in validations if item["status"] == "failed"]
    if currency_unknown:
        flags.append(CURRENCY_UNKNOWN)
    return flags


def _merged_flags(existing: Any, validation_flags: list[str]) -> list[str]:
    kept: list[str] = []
    if isinstance(existing, list):
        for flag in existing:
            if isinstance(flag, str) and not _owned_flag(flag):
                kept.append(flag)
    return kept + validation_flags


def _owned_flag(flag: str) -> bool:
    return flag == CURRENCY_UNKNOWN or flag.startswith("VALIDATION_FAILED:")


def _snapshot_fields(fields: Any) -> Any:
    if not isinstance(fields, Mapping):
        return None
    return {name: _snapshot_value(value) for name, value in fields.items()}


def _snapshot_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _snapshot_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snapshot_value(item) for item in value]
    return value


def _amount(
    fields: Mapping[str, Any] | None,
    name: str,
) -> tuple[Decimal, str | None] | None:
    payload = _payload(fields, name)
    if payload is None or not _usable_source(payload):
        return None
    value = payload.get("value")
    if value is None:
        return None
    try:
        amount = parse_decimal_money(value)
    except (ValueError, TypeError, InvalidOperation, ArithmeticError):
        return None
    if amount is None:
        return None
    currency = payload.get("currency")
    if currency is not None and not isinstance(currency, str):
        return None
    return amount, currency


def _date_value(fields: Mapping[str, Any] | None, name: str) -> date | None:
    payload = _payload(fields, name)
    if payload is None or not _usable_source(payload):
        return None
    value = payload.get("value")
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _tenure_days(fields: Mapping[str, Any] | None) -> int | None | _NotInDays:
    payload = _payload(fields, "tenure")
    if payload is None or not _usable_source(payload):
        return None
    value = payload.get("value")
    unit = payload.get("unit")
    if value is None and unit is None:
        return None
    if unit != "days":
        return _NotInDays()
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _rate_value(fields: Mapping[str, Any] | None, name: str) -> Decimal | None:
    payload = _payload(fields, name)
    if payload is None or not _usable_source(payload):
        return None
    value = payload.get("value")
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        return None
    try:
        if isinstance(value, Decimal):
            rate = value
        elif isinstance(value, int):
            rate = Decimal(value)
        elif isinstance(value, str):
            rate = Decimal(value)
        else:
            return None
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not rate.is_finite():
        return None
    return rate


def _payload(fields: Mapping[str, Any] | None, name: str) -> Mapping[str, Any] | None:
    if fields is None:
        return None
    field = fields.get(name)
    if isinstance(field, Mapping):
        return field
    if hasattr(field, "model_dump"):
        dumped = field.model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _usable_source(payload: Mapping[str, Any]) -> bool:
    source = payload.get("source")
    if source is None:
        return True
    return source in _USABLE_SOURCES
