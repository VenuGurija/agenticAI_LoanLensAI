"""Decimal money parsing and calculated loan fields (SPEC §9.3, §9.6, §11.3).

Arithmetic uses decimal.Decimal. A null input stays unknown. The disbursed
amount is never copied into the sanctioned amount.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from app.schemas.extraction import parse_money_value

_TWO_PLACES = Decimal("0.01")
_ONE_PLACE = Decimal("0.1")
_HUNDRED = Decimal("100")
_USABLE_SOURCES = frozenset({"extracted", "user_corrected"})


class _CurrencyMismatch:
    """Both amounts name a currency, and the codes differ."""


def parse_decimal_money(value: Any) -> Decimal | None:
    """Parse a money value to Decimal. Null stays null. Floats are rejected."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("money value must be a finite decimal")
        quantized = value.quantize(_TWO_PLACES)
        if quantized != value:
            raise ValueError("money value must have at most 2 decimal places")
        return quantized
    text = parse_money_value(value)
    if text is None:
        return None
    return Decimal(text)


def calculate_fields(fields: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Fill calculated money from extracted or user-corrected inputs.

    ``fee_plus_gst`` is processing fee plus GST. ``implied_fees_total`` is
    sanctioned minus disbursed. ``implied_cost`` is repayment minus disbursed.
    ``extra_percent`` is that cost as a percent of disbursed when disbursed is
    greater than zero. Each result is unknown when an input is missing.
    """

    sanctioned = _usable_money(_field(fields, "sanctioned_amount"))
    disbursed = _usable_money(_field(fields, "disbursed_amount"))
    processing_fee = _usable_money(_field(fields, "processing_fee"))
    gst_amount = _usable_money(_field(fields, "gst_amount"))
    repayment = _usable_money(_field(fields, "repayment_amount"))
    return {
        "implied_fees_total": _subtract(sanctioned, disbursed),
        "fee_plus_gst": _add(processing_fee, gst_amount),
        "implied_cost": _subtract(repayment, disbursed),
        "extra_percent": _extra_percent(repayment, disbursed),
    }


def _field(fields: Mapping[str, Any] | None, name: str) -> Any:
    if fields is None:
        return None
    return fields.get(name)


def _as_mapping(field: Any) -> Mapping[str, Any] | None:
    if isinstance(field, Mapping):
        return field
    if hasattr(field, "model_dump"):
        dumped = field.model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _usable_money(field: Any) -> tuple[Decimal, str | None] | None:
    """Amount and currency when source is extracted or user_corrected."""

    payload = _as_mapping(field)
    if payload is None:
        return None
    value = payload.get("value")
    if value is None:
        return None
    source = payload.get("source")
    if source is None:
        source = "extracted"
    if source not in _USABLE_SOURCES:
        return None
    amount = parse_decimal_money(value)
    if amount is None:
        return None
    currency = payload.get("currency")
    if currency is not None and not isinstance(currency, str):
        return None
    return amount, currency


def _shared_currency(
    left: tuple[Decimal, str | None],
    right: tuple[Decimal, str | None],
) -> str | None | _CurrencyMismatch:
    left_currency = left[1]
    right_currency = right[1]
    if (
        isinstance(left_currency, str)
        and isinstance(right_currency, str)
        and left_currency != right_currency
    ):
        return _CurrencyMismatch()
    if left_currency == right_currency:
        return left_currency
    return None


def _money_result(amount: Decimal, currency: str | None) -> dict[str, Any]:
    quantized = amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if quantized == 0:
        quantized = Decimal("0.00")
    return {
        "value": format(quantized, "f"),
        "currency": currency,
        "source": "calculated",
    }


def _unknown_money() -> dict[str, Any]:
    return {"value": None, "currency": None, "source": "unknown"}


def _unknown_percent() -> dict[str, Any]:
    return {"value": None, "source": "unknown"}


def _subtract(
    left: tuple[Decimal, str | None] | None,
    right: tuple[Decimal, str | None] | None,
) -> dict[str, Any]:
    if left is None or right is None:
        return _unknown_money()
    currency = _shared_currency(left, right)
    if isinstance(currency, _CurrencyMismatch):
        return _unknown_money()
    return _money_result(left[0] - right[0], currency)


def _add(
    left: tuple[Decimal, str | None] | None,
    right: tuple[Decimal, str | None] | None,
) -> dict[str, Any]:
    if left is None or right is None:
        return _unknown_money()
    currency = _shared_currency(left, right)
    if isinstance(currency, _CurrencyMismatch):
        return _unknown_money()
    return _money_result(left[0] + right[0], currency)


def _extra_percent(
    repayment: tuple[Decimal, str | None] | None,
    disbursed: tuple[Decimal, str | None] | None,
) -> dict[str, Any]:
    if repayment is None or disbursed is None or disbursed[0] <= 0:
        return _unknown_percent()
    currency = _shared_currency(repayment, disbursed)
    if isinstance(currency, _CurrencyMismatch):
        return _unknown_percent()
    percent = (repayment[0] - disbursed[0]) / disbursed[0] * _HUNDRED
    quantized = percent.quantize(_ONE_PLACE, rounding=ROUND_HALF_UP)
    if quantized == 0:
        quantized = Decimal("0.0")
    return {"value": format(quantized, "f"), "source": "calculated"}
