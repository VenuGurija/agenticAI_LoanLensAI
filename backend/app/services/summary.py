"""Short amount and date labels, and the §4.2 summary line (SPEC §9.4, §9.6, §14.7, §14.10)."""

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from app.services.validation.money import calculate_fields, parse_decimal_money

AMBIGUOUS_DATE = "AMBIGUOUS_DATE"

_THOUSAND = Decimal("1000")
_TEN_THOUSAND = Decimal("10000")
_MONTHS = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_MONTH_NUMBERS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_TWO_PLACES = Decimal("0.01")
_ONE_PLACE = Decimal("0.1")
_USABLE_SOURCES = frozenset({"extracted", "user_corrected"})
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMERIC_YMD = re.compile(r"^(?P<year>\d{4})[./-](?P<month>\d{1,2})[./-](?P<day>\d{1,2})$")
_NUMERIC_FULL = re.compile(r"^(?P<a>\d{1,2})[./-](?P<b>\d{1,2})[./-](?P<year>\d{4})$")
_NAMED_DATE = re.compile(
    r"^(?:(?P<month1>[A-Za-z]+)\.?\s+(?P<day1>\d{1,2})(?:st|nd|rd|th)?"
    r"|(?P<day2>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month2>[A-Za-z]+)\.?)"
    r"(?:,?\s+(?P<year>\d{4}))?$"
)


@dataclass(frozen=True)
class ResolvedDate:
    """A stored ``YYYY-MM-DD`` value, or null when the year cannot be inferred."""

    value: str | None
    review_flag: str | None = None


def display_sanctioned_short(value: Any) -> str | None:
    """Short sanctioned label. ``15k`` when the amount is a whole thousand and at least 10000.

    A null amount stays null. Other amounts stay the rupee figure, without a trailing ``.00``.
    """

    raw = _unwrap_value(value)
    if raw is None:
        return None
    amount = parse_decimal_money(raw)
    if amount is None:
        return None
    if amount >= _TEN_THOUSAND and amount % _THOUSAND == 0:
        thousands = amount / _THOUSAND
        return f"{_trim_zeros(thousands)}k"
    text = format(amount, "f")
    if text.endswith(".00"):
        return text[:-3]
    return text


def display_date_short(value: str | None, *, today: date | None = None) -> str | None:
    """Summary date such as ``jun 17``. The year is included when it is not ``today``'s year."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("stored date must be YYYY-MM-DD or null")
    parsed = _strict_iso(value)
    if parsed is None:
        raise ValueError("stored date must be YYYY-MM-DD or null")
    label = f"{_MONTHS[parsed.month - 1]} {parsed.day}"
    reference = date.today() if today is None else today
    if parsed.year != reference.year:
        return f"{label}, {parsed.year}"
    return label


def resolve_date(
    value: Any,
    *,
    created_at: date | datetime | str | None = None,
) -> ResolvedDate:
    """Store a date as ``YYYY-MM-DD``.

    A month and day with no year uses the analysis ``created_at`` year when that
    date falls within 18 months of ``created_at``. Otherwise the value stays
    null and ``review_flag`` is ``AMBIGUOUS_DATE``.
    """

    if value is None:
        return ResolvedDate(None)
    if isinstance(value, datetime):
        return ResolvedDate(value.date().isoformat())
    if isinstance(value, date):
        return ResolvedDate(value.isoformat())
    if not isinstance(value, str):
        return ResolvedDate(None, AMBIGUOUS_DATE)

    text = " ".join(value.split())
    if text == "":
        return ResolvedDate(None)
    stored = _strict_iso(text)
    if stored is not None:
        return ResolvedDate(stored.isoformat())
    if _ISO_DATE.fullmatch(text):
        return ResolvedDate(None, AMBIGUOUS_DATE)
    numeric = _numeric_full_date(text)
    if numeric is not None:
        return numeric

    named = _NAMED_DATE.fullmatch(text)
    if named is None:
        return ResolvedDate(None, AMBIGUOUS_DATE)

    month_name = named.group("month1") or named.group("month2")
    day_text = named.group("day1") or named.group("day2")
    year_text = named.group("year")
    month = _MONTH_NUMBERS.get(month_name.casefold())
    if month is None or day_text is None:
        return ResolvedDate(None, AMBIGUOUS_DATE)
    day = int(day_text)
    if year_text is not None:
        resolved = _iso_date(int(year_text), month, day)
        if resolved is None:
            return ResolvedDate(None, AMBIGUOUS_DATE)
        return ResolvedDate(resolved)

    anchor = _as_date(created_at) if created_at is not None else None
    if anchor is None:
        return ResolvedDate(None, AMBIGUOUS_DATE)
    resolved = _iso_date(anchor.year, month, day)
    if resolved is None:
        return ResolvedDate(None, AMBIGUOUS_DATE)
    candidate = date.fromisoformat(resolved)
    if not within_eighteen_months(candidate, anchor):
        return ResolvedDate(None, AMBIGUOUS_DATE)
    return ResolvedDate(resolved)


def within_eighteen_months(candidate: date, created_at: date) -> bool:
    """True when ``candidate`` is within 18 calendar months of ``created_at``, inclusive."""

    return _shift_months(created_at, -18) <= candidate <= _shift_months(created_at, 18)


def summary_line(loan: Mapping[str, Any], *, today: date | None = None) -> str:
    """One loan line in the §4.2 display contract.

    Unknown pieces are left out. A missing sanctioned amount starts the line
    with ``amount unknown`` when something else can be shown. A loan with no
    sanctioned, disbursed, or repayment amount is ``Needs review``.
    """

    sanctioned = _amount_short(loan, "sanctioned_amount")
    disbursed = _amount_short(loan, "disbursed_amount")
    repayment = _amount_short(loan, "repayment_amount")
    if sanctioned is None and disbursed is None and repayment is None:
        return "Needs review"

    parts = [sanctioned if sanctioned is not None else "amount unknown"]
    if disbursed is not None:
        got = f"got {disbursed}"
        disbursed_on = _date_label(loan, "disbursement_date", today=today)
        if disbursed_on is not None:
            got = f"{got} on {disbursed_on}"
        parts.append(got)
    if repayment is not None:
        pay = f"pay {repayment}"
        repayment_on = _date_label(loan, "repayment_date", today=today)
        if repayment_on is not None:
            pay = f"{pay} on {repayment_on}"
        parts.append(pay)
    extra = _extra_clause(loan)
    if extra is not None:
        parts.append(extra)
    return ", ".join(parts)


def group_header(lender_brand: str | None, legal_lending_entity: str | None) -> str:
    """``brand (legal)`` when the legal name is known. A blank name is omitted."""

    brand = _display_label(lender_brand)
    legal = _display_label(legal_lending_entity)
    if brand is not None and legal is not None:
        return f"{brand} ({legal})"
    if brand is not None:
        return brand
    if legal is not None:
        return legal
    return ""


def book_totals(loans: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum accepted-loan inputs that are non-null INR amounts.

    A missing disbursed or repayment amount, or an amount that is not INR,
    sets ``incomplete`` and is left out of the sum. It is not treated as zero.
    """

    received, received_gap = _sum_inputs(loans, "disbursed_amount")
    still_to_pay, repayment_gap = _sum_inputs(loans, "repayment_amount")
    extra, extra_gap = _sum_extra(loans)
    return {
        "received": received,
        "still_to_pay": still_to_pay,
        "extra": extra,
        "incomplete": received_gap or repayment_gap or extra_gap,
    }


def build_summary_groups(
    lender_groups: list[Any],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Summary groups: header plus one line per loan, in stored order."""

    groups: list[dict[str, Any]] = []
    for group in lender_groups:
        if not isinstance(group, Mapping):
            continue
        lines: list[dict[str, Any]] = []
        loans = group.get("loans")
        if isinstance(loans, list):
            for loan in loans:
                if not isinstance(loan, Mapping):
                    continue
                flag = loan.get("needs_review")
                lines.append(
                    {
                        "loan_internal_id": _loan_id(loan),
                        "text": summary_line(loan, today=today),
                        "needs_review": flag if isinstance(flag, bool) else False,
                    }
                )
        groups.append(
            {
                "header": group_header(
                    _optional_str(group.get("lender_brand")),
                    _optional_str(group.get("legal_lending_entity")),
                ),
                "lines": lines,
            }
        )
    return groups


def build_analysis_summary(
    analysis_id: str,
    status: str,
    lender_groups: list[Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Draft summary DTO (SPEC §14.7). Totals belong on the book summary."""

    return {
        "analysis_id": analysis_id,
        "status": status,
        "groups": build_summary_groups(lender_groups, today=today),
    }


def build_book_summary(
    book_id: str,
    lender_groups: list[Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Book summary DTO, including totals (SPEC §14.10)."""

    return {
        "book_id": book_id,
        "totals": book_totals(_loans_in(lender_groups)),
        "groups": build_summary_groups(lender_groups, today=today),
    }


def _unwrap_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get("value")
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
            return dumped.get("value")
    return value


def _trim_zeros(amount: Decimal) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _numeric_full_date(text: str) -> ResolvedDate | None:
    """Store a fully specified numeric date. A missing year stays unresolved.

    When both numbers can be a month, day-first is used. That matches the
    printed dates on Indian loan screenshots. A number above 12 decides the order.
    """

    year_first = _NUMERIC_YMD.fullmatch(text)
    if year_first is not None:
        resolved = _iso_date(
            int(year_first.group("year")),
            int(year_first.group("month")),
            int(year_first.group("day")),
        )
        if resolved is None:
            return ResolvedDate(None, AMBIGUOUS_DATE)
        return ResolvedDate(resolved)

    parts = _NUMERIC_FULL.fullmatch(text)
    if parts is None:
        return None
    first = int(parts.group("a"))
    second = int(parts.group("b"))
    year = int(parts.group("year"))
    if first > 12 and second > 12:
        return ResolvedDate(None, AMBIGUOUS_DATE)
    if second > 12:
        month, day = first, second
    else:
        day, month = first, second
    resolved = _iso_date(year, month, day)
    if resolved is None:
        return ResolvedDate(None, AMBIGUOUS_DATE)
    return ResolvedDate(resolved)


def _strict_iso(value: str) -> date | None:
    if not _ISO_DATE.fullmatch(value):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if parsed.isoformat() != value:
        return None
    return parsed


def _iso_date(year: int, month: int, day: int) -> str | None:
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    return parsed.isoformat()


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if "T" in text:
        return datetime.fromisoformat(text).date()
    parsed = _strict_iso(text)
    if parsed is None:
        raise ValueError("created_at must be a date or datetime")
    return parsed


def _shift_months(value: date, months: int) -> date:
    absolute = value.year * 12 + (value.month - 1) + months
    year, month_index = divmod(absolute, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _amount_short(loan: Mapping[str, Any], name: str) -> str | None:
    field = _usable_field(loan, name)
    if field is None:
        return None
    return display_sanctioned_short(field.get("value"))


def _date_label(loan: Mapping[str, Any], name: str, *, today: date | None) -> str | None:
    field = _usable_field(loan, name)
    if field is None:
        return None
    value = field.get("value")
    if not isinstance(value, str):
        return None
    try:
        return display_date_short(value, today=today)
    except (TypeError, ValueError):
        return None


def _extra_clause(loan: Mapping[str, Any]) -> str | None:
    """Present only when both extra rupees and extra percent exist."""

    fields = loan.get("fields")
    calculated = calculate_fields(fields if isinstance(fields, Mapping) else None)
    rupees = calculated["implied_cost"].get("value")
    percent = calculated["extra_percent"].get("value")
    if rupees is None or percent is None:
        return None
    short = display_sanctioned_short(rupees)
    if short is None:
        return None
    return f"extra {short} ({_one_decimal(percent)}%)"


def _one_decimal(value: str) -> str:
    quantized = Decimal(value).quantize(_ONE_PLACE, rounding=ROUND_HALF_UP)
    if quantized == 0:
        quantized = Decimal("0.0")
    return format(quantized, "f")


def _usable_field(loan: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    fields = loan.get("fields")
    if not isinstance(fields, Mapping):
        return None
    field = fields.get(name)
    if not isinstance(field, Mapping) or field.get("value") is None:
        return None
    source = field.get("source")
    if source is None:
        source = "extracted"
    if source not in _USABLE_SOURCES:
        return None
    return field


def _sum_inputs(loans: list[Mapping[str, Any]], name: str) -> tuple[str, bool]:
    incomplete = False
    total = Decimal("0.00")
    for loan in loans:
        field = _usable_field(loan, name)
        if field is None:
            incomplete = True
            continue
        try:
            amount = parse_decimal_money(field.get("value"))
        except ValueError:
            incomplete = True
            continue
        if amount is None:
            incomplete = True
            continue
        currency = field.get("currency")
        if currency != "INR":
            incomplete = True
            continue
        total += amount
    return _format_total(total), incomplete


def _sum_extra(loans: list[Mapping[str, Any]]) -> tuple[str, bool]:
    incomplete = False
    total = Decimal("0.00")
    for loan in loans:
        fields = loan.get("fields")
        calculated = calculate_fields(fields if isinstance(fields, Mapping) else None)
        cost = calculated["implied_cost"]
        if cost.get("value") is None:
            continue
        if cost.get("currency") != "INR":
            incomplete = True
            continue
        try:
            amount = parse_decimal_money(cost["value"])
        except ValueError:
            incomplete = True
            continue
        if amount is None:
            continue
        total += amount
    return _format_total(total), incomplete


def _format_total(amount: Decimal) -> str:
    quantized = amount.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
    if quantized == 0:
        quantized = Decimal("0.00")
    return format(quantized, "f")


def _loans_in(lender_groups: list[Any]) -> list[Mapping[str, Any]]:
    loans: list[Mapping[str, Any]] = []
    for group in lender_groups:
        if not isinstance(group, Mapping):
            continue
        items = group.get("loans")
        if not isinstance(items, list):
            continue
        for loan in items:
            if isinstance(loan, Mapping):
                loans.append(loan)
    return loans


def _loan_id(loan: Mapping[str, Any]) -> str:
    value = loan.get("loan_internal_id")
    if isinstance(value, str):
        return value
    return ""


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _display_label(value: str | None) -> str | None:
    if value is None:
        return None
    if value.strip() == "":
        return None
    return value
