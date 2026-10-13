"""Printed Ollama extract shape is normalized. Missing money stays null."""

import asyncio
import json

import httpx
import pytest

from app.core.config import Settings
from app.models.database import AnalysisRow
from app.schemas.vision import VisionImage, VisionRequest
from app.services.classification import _finish_failed
from app.services.extraction import normalize_printed_extract, parse_extract_response
from app.services.vision.contracts import validate_contract
from app.services.vision.base import VisionResponseInvalidError
from app.services.vision.openai_compatible import OpenAICompatibleVisionProvider

# The rejected gemma3:4b extract: bare lender string, null identity fields,
# tenure text, a spaced money amount, a percent sign, and a day-first date.
OLLAMA_EXTRACT = {
    "results": [
        {
            "image_id": "img_loan",
            "loans": [
                {
                    "lender_brand": "VN CREDITS LIMITED",
                    "legal_lending_entity": None,
                    "borrower_name": None,
                    "loan_id": None,
                    "tenure": {"value": "31 days", "unit": "days"},
                    "sanctioned_amount": {"value": "30 000.00", "currency": "AUD"},
                    "interest_rate": {"value": "0.00%"},
                    "sanction_date": {"value": "30-03-2020"},
                    "gst_amount": None,
                }
            ],
        }
    ]
}


def test_ollama_extract_shape_parses_without_inventing_money() -> None:
    parsed = parse_extract_response(OLLAMA_EXTRACT, "img_server")

    assert parsed.image_id == "img_server"
    loan = parsed.loans[0]
    assert loan.lender_brand.value == "VN CREDITS LIMITED"
    assert loan.legal_lending_entity.value is None
    assert loan.borrower_name.value is None
    assert loan.loan_id.value is None
    assert loan.tenure.value == 31
    assert loan.tenure.unit == "days"
    assert loan.sanctioned_amount.value == "30000.00"
    assert loan.sanctioned_amount.currency == "AUD"
    assert loan.interest_rate.value == "0"
    assert loan.interest_rate.unit == "percent"
    assert loan.sanction_date.value == "2020-03-30"
    assert loan.disbursed_amount.value is None
    assert loan.disbursed_amount.currency is None
    assert loan.gst_amount.value is None
    assert loan.processing_fee.value is None
    assert loan.repayment_amount.value is None


def test_tenure_months_parse_and_unclear_tenure_stays_null() -> None:
    parsed = parse_extract_response(
        {
            "results": [
                {
                    "image_id": "img_loan",
                    "loans": [
                        {"tenure": {"value": "12 months", "unit": "months"}},
                        {"tenure": {"value": "about a year"}},
                    ],
                }
            ]
        },
        "img_server",
    )

    assert parsed.loans[0].tenure.value == 12
    assert parsed.loans[0].tenure.unit == "months"
    assert parsed.loans[1].tenure.value is None
    assert parsed.loans[0].sanctioned_amount.value is None


def test_grouping_comma_does_not_change_the_amount() -> None:
    parsed = parse_extract_response(
        {
            "results": [
                {
                    "image_id": "img_loan",
                    "loans": [
                        {"sanctioned_amount": {"value": "30,000.00", "currency": "AUD"}}
                    ],
                }
            ]
        },
        "img_server",
    )

    assert parsed.loans[0].sanctioned_amount.value == "30000.00"
    assert parsed.loans[0].sanctioned_amount.currency == "AUD"


def _provider(payloads: list[object]) -> tuple[OpenAICompatibleVisionProvider, list[httpx.Request]]:
    pending = iter(payloads)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = next(pending)
        if isinstance(body, str):
            content = body
        else:
            content = json.dumps(body)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    provider = OpenAICompatibleVisionProvider(
        Settings(
            vision_provider="openai_compatible",
            vision_api_key="sk-test-secret-value",
            vision_base_url="https://vision.example.test/v1",
            vision_model="gemma3:4b",
            loanlens_vision_timeout_seconds=60,
        ),
        transport=httpx.MockTransport(handler),
    )
    return provider, seen


def _extract_request() -> VisionRequest:
    return VisionRequest(
        analysis_id="anl_printed",
        task="extract",
        images=[VisionImage(image_id="img_loan", media_type="image/png", bytes=b"png")],
        response_schema_name="extract",
        hint=None,
    )


def test_openai_numeric_extract_parses_without_changing_amounts() -> None:
    parsed = parse_extract_response(
        {
            "results": [
                {
                    "image_id": "img_loan",
                    "loans": [
                        {
                            "lender_brand": "NextBigLoan",
                            "sanctioned_amount": {"value": 30000, "currency": "INR"},
                            "disbursed_amount": {"value": 26637, "currency": "INR"},
                            "processing_fee": {"value": 2580, "currency": "INR"},
                            "gst_amount": {"value": 513, "currency": "INR"},
                            "other_fees_total": None,
                            "interest_rate": "0.95% Per Day",
                            "apr": None,
                            "tenure": "30 days",
                            "sanction_date": "30th Sep, 2026",
                            "disbursement_date": None,
                            "repayment_amount": {"value": 38550, "currency": "INR"},
                            "repayment_date": "30th Oct, 2026",
                            "repayment_frequency": None,
                            "penalties": None,
                        }
                    ],
                }
            ]
        },
        "img_server",
    )

    loan = parsed.loans[0]
    assert loan.lender_brand.value == "NextBigLoan"
    assert loan.sanctioned_amount.value == "30000.00"
    assert loan.sanctioned_amount.currency == "INR"
    assert loan.disbursed_amount.value == "26637.00"
    assert loan.processing_fee.value == "2580.00"
    assert loan.gst_amount.value == "513.00"
    assert loan.other_fees_total.value is None
    assert loan.interest_rate.value == "0.95"
    assert loan.interest_rate.unit == "percent"
    assert loan.apr.value is None
    assert loan.tenure.value == 30
    assert loan.tenure.unit == "days"
    assert loan.sanction_date.value == "2026-09-30"
    assert loan.repayment_amount.value == "38550.00"
    assert loan.repayment_date.value == "2026-10-30"
    assert loan.repayment_frequency.value is None
    assert loan.penalties == []


def test_openai_numeric_extract_is_accepted_without_a_retry() -> None:
    body = {
        "results": [
            {
                "image_id": "img_loan",
                "loans": [
                    {
                        "sanctioned_amount": {"value": 30000, "currency": "INR"},
                        "interest_rate": "0.95% Per Day",
                        "tenure": "30 days",
                        "repayment_frequency": None,
                        "penalties": None,
                    }
                ],
            }
        ]
    }
    provider, seen = _provider([body])

    response = asyncio.run(provider.analyze_images(_extract_request()))

    assert len(seen) == 1
    parsed = parse_extract_response(response.raw_json, "img_loan")
    assert parsed.loans[0].sanctioned_amount.value == "30000.00"
    assert parsed.loans[0].disbursed_amount.value is None


def test_string_null_tenure_unit_becomes_json_null() -> None:
    normalized = normalize_printed_extract(
        {
            "results": [
                {
                    "image_id": "img_loan",
                    "loans": [
                        {"tenure": {"value": None, "unit": "null"}},
                        {"tenure": {"value": 30, "unit": "days"}},
                        {"interest_rate": {"value": None, "unit": "none"}},
                        {"repayment_frequency": {"value": "null"}},
                    ],
                }
            ]
        }
    )

    loans = normalized["results"][0]["loans"]
    assert loans[0]["tenure"]["unit"] is None
    assert loans[0]["tenure"]["value"] is None
    assert loans[1]["tenure"]["unit"] == "days"
    assert loans[1]["tenure"]["value"] == 30
    assert loans[2]["interest_rate"]["unit"] is None
    assert loans[3]["repayment_frequency"]["value"] is None
    contract = validate_contract("extract", normalized)
    assert contract.results[0].loans[0].tenure.unit is None
    assert contract.results[0].loans[0].tenure.value is None
    assert contract.results[0].loans[0].sanctioned_amount.value is None


def test_failed_draft_message_lists_loc_and_type_only() -> None:
    body = {
        "results": [
            {
                "image_id": "img_loan",
                "loans": [
                    {
                        "borrower_name": "Sample Borrower",
                        "loan_id": "LN-SAMPLE",
                        "sanctioned_amount": {"value": "15000", "currency": "INR"},
                        "tenure": {"value": None, "unit": "weeks"},
                    }
                ],
            }
        ]
    }
    provider, _seen = _provider([body] * 6)

    with pytest.raises(VisionResponseInvalidError) as raised:
        asyncio.run(provider.analyze_images(_extract_request()))

    row = AnalysisRow(
        id="anl_failed",
        book_id="bok_failed",
        status="running",
        created_at="2026-10-04T09:00:44Z",
    )
    _finish_failed(row, raised.value.code, failures=raised.value.failures)

    message = row.error_message or ""
    assert row.error_code == "VISION_INVALID_RESPONSE"
    assert message.startswith("The vision service returned an invalid response.")
    assert "tenure.unit" in message
    assert "literal_error" in message
    assert "15000" not in message
    assert "Sample Borrower" not in message
    assert "LN-SAMPLE" not in message
    assert "weeks" not in message


def test_blank_extra_result_is_removed_before_validation() -> None:
    image_id = "img_01M431CZXC3M1J1152MDJ0171P"
    normalized = normalize_printed_extract(
        {
            "results": [
                {"image_id": image_id, "loans": [{"lender_brand": "NextBigLoan"}]},
                {"image_id": None, "loans": []},
            ]
        }
    )

    assert len(normalized["results"]) == 1
    assert normalized["results"][0]["image_id"] == image_id
    assert normalized["results"][0]["loans"][0]["lender_brand"]["value"] == "NextBigLoan"
    contract = validate_contract("extract", normalized)
    assert len(contract.results) == 1
    assert contract.results[0].image_id == image_id


def test_result_with_loans_and_a_null_image_id_is_kept() -> None:
    normalized = normalize_printed_extract(
        {
            "results": [
                {"image_id": "img_loan", "loans": [{"loan_id": "A"}]},
                {"image_id": None, "loans": [{"loan_id": "B"}]},
            ]
        }
    )

    assert len(normalized["results"]) == 2
    assert normalized["results"][0]["loans"][0]["loan_id"]["value"] == "A"
    assert normalized["results"][1]["image_id"] is None
    assert normalized["results"][1]["loans"][0]["loan_id"]["value"] == "B"


def test_extract_prompt_has_a_null_example_and_not_the_schema() -> None:
    provider, seen = _provider([OLLAMA_EXTRACT])

    asyncio.run(provider.analyze_images(_extract_request()))

    body = seen[0].content.decode()
    text = json.loads(body)["messages"][1]["content"][0]["text"]
    assert "$defs" not in body
    assert "properties" not in body
    assert "json_schema_for" not in body
    assert "JSON schema:" not in body
    assert "Do not return the schema." in text
    assert "Return exactly one result for each named image_id, and do not append an extra result." in text
    assert "never a JSON number" in text
    assert "penalties is an array" in text
    assert "total amount to be paid by the borrower" in text
    assert "amount ((i)-(iii))" in text
    assert "Do not copy that number into interest_rate." in text
    assert "unless that figure is printed" in text
    assert "img_loan" in text
    assert '"value":null' in text
    assert "15000" not in text
    assert "30000" not in text
    for name in (
        "lender_brand",
        "legal_lending_entity",
        "borrower_name",
        "loan_id",
        "sanctioned_amount",
        "disbursed_amount",
        "processing_fee",
        "gst_amount",
        "other_fees_total",
        "interest_rate",
        "apr",
        "tenure",
        "sanction_date",
        "disbursement_date",
        "repayment_amount",
        "repayment_date",
        "repayment_frequency",
        "penalties",
    ):
        assert name in text


def test_schema_echo_retries_once_and_asks_for_results() -> None:
    echo = {
        "$defs": {"Money": {"type": "object"}},
        "additionalProperties": False,
        "description": "extract",
        "properties": {"results": {"type": "array"}},
        "required": ["results"],
        "title": "extract",
        "type": "object",
    }
    filled = {
        "results": [
            {
                "image_id": "img_loan",
                "loans": [{"sanctioned_amount": {"value": None, "currency": None}}],
            }
        ]
    }
    provider, seen = _provider([echo, filled])

    response = asyncio.run(provider.analyze_images(_extract_request()))

    assert len(seen) == 2
    first = seen[0].content.decode()
    second = seen[1].content.decode()
    assert "Fill results from the image" not in first
    assert "Fill results from the image. Do not repeat the schema." in second
    parsed = parse_extract_response(response.raw_json, "img_loan")
    assert parsed.loans[0].sanctioned_amount.value is None
    assert parsed.loans[0].sanctioned_amount.currency is None


def test_provider_accepts_ollama_extract_without_a_json_retry() -> None:
    provider, seen = _provider([OLLAMA_EXTRACT])

    response = asyncio.run(provider.analyze_images(_extract_request()))

    assert len(seen) == 1
    assert b"return JSON only" not in seen[0].content
    parsed = parse_extract_response(response.raw_json, "img_loan")
    assert parsed.loans[0].sanctioned_amount.value == "30000.00"
    assert parsed.loans[0].sanctioned_amount.currency == "AUD"
    assert parsed.loans[0].disbursed_amount.value is None


def test_non_json_extract_retries_up_to_five() -> None:
    provider, seen = _provider(["not json at all"] * 6)

    with pytest.raises(VisionResponseInvalidError) as raised:
        asyncio.run(provider.analyze_images(_extract_request()))

    assert len(seen) == 6
    assert b"return JSON only" not in seen[0].content
    assert b"return JSON only" in seen[1].content
    assert raised.value.code == "VISION_INVALID_RESPONSE"


def test_wrong_tenure_unit_is_sent_back_and_the_next_answer_is_kept() -> None:
    wrong = {
        "results": [
            {
                "image_id": "img_loan",
                "loans": [{"tenure": {"value": 31, "unit": "weeks"}}],
            }
        ]
    }
    provider, seen = _provider([wrong, OLLAMA_EXTRACT])

    response = asyncio.run(provider.analyze_images(_extract_request()))

    assert len(seen) == 2
    assert b"return JSON only" not in seen[0].content
    assert b"tenure.unit" in seen[1].content
    assert b"The previous answer was rejected." in seen[1].content
    parsed = parse_extract_response(response.raw_json, "img_loan")
    assert parsed.loans[0].sanctioned_amount.value == "30000.00"


def test_bullet_repayment_frequency_becomes_one_time() -> None:
    parsed = parse_extract_response(
        {
            "results": [
                {
                    "image_id": "img_loan",
                    "loans": [
                        {"repayment_frequency": "Bullet"},
                        {"repayment_frequency": {"value": "single instalment"}},
                        {"repayment_frequency": "fortnightly"},
                    ],
                }
            ]
        },
        "img_loan",
    )

    loans = parsed.loans
    assert loans[0].repayment_frequency.value == "one_time"
    assert loans[1].repayment_frequency.value == "one_time"
    assert loans[2].repayment_frequency.value is None


def test_unnamed_penalty_is_omitted_and_the_loan_is_kept() -> None:
    body = {
        "results": [
            {
                "image_id": "img_loan",
                "loans": [
                    {
                        "lender_brand": {"value": "Sample Lender"},
                        "sanctioned_amount": {"value": "15000", "currency": "₹"},
                        "repayment_amount": {"value": "16350", "currency": "₹"},
                        "repayment_frequency": "one_time",
                        "penalties": [{"value": "1000", "currency": "₹"}],
                    }
                ],
            }
        ]
    }
    provider, seen = _provider([body])

    response = asyncio.run(provider.analyze_images(_extract_request()))

    assert len(seen) == 1
    parsed = parse_extract_response(response.raw_json, "img_loan")
    loan = parsed.loans[0]
    assert loan.penalties == []
    assert loan.sanctioned_amount.value == "15000.00"
    assert loan.sanctioned_amount.currency is None
    assert loan.repayment_amount.value == "16350.00"
    assert loan.repayment_frequency.value == "one_time"


def test_named_penalty_stays_when_another_penalty_has_no_name() -> None:
    parsed = parse_extract_response(
        {
            "results": [
                {
                    "image_id": "img_loan",
                    "loans": [
                        {
                            "penalties": [
                                {"value": "1000", "currency": "₹"},
                                {
                                    "name": "Late fee",
                                    "amount": {"value": "50.00", "currency": "INR"},
                                },
                                {"name": "NA", "amount": {"value": "10", "currency": "INR"}},
                            ]
                        }
                    ],
                }
            ]
        },
        "img_loan",
    )

    assert len(parsed.loans[0].penalties) == 1
    assert parsed.loans[0].penalties[0].name == "Late fee"
    assert parsed.loans[0].penalties[0].amount is not None
    assert parsed.loans[0].penalties[0].amount.value == "50.00"
    assert parsed.loans[0].penalties[0].amount.currency == "INR"


def test_currency_word_inside_money_is_sent_back() -> None:
    priced = {
        "results": [
            {
                "image_id": "img_loan",
                "loans": [
                    {
                        "sanctioned_amount": {"value": "INR 24800", "currency": "INR"},
                        "repayment_amount": {"value": "INR 32240", "currency": "INR"},
                    }
                ],
            }
        ]
    }
    fixed = {
        "results": [
            {
                "image_id": "img_loan",
                "loans": [
                    {
                        "sanctioned_amount": {"value": "24800.00", "currency": "INR"},
                        "repayment_amount": {"value": "32240.00", "currency": "INR"},
                    }
                ],
            }
        ]
    }
    provider, seen = _provider([priced, fixed])

    response = asyncio.run(provider.analyze_images(_extract_request()))

    assert len(seen) == 2
    assert b"sanctioned_amount.value" in seen[1].content
    assert b"currency, not in the value" in seen[1].content
    parsed = parse_extract_response(response.raw_json, "img_loan")
    assert parsed.loans[0].sanctioned_amount.value == "24800.00"
    assert parsed.loans[0].repayment_amount.value == "32240.00"
