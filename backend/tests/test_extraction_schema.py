"""Extraction schema: SPEC §9.7 sample, null money, no float money."""

import pytest
from pydantic import ValidationError

from app.schemas.extraction import (
    ExtractedLoan,
    ImageExtractResult,
    MoneyField,
    PenaltyField,
)

# SPEC §9.7 sample. Source is omitted on purpose; the schema fills it.
_SAMPLE = {
    "image_id": "img_01HZX...",
    "loans": [
        {
            "lender_brand": {"value": "Dhanrishi", "confidence": "high"},
            "legal_lending_entity": {"value": "Gagandeep", "confidence": "medium"},
            "borrower_name": {"value": None, "confidence": "unknown"},
            "loan_id": {"value": None, "confidence": "unknown"},
            "sanctioned_amount": {
                "value": "15000.00",
                "currency": "INR",
                "confidence": "high",
            },
            "disbursed_amount": {
                "value": "13230.00",
                "currency": "INR",
                "confidence": "high",
            },
            "processing_fee": {"value": None, "currency": None, "confidence": "unknown"},
            "gst_amount": {"value": None, "currency": None, "confidence": "unknown"},
            "interest_rate": {"value": None, "unit": "percent", "confidence": "unknown"},
            "apr": {"value": None, "unit": "percent", "confidence": "unknown"},
            "tenure": {"value": None, "unit": None, "confidence": "unknown"},
            "sanction_date": {"value": None, "confidence": "unknown"},
            "disbursement_date": {"value": "2026-06-17", "confidence": "high"},
            "repayment_amount": {
                "value": "16950.00",
                "currency": "INR",
                "confidence": "high",
            },
            "repayment_date": {"value": "2026-06-30", "confidence": "high"},
            "repayment_frequency": {"value": "one_time", "confidence": "medium"},
            "penalties": [],
            "same_loan_as_image_ids": [],
        }
    ],
}


def test_spec_sample_validates() -> None:
    parsed = ImageExtractResult.model_validate(_SAMPLE)
    loan = parsed.loans[0]

    assert parsed.image_id == "img_01HZX..."
    assert loan.lender_brand.value == "Dhanrishi"
    assert loan.lender_brand.source == "extracted"
    assert loan.lender_brand.confidence == "high"
    assert loan.legal_lending_entity.value == "Gagandeep"
    assert loan.legal_lending_entity.confidence == "medium"
    assert loan.borrower_name.value is None
    assert loan.borrower_name.source == "unknown"
    assert loan.loan_id.value is None
    assert loan.sanctioned_amount.value == "15000.00"
    assert loan.sanctioned_amount.currency == "INR"
    assert loan.disbursed_amount.value == "13230.00"
    assert loan.processing_fee.value is None
    assert loan.processing_fee.currency is None
    assert loan.processing_fee.source == "unknown"
    assert loan.gst_amount.value is None
    assert loan.interest_rate.value is None
    assert loan.interest_rate.unit == "percent"
    assert loan.interest_rate.source == "unknown"
    assert loan.apr.value is None
    assert loan.tenure.value is None
    assert loan.tenure.unit is None
    assert loan.sanction_date.value is None
    assert loan.disbursement_date.value == "2026-06-17"
    assert loan.disbursement_date.source == "extracted"
    assert loan.repayment_amount.value == "16950.00"
    assert loan.repayment_date.value == "2026-06-30"
    assert loan.repayment_frequency.value == "one_time"
    assert loan.repayment_frequency.source == "extracted"
    assert loan.penalties == []
    assert loan.same_loan_as_image_ids == []
    assert loan.other_fees_total.value is None
    assert loan.other_fees_total.source == "unknown"


def test_missing_money_fields_are_null_not_zero() -> None:
    parsed = ImageExtractResult.model_validate(
        {
            "image_id": "img_partial",
            "loans": [{"lender_brand": {"value": "Dhanrishi", "confidence": "high"}}],
        }
    )
    loan = parsed.loans[0]
    dumped = loan.model_dump()

    for name in (
        "sanctioned_amount",
        "disbursed_amount",
        "processing_fee",
        "gst_amount",
        "other_fees_total",
        "repayment_amount",
    ):
        assert dumped[name]["value"] is None
        assert dumped[name]["value"] != 0
        assert dumped[name]["value"] != "0"
        assert dumped[name]["value"] != "0.00"
        assert dumped[name]["source"] == "unknown"

    assert loan.borrower_name.value is None
    assert loan.interest_rate.value is None
    assert loan.sanction_date.value is None
    assert loan.tenure.value is None
    assert loan.repayment_frequency.value is None


def test_empty_loan_list_is_valid() -> None:
    parsed = ImageExtractResult.model_validate({"image_id": "img_none", "loans": []})
    assert parsed.loans == []


def test_money_coerces_int_and_decimal_string_and_rejects_float() -> None:
    from_int = MoneyField.model_validate({"value": 15000, "confidence": "high"})
    assert from_int.value == "15000.00"
    assert isinstance(from_int.value, str)
    assert from_int.source == "extracted"

    from_string = MoneyField.model_validate({"value": "13230", "currency": "INR"})
    assert from_string.value == "13230.00"

    one_place = MoneyField.model_validate({"value": "13230.5", "confidence": "high"})
    assert one_place.value == "13230.50"

    explicit_zero = MoneyField.model_validate({"value": 0, "source": "extracted"})
    assert explicit_zero.value == "0.00"

    with pytest.raises(ValidationError):
        MoneyField.model_validate({"value": 15000.5, "confidence": "high"})

    with pytest.raises(ValidationError):
        MoneyField.model_validate({"value": 15000.0, "currency": "INR"})


def test_unknown_source_rejects_a_value() -> None:
    with pytest.raises(ValidationError):
        MoneyField.model_validate(
            {"value": "10.00", "source": "unknown", "confidence": "unknown"}
        )

    empty = MoneyField.model_validate(
        {"value": None, "source": "unknown", "confidence": "unknown"}
    )
    assert empty.value is None


def test_source_literals_and_wrapper_evidence() -> None:
    corrected = MoneyField.model_validate(
        {
            "value": "15000.00",
            "currency": "INR",
            "source": "user_corrected",
            "confidence": "high",
            "evidence": {"image_id": "img_01HZX...", "label_text": "Loan Amount"},
        }
    )
    assert corrected.source == "user_corrected"
    assert corrected.evidence is not None
    assert corrected.evidence.label_text == "Loan Amount"

    calculated = MoneyField.model_validate(
        {"value": "1770.00", "source": "calculated", "confidence": "medium"}
    )
    assert calculated.source == "calculated"
    assert calculated.currency is None

    with pytest.raises(ValidationError):
        MoneyField.model_validate({"value": "1.00", "source": "guessed"})


def test_invalid_date_frequency_and_float_rate_fail() -> None:
    with pytest.raises(ValidationError):
        ExtractedLoan.model_validate(
            {"disbursement_date": {"value": "jun 17", "confidence": "high"}}
        )
    with pytest.raises(ValidationError):
        ExtractedLoan.model_validate(
            {"repayment_frequency": {"value": "daily", "confidence": "high"}}
        )
    with pytest.raises(ValidationError):
        ExtractedLoan.model_validate(
            {"interest_rate": {"value": 29.95, "unit": "percent", "confidence": "high"}}
        )

    loan = ExtractedLoan.model_validate(
        {
            "interest_rate": {"value": "29.95", "unit": "percent", "confidence": "high"},
            "tenure": {"value": 14, "unit": "days", "confidence": "high"},
            "penalties": [
                {
                    "name": "Late fee",
                    "amount": {"value": "100.00", "currency": "INR", "confidence": "medium"},
                    "rate": None,
                    "description": None,
                }
            ],
        }
    )
    assert loan.interest_rate.value == "29.95"
    assert loan.tenure.value == 14
    assert loan.tenure.unit == "days"
    assert loan.penalties[0] == PenaltyField(
        name="Late fee",
        amount=MoneyField(
            value="100.00",
            currency="INR",
            source="extracted",
            confidence="medium",
        ),
    )


def test_extra_money_precision_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MoneyField.model_validate({"value": "10.555", "confidence": "low"})
