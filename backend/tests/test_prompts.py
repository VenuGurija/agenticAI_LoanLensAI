"""Vision contracts and prompts: nulls allowed, fees are not calculated."""

import json

import pytest
from pydantic import ValidationError

from app.schemas.extraction import ExtractedLoan, ImageExtractResult
from app.services.vision.contracts import (
    CLASSIFY_SCHEMA_NAME,
    EXTRACT_SCHEMA_NAME,
    ClassifyContract,
    ExtractContract,
    ImageClassification,
    VisionImageExtract,
    VisionLoan,
    json_schema_for,
    validate_contract,
)
from app.services.vision.prompts import NULL_FIELD, prompt_for

# SPEC §8.1
_CLASSIFY_ITEM = {
    "image_id": "img_01HZX...",
    "document_class": "loan_document",
    "confidence": "high",
    "page_role": "unknown",
    "notes": None,
}

# SPEC §9.7. Source is omitted; the vision contract does not ask for it.
_EXTRACT_ITEM = {
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

_CALCULATED_FIELDS = (
    "implied_fees_total",
    "fee_plus_gst",
    "implied_cost",
    "extra_percent",
    "validation_summary",
)

_FEE_REQUESTS = (
    "calculate fees",
    "calculate the fees",
    "calculate processing fee",
    "calculate gst",
    "calculate apr",
    "compute the fees",
    "derive fees",
    "estimate fees",
)


def test_classify_and_extract_contracts_are_separate() -> None:
    classify = json_schema_for(CLASSIFY_SCHEMA_NAME)
    extract = json_schema_for(EXTRACT_SCHEMA_NAME)

    assert classify["title"] == "classify"
    assert extract["title"] == "extract"
    assert classify != extract

    classify_text = json.dumps(classify)
    extract_text = json.dumps(extract)
    assert "loan_document" in classify_text
    assert "not_loan_document" in classify_text
    assert "unclear" in classify_text
    assert "document_class" in classify_text
    assert "document_class" not in extract_text
    assert "sanctioned_amount" in extract_text
    assert "sanctioned_amount" not in classify_text
    for name in _CALCULATED_FIELDS:
        assert name not in extract_text
        assert name not in classify_text


def test_classify_contract_matches_spec_example() -> None:
    item = ImageClassification.model_validate(_CLASSIFY_ITEM)
    batch = validate_contract("classify", {"results": [_CLASSIFY_ITEM]})

    assert item.document_class == "loan_document"
    assert item.confidence == "high"
    assert item.page_role == "unknown"
    assert item.notes is None
    assert isinstance(batch, ClassifyContract)
    assert batch.results[0].image_id == "img_01HZX..."


def test_missing_page_role_stays_unknown() -> None:
    item = ImageClassification.model_validate(
        {
            "image_id": "img_01",
            "document_class": "unclear",
        }
    )
    assert item.page_role == "unknown"
    assert item.confidence == "unknown"
    assert item.notes is None


def test_extract_contract_matches_spec_example_and_leaves_fees_null() -> None:
    parsed = validate_contract("extract", {"results": [_EXTRACT_ITEM]})
    assert isinstance(parsed, ExtractContract)
    loan = parsed.results[0].loans[0]

    assert loan.lender_brand.value == "Dhanrishi"
    assert loan.sanctioned_amount.value == "15000.00"
    assert loan.disbursed_amount.value == "13230.00"
    assert loan.repayment_amount.value == "16950.00"
    assert loan.processing_fee.value is None
    assert loan.processing_fee.currency is None
    assert loan.gst_amount.value is None
    assert loan.apr.value is None
    assert loan.other_fees_total.value is None
    assert loan.penalties == []


def test_omitted_extract_fields_are_null_not_zero() -> None:
    image = VisionImageExtract.model_validate(
        {"image_id": "img_01", "loans": [{}]}
    )
    loan = image.loans[0]
    assert loan.sanctioned_amount.value is None
    assert loan.processing_fee.value is None
    assert loan.gst_amount.value is None
    assert loan.repayment_amount.value is None
    assert loan.penalties == []


def test_extract_loan_fields_match_canonical_names() -> None:
    assert set(VisionLoan.model_fields) == set(ExtractedLoan.model_fields)
    assert set(VisionImageExtract.model_fields) == set(ImageExtractResult.model_fields)


def test_unknown_document_class_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ImageClassification.model_validate(
            {
                "image_id": "img_01",
                "document_class": "bank_statement",
            }
        )


def test_calculated_field_is_rejected_on_extract_loan() -> None:
    payload = {
        "results": [
            {
                "image_id": "img_01",
                "loans": [{"implied_fees_total": {"value": "100.00"}}],
            }
        ]
    }
    with pytest.raises(ValidationError):
        validate_contract("extract", payload)


def test_unknown_contract_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown vision contract"):
        json_schema_for("summarize")


def test_prompt_contains_null_instruction_and_does_not_ask_to_calculate_fees() -> None:
    extract_prompt = prompt_for("extract")
    classify_prompt = prompt_for("classify")

    assert "If a field is not clearly present, return null" in extract_prompt
    assert NULL_FIELD in extract_prompt
    assert NULL_FIELD in classify_prompt
    assert "Do not compute fees, GST, APR, or remaining principal" in extract_prompt
    assert "Do not copy values between different loans." in extract_prompt
    assert "other users' data" in extract_prompt

    for prompt in (extract_prompt, classify_prompt):
        lowered = prompt.lower()
        assert "calculate" not in lowered
        for phrase in _FEE_REQUESTS:
            assert phrase not in lowered

    assert extract_prompt != classify_prompt


def test_unknown_task_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown vision task"):
        prompt_for("summarize")  # type: ignore[arg-type]


def test_single_classify_result_keeps_the_server_image_id() -> None:
    from app.services.classification import parse_classification
    from app.services.vision.base import VisionResponseInvalidError

    parsed = parse_classification(
        {"results": [{"image_id": "page-1", "document_class": "loan_document"}]},
        "img_server",
    )
    assert parsed.image_id == "img_server"
    assert parsed.document_class.value == "loan_document"

    with pytest.raises(VisionResponseInvalidError):
        parse_classification(
            {
                "results": [
                    {"image_id": "page-1", "document_class": "loan_document"},
                    {"image_id": "page-2", "document_class": "unclear"},
                ]
            },
            "img_server",
        )


def test_single_extract_result_keeps_the_server_image_id() -> None:
    from app.services.extraction import parse_extract_response

    parsed = parse_extract_response(
        {"results": [{"image_id": "page-1", "loans": []}]},
        "img_server",
    )
    assert parsed.image_id == "img_server"
    assert parsed.loans == []

    dated = parse_extract_response(
        {
            "results": [
                {
                    "image_id": "page-1",
                    "loans": [
                        {"repayment_date": {"value": "17/06/2026", "confidence": "high"}}
                    ],
                }
            ]
        },
        "img_server",
    )
    assert dated.loans[0].repayment_date.value == "2026-06-17"
