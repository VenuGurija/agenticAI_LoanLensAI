"""Review flags and terminal analysis status (SPEC §12, §18)."""

import copy

from app.services.review import (
    AMBIGUOUS_DATE,
    CURRENCY_UNKNOWN,
    EXTRACTION_INVALID,
    LOW_CONFIDENCE_FIELD,
    NO_LENDER,
    NOT_LOAN_DOCUMENT,
    UNCLEAR_DOCUMENT,
    VALIDATION_FAILED_PREFIX,
    apply_review,
)
from app.services.vision.contracts import DocumentClass


def _money(
    value: str | None,
    *,
    currency: str | None = "INR",
    confidence: str = "high",
    source: str = "extracted",
) -> dict:
    if value is None:
        return {
            "value": None,
            "currency": None,
            "source": "unknown",
            "confidence": "unknown",
        }
    return {
        "value": value,
        "currency": currency,
        "source": source,
        "confidence": confidence,
    }


def _text(value: str | None, *, confidence: str = "high", source: str = "extracted") -> dict:
    if value is None:
        return {"value": None, "source": "unknown", "confidence": "unknown"}
    return {"value": value, "source": source, "confidence": confidence}


def _date(value: str | None, *, confidence: str = "high", source: str = "extracted") -> dict:
    if value is None:
        return {"value": None, "source": "unknown", "confidence": "unknown"}
    return {"value": value, "source": source, "confidence": confidence}


def _clean_loan() -> dict:
    return {
        "loan_internal_id": "lon_clean",
        "image_ids": ["img_loan"],
        "fields": {
            "lender_brand": _text("Dhanrishi"),
            "legal_lending_entity": _text("Gagandeep", confidence="medium"),
            "sanctioned_amount": _money("15000.00"),
            "disbursed_amount": _money("13230.00"),
            "repayment_amount": _money("16950.00"),
            "disbursement_date": _date("2026-06-17"),
            "repayment_date": _date("2026-06-30"),
            "processing_fee": _money("1500.00"),
            "sanction_date": _date("2026-06-01"),
        },
        "review_flags": [],
        "validations": [],
        "validation_summary": "ok",
    }


def _groups(*loans: dict) -> list[dict]:
    return [{"lender_brand": "Dhanrishi", "loans": list(loans)}]


def test_null_disbursed_amount_needs_review() -> None:
    loan = _clean_loan()
    loan["fields"]["disbursed_amount"] = _money(None)
    fields = copy.deepcopy(loan["fields"])

    result = apply_review(_groups(loan))

    assert loan["needs_review"] is True
    assert result.status == "completed_needs_review"
    assert loan["fields"]["disbursed_amount"]["value"] is None
    assert loan["fields"] == fields


def test_clean_loan_is_completed() -> None:
    loan = _clean_loan()
    fields = copy.deepcopy(loan["fields"])

    result = apply_review(
        _groups(loan),
        images=[{"image_id": "img_loan", "document_class": DocumentClass.LOAN_DOCUMENT}],
    )

    assert loan["needs_review"] is False
    assert loan["review_flags"] == []
    assert result.status == "completed"
    assert loan["fields"] == fields


def test_low_or_unknown_confidence_on_a_shown_field_is_flagged() -> None:
    low = _clean_loan()
    low["fields"]["sanctioned_amount"] = _money("15000.00", confidence="low")
    unknown = _clean_loan()
    unknown["fields"]["repayment_amount"] = _money("16950.00", confidence="unknown")
    missing = _clean_loan()
    missing["fields"]["lender_brand"] = {"value": "Dhanrishi", "source": "extracted"}

    apply_review(_groups(low))
    apply_review(_groups(unknown))
    apply_review(_groups(missing))

    assert low["review_flags"] == [LOW_CONFIDENCE_FIELD]
    assert low["needs_review"] is True
    assert unknown["review_flags"] == [LOW_CONFIDENCE_FIELD]
    assert missing["review_flags"] == [LOW_CONFIDENCE_FIELD]


def test_medium_confidence_and_hidden_low_confidence_are_not_flagged() -> None:
    medium = _clean_loan()
    medium["fields"]["disbursed_amount"] = _money("13230.00", confidence="medium")
    hidden = _clean_loan()
    hidden["fields"]["sanctioned_amount"] = {
        "value": None,
        "currency": None,
        "source": "unknown",
        "confidence": "low",
    }
    hidden["validation_summary"] = "ok"
    fee = _clean_loan()
    fee["fields"]["processing_fee"] = _money("1500.00", confidence="low")

    apply_review(_groups(medium))
    apply_review(_groups(fee))
    apply_review(_groups(hidden))

    assert medium["review_flags"] == []
    assert medium["needs_review"] is False
    assert fee["review_flags"] == []
    assert fee["needs_review"] is False
    assert LOW_CONFIDENCE_FIELD not in hidden["review_flags"]
    assert hidden["needs_review"] is True


def test_merge_and_grouping_flags_are_kept() -> None:
    conflict = _clean_loan()
    conflict["review_flags"] = ["FIELD_CONFLICT"]
    grouping = _clean_loan()
    grouping["review_flags"] = ["GROUPING_CONFLICT"]

    apply_review(_groups(conflict))
    apply_review(_groups(grouping))

    assert conflict["review_flags"] == ["FIELD_CONFLICT"]
    assert conflict["needs_review"] is True
    assert grouping["review_flags"] == ["GROUPING_CONFLICT"]
    assert grouping["needs_review"] is True


def test_ambiguous_date_when_year_inference_is_refused() -> None:
    refused = _clean_loan()
    refused["fields"]["sanction_date"] = {
        "value": None,
        "source": "unknown",
        "confidence": "unknown",
        "review_flag": AMBIGUOUS_DATE,
    }
    partial = _clean_loan()
    partial["fields"]["repayment_date"] = _date("jun 17")
    inferred = _clean_loan()
    inferred["fields"]["repayment_date"] = _date("jun 17")
    invalid = _clean_loan()
    invalid["fields"]["disbursement_date"] = _date("2026-02-31")

    apply_review(_groups(refused))
    apply_review(_groups(partial))
    apply_review(_groups(inferred), created_at="2026-06-01T00:00:00Z")
    apply_review(_groups(invalid), created_at="2026-06-01T00:00:00Z")

    assert refused["review_flags"] == [AMBIGUOUS_DATE]
    assert refused["needs_review"] is True
    assert partial["review_flags"] == [AMBIGUOUS_DATE]
    assert inferred["review_flags"] == []
    assert inferred["needs_review"] is False
    assert invalid["review_flags"] == [AMBIGUOUS_DATE]
    assert invalid["fields"]["disbursement_date"]["value"] == "2026-02-31"


def test_amount_without_currency_is_flagged() -> None:
    loan = _clean_loan()
    loan["fields"]["disbursed_amount"] = _money("13230.00", currency=None)
    mixed = _clean_loan()
    mixed["review_flags"] = [CURRENCY_UNKNOWN]
    penalty = _clean_loan()
    penalty["fields"]["penalties"] = [
        {"name": "Late fee", "amount": _money("100.00", currency=None)}
    ]

    apply_review(_groups(loan))
    apply_review(_groups(mixed))
    apply_review(_groups(penalty))

    assert loan["review_flags"] == [CURRENCY_UNKNOWN]
    assert loan["fields"]["disbursed_amount"]["value"] == "13230.00"
    assert mixed["review_flags"] == [CURRENCY_UNKNOWN]
    assert penalty["review_flags"] == [CURRENCY_UNKNOWN]


def test_document_class_and_extraction_failure_flags() -> None:
    loan = _clean_loan()
    unclear_only = apply_review(
        [],
        images=[{"image_id": "img_blur", "document_class": "unclear"}],
    )
    not_loan = apply_review(
        _groups(loan),
        images=[
            {"image_id": "img_loan", "document_class": DocumentClass.LOAN_DOCUMENT},
            {"image_id": "img_receipt", "document_class": "not_loan_document"},
        ],
    )
    failed = _clean_loan()
    failed["loan_internal_id"] = "lon_other"
    extraction = apply_review(
        _groups(failed),
        images=[
            {
                "image_id": "img_broken",
                "document_class": DocumentClass.LOAN_DOCUMENT,
                "extraction_invalid": True,
            }
        ],
    )

    assert unclear_only.status == "completed_needs_review"
    assert unclear_only.image_flags == [UNCLEAR_DOCUMENT]
    assert not_loan.image_flags == [NOT_LOAN_DOCUMENT]
    assert loan["review_flags"] == [NOT_LOAN_DOCUMENT]
    assert loan["needs_review"] is True
    assert not_loan.status == "completed_needs_review"
    assert extraction.image_flags == [EXTRACTION_INVALID]
    assert failed["review_flags"] == [EXTRACTION_INVALID]
    assert extraction.status == "completed_needs_review"


def test_failed_validation_and_incomplete_summary_need_review() -> None:
    failed = _clean_loan()
    failed["validations"] = [
        {"id": "V_DISBURSED_LTE_SANCTIONED", "status": "failed", "message": "Too high."}
    ]
    failed["validation_summary"] = "failed"
    failed["review_flags"] = ["FIELD_CONFLICT", "VALIDATION_FAILED:V_POSITIVE_SANCTIONED"]
    incomplete = _clean_loan()
    incomplete["validation_summary"] = "incomplete"

    apply_review(_groups(failed))
    apply_review(_groups(incomplete))

    assert failed["review_flags"] == [
        "FIELD_CONFLICT",
        f"{VALIDATION_FAILED_PREFIX}V_DISBURSED_LTE_SANCTIONED",
    ]
    assert failed["needs_review"] is True
    assert incomplete["review_flags"] == []
    assert incomplete["needs_review"] is True


def test_loan_with_no_brand_and_no_legal_entity_is_flagged() -> None:
    loan = _clean_loan()
    loan["fields"]["lender_brand"] = _text(None)
    loan["fields"]["legal_lending_entity"] = _text("   ")

    apply_review(_groups(loan))

    assert NO_LENDER in loan["review_flags"]
    assert loan["needs_review"] is True


def test_completed_requires_one_loan_and_none_needing_review() -> None:
    clean = _clean_loan()
    missing = _clean_loan()
    missing["loan_internal_id"] = "lon_missing"
    missing["fields"]["disbursed_amount"] = _money(None)

    assert apply_review([]).status == "completed_needs_review"
    assert apply_review(_groups(clean, missing)).status == "completed_needs_review"
    assert clean["needs_review"] is False
    assert missing["needs_review"] is True
    again = apply_review(_groups(clean))
    assert again.status == "completed"
    assert clean["review_flags"] == []
