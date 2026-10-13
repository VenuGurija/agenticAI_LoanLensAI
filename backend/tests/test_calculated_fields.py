"""Calculated money fields and short amount/date labels (SPEC §9.3, §9.4, §9.6)."""

from datetime import date
from decimal import Decimal

import pytest

from app.services.summary import (
    AMBIGUOUS_DATE,
    display_date_short,
    display_sanctioned_short,
    resolve_date,
    within_eighteen_months,
)
from app.services.validation.money import calculate_fields, parse_decimal_money


def _money(
    value: str | None,
    source: str | None = None,
    currency: str | None = "INR",
) -> dict:
    if value is None:
        return {"value": None, "currency": None, "source": "unknown"}
    return {
        "value": value,
        "currency": currency,
        "source": "extracted" if source is None else source,
    }


def test_spec_example_implied_fees_cost_and_extra_percent() -> None:
    calculated = calculate_fields(
        {
            "sanctioned_amount": _money("15000.00"),
            "disbursed_amount": _money("13230.00"),
            "repayment_amount": _money("16950.00"),
        }
    )

    assert calculated["implied_fees_total"]["value"] == "1770.00"
    assert calculated["implied_fees_total"]["source"] == "calculated"
    assert calculated["implied_fees_total"]["currency"] == "INR"
    assert calculated["implied_cost"]["value"] == "3720.00"
    assert calculated["implied_cost"]["source"] == "calculated"
    assert calculated["extra_percent"]["value"] == "28.1"
    assert calculated["extra_percent"]["source"] == "calculated"
    assert display_sanctioned_short("15000.00") == "15k"
    assert display_sanctioned_short("20000") == "20k"


def test_missing_disbursed_leaves_fees_cost_and_percent_null() -> None:
    calculated = calculate_fields(
        {
            "sanctioned_amount": _money("15000.00"),
            "disbursed_amount": _money(None),
            "repayment_amount": _money("16950.00"),
            "processing_fee": _money("1000.00"),
            "gst_amount": _money("180.00"),
        }
    )

    assert calculated["implied_fees_total"]["value"] is None
    assert calculated["implied_fees_total"]["source"] == "unknown"
    assert calculated["implied_cost"]["value"] is None
    assert calculated["implied_cost"]["source"] == "unknown"
    assert calculated["extra_percent"]["value"] is None
    assert calculated["extra_percent"]["source"] == "unknown"
    assert calculated["fee_plus_gst"]["value"] == "1180.00"


def test_extra_percent_is_null_when_disbursed_is_zero_or_negative() -> None:
    zero = calculate_fields(
        {
            "disbursed_amount": _money("0.00"),
            "repayment_amount": _money("16950.00"),
        }
    )
    negative = calculate_fields(
        {
            "disbursed_amount": _money("-10.00"),
            "repayment_amount": _money("100.00"),
        }
    )

    assert zero["extra_percent"]["value"] is None
    assert zero["extra_percent"]["source"] == "unknown"
    assert zero["implied_cost"]["value"] == "16950.00"
    assert negative["extra_percent"]["value"] is None
    assert negative["implied_cost"]["value"] == "110.00"


def test_disbursed_is_not_used_as_sanctioned() -> None:
    fields = {
        "sanctioned_amount": _money(None),
        "disbursed_amount": _money("13230.00"),
        "repayment_amount": _money("16950.00"),
    }

    calculated = calculate_fields(fields)

    assert calculated["implied_fees_total"]["value"] is None
    assert calculated["implied_fees_total"]["source"] == "unknown"
    assert fields["sanctioned_amount"]["value"] is None
    assert display_sanctioned_short(fields["sanctioned_amount"]) is None
    assert calculated["implied_cost"]["value"] == "3720.00"
    assert calculated["extra_percent"]["value"] == "28.1"


def test_null_repayment_leaves_cost_and_percent_unknown() -> None:
    calculated = calculate_fields(
        {
            "sanctioned_amount": _money("15000.00"),
            "disbursed_amount": _money("13230.00"),
            "repayment_amount": _money(None),
        }
    )

    assert calculated["implied_fees_total"]["value"] == "1770.00"
    assert calculated["implied_cost"]["value"] is None
    assert calculated["extra_percent"]["value"] is None


def test_fee_plus_gst_requires_both_inputs() -> None:
    both = calculate_fields(
        {
            "processing_fee": _money("1000.00"),
            "gst_amount": _money("180.00"),
        }
    )
    fee_only = calculate_fields(
        {
            "processing_fee": _money("1000.00"),
            "gst_amount": _money(None),
        }
    )
    gst_only = calculate_fields(
        {
            "processing_fee": _money(None),
            "gst_amount": _money("180.00"),
        }
    )

    assert both["fee_plus_gst"] == {
        "value": "1180.00",
        "currency": "INR",
        "source": "calculated",
    }
    assert fee_only["fee_plus_gst"]["value"] is None
    assert fee_only["fee_plus_gst"]["source"] == "unknown"
    assert gst_only["fee_plus_gst"]["value"] is None


def test_listed_fees_are_not_copied_into_cost_or_sanctioned() -> None:
    calculated = calculate_fields(
        {
            "sanctioned_amount": _money("15000.00"),
            "disbursed_amount": _money("13230.00"),
            "processing_fee": _money("1000.00"),
            "gst_amount": _money("180.00"),
            "repayment_amount": _money("16950.00"),
        }
    )

    assert calculated["implied_fees_total"]["value"] == "1770.00"
    assert calculated["fee_plus_gst"]["value"] == "1180.00"
    assert calculated["implied_cost"]["value"] == "3720.00"
    assert calculated["extra_percent"]["value"] == "28.1"


def test_user_corrected_inputs_are_used_and_calculated_inputs_are_not() -> None:
    corrected = calculate_fields(
        {
            "sanctioned_amount": _money("15000.00", source="user_corrected"),
            "disbursed_amount": _money("13230.00"),
        }
    )
    reused = calculate_fields(
        {
            "sanctioned_amount": _money("15000.00", source="calculated"),
            "disbursed_amount": _money("13230.00"),
        }
    )

    assert corrected["implied_fees_total"]["value"] == "1770.00"
    assert reused["implied_fees_total"]["value"] is None
    assert reused["implied_fees_total"]["source"] == "unknown"


def test_mixed_currency_does_not_produce_a_difference() -> None:
    calculated = calculate_fields(
        {
            "sanctioned_amount": _money("15000.00", currency="INR"),
            "disbursed_amount": _money("13230.00", currency="USD"),
            "repayment_amount": _money("16950.00", currency="USD"),
        }
    )

    assert calculated["implied_fees_total"]["value"] is None
    assert calculated["implied_cost"]["value"] == "3720.00"
    assert calculated["implied_cost"]["currency"] == "USD"
    assert calculated["extra_percent"]["value"] == "28.1"


def test_subtraction_stays_exact_without_float_residue() -> None:
    calculated = calculate_fields(
        {
            "sanctioned_amount": _money("0.30"),
            "disbursed_amount": _money("0.10"),
        }
    )

    assert calculated["implied_fees_total"]["value"] == "0.20"
    assert parse_decimal_money("15000") == Decimal("15000.00")
    assert parse_decimal_money(13230) == Decimal("13230.00")
    assert parse_decimal_money(None) is None


def test_float_money_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_decimal_money(15000.5)
    with pytest.raises(ValueError):
        parse_decimal_money(15000.0)


def test_sanctioned_short_keeps_amounts_that_are_not_whole_thousands() -> None:
    assert display_sanctioned_short("13230.00") == "13230"
    assert display_sanctioned_short("9999.00") == "9999"
    assert display_sanctioned_short("10000.00") == "10k"
    assert display_sanctioned_short("15000.50") == "15000.50"
    assert display_sanctioned_short(None) is None
    assert display_sanctioned_short({"value": None, "source": "unknown"}) is None


def test_date_short_form_adds_the_year_only_outside_the_current_year() -> None:
    today = date(2026, 9, 24)

    assert display_date_short("2026-06-17", today=today) == "jun 17"
    assert display_date_short("2026-07-01", today=today) == "jul 1"
    assert display_date_short("2025-06-17", today=today) == "jun 17, 2025"
    assert display_date_short(None, today=today) is None


def test_partial_date_uses_the_analysis_year_inside_eighteen_months() -> None:
    created_at = date(2026, 1, 10)

    resolved = resolve_date("jun 17", created_at=created_at)
    december = resolve_date("20 dec", created_at=created_at)

    assert resolved.value == "2026-06-17"
    assert resolved.review_flag is None
    assert december.value == "2026-12-20"
    assert december.review_flag is None
    assert within_eighteen_months(date(2025, 3, 24), date(2026, 9, 24)) is True
    assert within_eighteen_months(date(2025, 3, 23), date(2026, 9, 24)) is False


def test_unresolved_dates_stay_null_and_are_flagged() -> None:
    created_at = date(2026, 6, 1)

    impossible = resolve_date("feb 31", created_at=created_at)
    numeric = resolve_date("06/17", created_at=created_at)
    missing_anchor = resolve_date("jun 17", created_at=None)
    blank = resolve_date("  ", created_at=created_at)
    stored = resolve_date("2024-02-29", created_at=created_at)

    assert impossible.value is None
    assert impossible.review_flag == AMBIGUOUS_DATE
    assert numeric.value is None
    assert numeric.review_flag == AMBIGUOUS_DATE
    assert missing_anchor.value is None
    assert missing_anchor.review_flag == AMBIGUOUS_DATE
    assert blank.value is None
    assert blank.review_flag is None
    assert stored.value == "2024-02-29"
    assert stored.review_flag is None

    day_first = resolve_date("17/06/2026", created_at=created_at)
    dashed = resolve_date("17-06-2026", created_at=created_at)
    month_first = resolve_date("06/17/2026", created_at=created_at)
    assert day_first.value == "2026-06-17"
    assert day_first.review_flag is None
    assert dashed.value == "2026-06-17"
    assert month_first.value == "2026-06-17"
