"""Deterministic financial checks (SPEC §11)."""

import copy
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from app.models.database import AnalysisRow
from app.schemas.vision import VisionRequest, VisionResponse
from app.services.validation.rules import CHECK_IDS, attach_loan_validations, validate_loan_fields
from app.services.vision.fake import FakeVisionProvider


def _money(
    value: str | None,
    *,
    currency: str | None = "INR",
    source: str = "extracted",
) -> dict:
    if value is None:
        return {"value": None, "currency": None, "source": "unknown"}
    return {"value": value, "currency": currency, "source": source}


def _date(value: str | None, source: str = "extracted") -> dict:
    if value is None:
        return {"value": None, "source": "unknown"}
    return {"value": value, "source": source}


def _rate(value: str | None) -> dict:
    if value is None:
        return {"value": None, "unit": "percent", "source": "unknown"}
    return {"value": value, "unit": "percent", "source": "extracted"}


def _tenure(value: int | None, unit: str | None) -> dict:
    if value is None and unit is None:
        return {"value": None, "unit": None, "source": "unknown"}
    return {"value": value, "unit": unit, "source": "extracted"}


def _check(result: dict, check_id: str) -> dict:
    matches = [item for item in result["validations"] if item["id"] == check_id]
    assert len(matches) == 1
    return matches[0]


def _assert_check(result: dict, check_id: str, status: str) -> None:
    item = _check(result, check_id)
    assert item["status"] == status
    assert isinstance(item["message"], str) and item["message"]
    assert set(item) == {"id", "status", "message"}


def test_every_check_id_is_reported() -> None:
    result = validate_loan_fields({})

    assert [item["id"] for item in result["validations"]] == list(CHECK_IDS)
    assert result["validation_summary"] == "incomplete"
    for check_id in CHECK_IDS:
        _assert_check(result, check_id, "skipped")


def test_v_positive_sanctioned_passed_failed_and_skipped() -> None:
    passed = validate_loan_fields({"sanctioned_amount": _money("15000.00")})
    failed = validate_loan_fields({"sanctioned_amount": _money("0.00")})
    negative = validate_loan_fields({"sanctioned_amount": _money("-1.00")})
    skipped = validate_loan_fields({"sanctioned_amount": _money(None)})

    _assert_check(passed, "V_POSITIVE_SANCTIONED", "passed")
    _assert_check(failed, "V_POSITIVE_SANCTIONED", "failed")
    _assert_check(negative, "V_POSITIVE_SANCTIONED", "failed")
    _assert_check(skipped, "V_POSITIVE_SANCTIONED", "skipped")
    assert "VALIDATION_FAILED:V_POSITIVE_SANCTIONED" in failed["review_flags"]


def test_v_positive_disbursed_passed_failed_and_skipped() -> None:
    passed = validate_loan_fields({"disbursed_amount": _money("13230.00", source="user_corrected")})
    failed = validate_loan_fields({"disbursed_amount": _money("0.00")})
    skipped = validate_loan_fields({"disbursed_amount": _money(None)})

    _assert_check(passed, "V_POSITIVE_DISBURSED", "passed")
    _assert_check(failed, "V_POSITIVE_DISBURSED", "failed")
    _assert_check(skipped, "V_POSITIVE_DISBURSED", "skipped")


def test_v_positive_repayment_passed_failed_and_skipped() -> None:
    passed = validate_loan_fields({"repayment_amount": _money("16950.00")})
    failed = validate_loan_fields({"repayment_amount": _money("-5.00")})
    skipped = validate_loan_fields({"repayment_amount": _money(None)})

    _assert_check(passed, "V_POSITIVE_REPAYMENT", "passed")
    _assert_check(failed, "V_POSITIVE_REPAYMENT", "failed")
    _assert_check(skipped, "V_POSITIVE_REPAYMENT", "skipped")


def test_v_disbursed_lte_sanctioned_passed_failed_and_skipped() -> None:
    equal = validate_loan_fields(
        {
            "sanctioned_amount": _money("100.00"),
            "disbursed_amount": _money("100.00"),
        }
    )
    less = validate_loan_fields(
        {
            "sanctioned_amount": _money("15000.00"),
            "disbursed_amount": _money("13230.00"),
        }
    )
    failed = validate_loan_fields(
        {
            "sanctioned_amount": _money("100.00"),
            "disbursed_amount": _money("100.01"),
        }
    )
    skipped = validate_loan_fields(
        {
            "sanctioned_amount": _money("100.00"),
            "disbursed_amount": _money(None),
        }
    )

    _assert_check(equal, "V_DISBURSED_LTE_SANCTIONED", "passed")
    _assert_check(less, "V_DISBURSED_LTE_SANCTIONED", "passed")
    _assert_check(failed, "V_DISBURSED_LTE_SANCTIONED", "failed")
    _assert_check(skipped, "V_DISBURSED_LTE_SANCTIONED", "skipped")
    assert "VALIDATION_FAILED:V_DISBURSED_LTE_SANCTIONED" in failed["review_flags"]
    assert failed["validation_summary"] == "failed"


def test_v_repayment_gte_disbursed_passed_failed_and_skipped() -> None:
    equal = validate_loan_fields(
        {
            "disbursed_amount": _money("13230.00"),
            "repayment_amount": _money("13230.00"),
        }
    )
    greater = validate_loan_fields(
        {
            "disbursed_amount": _money("13230.00"),
            "repayment_amount": _money("16950.00"),
        }
    )
    failed = validate_loan_fields(
        {
            "disbursed_amount": _money("100.01"),
            "repayment_amount": _money("100.00"),
        }
    )
    skipped = validate_loan_fields({"repayment_amount": _money("16950.00")})

    _assert_check(equal, "V_REPAYMENT_GTE_DISBURSED", "passed")
    _assert_check(greater, "V_REPAYMENT_GTE_DISBURSED", "passed")
    _assert_check(failed, "V_REPAYMENT_GTE_DISBURSED", "failed")
    _assert_check(skipped, "V_REPAYMENT_GTE_DISBURSED", "skipped")


def test_v_fees_reconcile_tolerance_and_skipped_when_gst_missing() -> None:
    matched = validate_loan_fields(
        {
            "sanctioned_amount": _money("15000.00"),
            "disbursed_amount": _money("13230.00"),
            "processing_fee": _money("1500.00"),
            "gst_amount": _money("270.00"),
        }
    )
    # |100.00 - 80.00 - 19.00 - 0.00| = 1.00, the only allowed tolerance.
    at_tolerance = validate_loan_fields(
        {
            "sanctioned_amount": _money("100.00"),
            "disbursed_amount": _money("80.00"),
            "processing_fee": _money("19.00"),
            "gst_amount": _money("0.00"),
        }
    )
    # |100.00 - 80.00 - 18.99 - 0.00| = 1.01.
    over = validate_loan_fields(
        {
            "sanctioned_amount": _money("100.00"),
            "disbursed_amount": _money("80.00"),
            "processing_fee": _money("18.99"),
            "gst_amount": _money("0.00"),
        }
    )
    missing_gst = validate_loan_fields(
        {
            "sanctioned_amount": _money("100.00"),
            "disbursed_amount": _money("80.00"),
            "processing_fee": _money("10.00"),
            "gst_amount": _money(None),
        }
    )
    one_rupee_over_disbursed = validate_loan_fields(
        {
            "sanctioned_amount": _money("100.00"),
            "disbursed_amount": _money("101.00"),
        }
    )

    _assert_check(matched, "V_FEES_RECONCILE", "passed")
    _assert_check(at_tolerance, "V_FEES_RECONCILE", "passed")
    _assert_check(over, "V_FEES_RECONCILE", "failed")
    _assert_check(missing_gst, "V_FEES_RECONCILE", "skipped")
    assert "VALIDATION_FAILED:V_FEES_RECONCILE" not in missing_gst["review_flags"]
    _assert_check(one_rupee_over_disbursed, "V_DISBURSED_LTE_SANCTIONED", "failed")


def test_v_dates_order_passed_failed_and_skipped() -> None:
    passed = validate_loan_fields(
        {
            "sanction_date": _date("2026-06-17"),
            "disbursement_date": _date("2026-06-17"),
            "repayment_date": _date("2026-06-30"),
        }
    )
    failed = validate_loan_fields(
        {
            "sanction_date": _date("2026-06-18"),
            "disbursement_date": _date("2026-06-17"),
        }
    )
    skipped = validate_loan_fields({"sanction_date": _date("2026-06-17")})

    _assert_check(passed, "V_DATES_ORDER", "passed")
    _assert_check(failed, "V_DATES_ORDER", "failed")
    _assert_check(skipped, "V_DATES_ORDER", "skipped")


def test_v_tenure_vs_dates_passed_failed_and_skipped() -> None:
    passed = validate_loan_fields(
        {
            "disbursement_date": _date("2026-06-17"),
            "repayment_date": _date("2026-06-30"),
            "tenure": _tenure(14, "days"),
        }
    )
    failed = validate_loan_fields(
        {
            "disbursement_date": _date("2026-06-17"),
            "repayment_date": _date("2026-06-30"),
            "tenure": _tenure(16, "days"),
        }
    )
    skipped_months = validate_loan_fields(
        {
            "disbursement_date": _date("2026-06-17"),
            "repayment_date": _date("2026-06-30"),
            "tenure": _tenure(2, "months"),
        }
    )
    skipped_missing = validate_loan_fields(
        {
            "disbursement_date": _date("2026-06-17"),
            "tenure": _tenure(14, "days"),
        }
    )

    _assert_check(passed, "V_TENURE_VS_DATES", "passed")
    _assert_check(failed, "V_TENURE_VS_DATES", "failed")
    _assert_check(skipped_months, "V_TENURE_VS_DATES", "skipped")
    _assert_check(skipped_missing, "V_TENURE_VS_DATES", "skipped")


def test_v_rate_range_passed_failed_and_skipped() -> None:
    passed = validate_loan_fields({"interest_rate": _rate("29.95"), "apr": _rate("36")})
    zero = validate_loan_fields({"interest_rate": _rate("0")})
    too_high = validate_loan_fields({"apr": _rate("1000")})
    skipped = validate_loan_fields({"interest_rate": _rate(None), "apr": _rate(None)})

    _assert_check(passed, "V_RATE_RANGE", "passed")
    _assert_check(zero, "V_RATE_RANGE", "failed")
    _assert_check(too_high, "V_RATE_RANGE", "failed")
    _assert_check(skipped, "V_RATE_RANGE", "skipped")


def test_failed_check_does_not_rewrite_extracted_amounts() -> None:
    fields = {
        "sanctioned_amount": _money("100.00"),
        "disbursed_amount": _money("150.00"),
        "repayment_amount": _money("90.00"),
        "processing_fee": _money("10.00"),
        "gst_amount": _money("1.00"),
    }
    snapshot = copy.deepcopy(fields)
    loan = {"fields": fields, "review_flags": ["FIELD_CONFLICT"]}

    result = validate_loan_fields(fields)
    attach_loan_validations([{"loans": [loan]}])

    assert fields == snapshot
    assert loan["fields"] == snapshot
    _assert_check(result, "V_DISBURSED_LTE_SANCTIONED", "failed")
    assert loan["fields"]["sanctioned_amount"]["value"] == "100.00"
    assert loan["fields"]["disbursed_amount"]["value"] == "150.00"
    assert loan["fields"]["repayment_amount"]["value"] == "90.00"
    assert loan["validation_summary"] == "failed"
    assert loan["review_flags"][0] == "FIELD_CONFLICT"
    assert "VALIDATION_FAILED:V_DISBURSED_LTE_SANCTIONED" in loan["review_flags"]


def test_validation_summary_ok_failed_and_incomplete() -> None:
    complete = validate_loan_fields(
        {
            "sanctioned_amount": _money("15000.00"),
            "disbursed_amount": _money("13230.00"),
            "repayment_amount": _money("16950.00"),
        }
    )
    incomplete = validate_loan_fields({"sanctioned_amount": _money("15000.00")})
    failed = validate_loan_fields(
        {
            "sanctioned_amount": _money("100.00"),
            "disbursed_amount": _money("150.00"),
            "repayment_amount": _money(None),
        }
    )

    assert complete["validation_summary"] == "ok"
    assert incomplete["validation_summary"] == "incomplete"
    assert failed["validation_summary"] == "failed"
    _assert_check(complete, "V_FEES_RECONCILE", "skipped")


def test_mixed_or_unknown_currency_skips_amount_comparison() -> None:
    mixed = validate_loan_fields(
        {
            "sanctioned_amount": _money("15000.00", currency="INR"),
            "disbursed_amount": _money("13230.00", currency="USD"),
            "repayment_amount": _money("16950.00", currency="USD"),
        }
    )
    unknown = validate_loan_fields(
        {
            "sanctioned_amount": _money("15000.00", currency=None),
            "disbursed_amount": _money("13230.00", currency=None),
        }
    )

    _assert_check(mixed, "V_POSITIVE_SANCTIONED", "passed")
    _assert_check(mixed, "V_POSITIVE_DISBURSED", "passed")
    _assert_check(mixed, "V_DISBURSED_LTE_SANCTIONED", "skipped")
    _assert_check(mixed, "V_REPAYMENT_GTE_DISBURSED", "passed")
    assert mixed["validation_summary"] == "ok"
    assert "CURRENCY_UNKNOWN" in mixed["review_flags"]
    assert "VALIDATION_FAILED:V_DISBURSED_LTE_SANCTIONED" not in mixed["review_flags"]
    _assert_check(unknown, "V_DISBURSED_LTE_SANCTIONED", "skipped")
    assert "CURRENCY_UNKNOWN" in unknown["review_flags"]


def test_calculated_amounts_are_not_treated_as_extracted() -> None:
    result = validate_loan_fields(
        {
            "sanctioned_amount": _money("15000.00", source="calculated"),
            "disbursed_amount": _money("13230.00"),
        }
    )

    _assert_check(result, "V_POSITIVE_SANCTIONED", "skipped")
    _assert_check(result, "V_DISBURSED_LTE_SANCTIONED", "skipped")
    assert result["validation_summary"] == "incomplete"


class _SwitchingFakeVisionProvider:
    provider_name = "fake"
    model_name = "fake"

    def __init__(self, classify_dir: Path, extract_dir: Path) -> None:
        self.classify_provider = FakeVisionProvider(fixture_dir=classify_dir)
        self.extract_provider = FakeVisionProvider(fixture_dir=extract_dir)
        self.calls: list[VisionRequest] = []

    async def analyze_images(self, request: VisionRequest) -> VisionResponse:
        self.calls.append(request)
        if request.task == "classify":
            return await self.classify_provider.analyze_images(request)
        if request.task == "extract":
            return await self.extract_provider.analyze_images(request)
        raise AssertionError(request.task)


def _page_png() -> bytes:
    image = Image.new("RGB", (200, 200), (30, 90, 140))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@contextmanager
def _analyze_client(tmp_path: Path) -> Iterator[tuple[TestClient, Path, Path]]:
    classify_dir = tmp_path / "classify"
    extract_dir = tmp_path / "extract"
    classify_dir.mkdir()
    extract_dir.mkdir()
    provider = _SwitchingFakeVisionProvider(classify_dir, extract_dir)
    settings = Settings(
        loanlens_env="test",
        loanlens_database_url=f"sqlite:///{(tmp_path / 'loanlens.db').as_posix()}",
        loanlens_tmp_dir=str((tmp_path / "images").resolve()),
        loanlens_analysis_ttl_hours=24,
        loanlens_max_image_bytes=10_485_760,
        vision_provider="fake",
        _env_file=None,
    )
    application = create_app(settings)
    application.state.vision_provider = provider
    try:
        with TestClient(application) as client:
            yield client, classify_dir, extract_dir
    finally:
        application.state.engine.dispose()


def _write_json(directory: Path, image_id: str, payload: dict) -> None:
    (directory / f"{image_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_analyze_attaches_validations_without_changing_amounts(tmp_path: Path) -> None:
    with _analyze_client(tmp_path) as (client, classify_dir, extract_dir):
        book = client.post("/api/v1/books", json={})
        assert book.status_code == 201
        draft = client.post(f"/api/v1/books/{book.json()['book_id']}/drafts", json={})
        assert draft.status_code == 201
        analysis_id = draft.json()["analysis_id"]
        png = _page_png()
        uploaded = client.post(
            f"/api/v1/analyses/{analysis_id}/images",
            files=[("files", ("sanction.png", png, "image/png"))],
        )
        assert uploaded.status_code == 201
        image_id = uploaded.json()["images"][0]["image_id"]
        _write_json(
            classify_dir,
            image_id,
            {
                "results": [
                    {
                        "image_id": image_id,
                        "document_class": "loan_document",
                        "confidence": "high",
                        "page_role": "sanction",
                        "notes": None,
                    }
                ]
            },
        )
        _write_json(
            extract_dir,
            image_id,
            {
                "results": [
                    {
                        "image_id": image_id,
                        "loans": [
                            {
                                "lender_brand": {"value": "Sample", "confidence": "high"},
                                "sanctioned_amount": {
                                    "value": "100.00",
                                    "currency": "INR",
                                    "confidence": "high",
                                },
                                "disbursed_amount": {
                                    "value": "150.00",
                                    "currency": "INR",
                                    "confidence": "high",
                                },
                            }
                        ],
                    }
                ]
            },
        )

        analyzed = client.post(f"/api/v1/analyses/{analysis_id}/analyze", json={})

        assert analyzed.status_code == 202
        assert analyzed.json()["status"] == "queued"
        deadline = time.monotonic() + 5
        finished = client.get(f"/api/v1/analyses/{analysis_id}")
        while time.monotonic() < deadline and finished.json()["status"] not in {
            "completed",
            "completed_needs_review",
            "failed",
        }:
            time.sleep(0.05)
            finished = client.get(f"/api/v1/analyses/{analysis_id}")
        assert finished.status_code == 200
        assert finished.json()["status"] == "completed_needs_review"
        loan = finished.json()["lender_groups"][0]["loans"][0]
        assert loan["fields"]["sanctioned_amount"]["value"] == "100.00"
        assert loan["fields"]["disbursed_amount"]["value"] == "150.00"
        assert [item["id"] for item in loan["validations"]] == list(CHECK_IDS)
        assert _check(loan, "V_DISBURSED_LTE_SANCTIONED")["status"] == "failed"
        assert loan["validation_summary"] == "failed"
        assert "VALIDATION_FAILED:V_DISBURSED_LTE_SANCTIONED" in loan["review_flags"]
        assert "calculated" not in loan

        session: Session = client.app.state.session_factory()
        try:
            row = session.get(AnalysisRow, analysis_id)
            assert row is not None and row.result_json is not None
            stored = json.loads(row.result_json)
        finally:
            session.close()
        extracted = stored["raw_extracts"][0]["loans"][0]
        assert extracted["disbursed_amount"]["value"] == "150.00"
        assert "validations" not in extracted
        assert stored["lender_groups"][0]["loans"][0]["validation_summary"] == "failed"
