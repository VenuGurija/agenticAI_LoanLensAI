import json
import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import ERROR_SPECS, AppError
from app.core.logging import LOGGER_NAME, StructuredFormatter, configure_logging, log_event, safe_log_extra
from app.main import create_app

_SPEC_ERRORS = (
    ("ANALYSIS_NOT_FOUND", 404, False),
    ("BOOK_NOT_FOUND", 404, False),
    ("LOAN_NOT_FOUND", 404, False),
    ("INVALID_STATUS", 409, False),
    ("NO_IMAGES", 400, False),
    ("TOO_MANY_IMAGES", 400, False),
    ("IMAGE_TOO_LARGE", 400, False),
    ("IMAGE_UNSUPPORTED_TYPE", 400, False),
    ("IMAGE_UNREADABLE", 400, False),
    ("IMAGE_TOO_SMALL", 400, False),
    ("NO_LOAN_DOCUMENTS", 422, False),
    ("VISION_UNAVAILABLE", 503, True),
    ("VISION_INVALID_RESPONSE", 502, True),
    ("VISION_TIMEOUT", 504, True),
    ("INTERNAL_ERROR", 500, True),
)


@pytest.mark.parametrize(("code", "status_code", "retryable"), _SPEC_ERRORS)
def test_app_error_uses_spec_table(code: str, status_code: int, retryable: bool) -> None:
    error = AppError(code)

    assert error.status_code == status_code
    assert error.retryable is retryable
    assert error.envelope() == {
        "error": {
            "code": code,
            "message": ERROR_SPECS[code].message,
            "retryable": retryable,
            "details": {},
        }
    }


def test_unknown_error_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown error code"):
        AppError("NOT_A_CODE")


def test_image_too_large_envelope_matches_spec() -> None:
    application = create_app()

    @application.get("/_test/too-large")
    def _too_large() -> None:
        raise AppError("IMAGE_TOO_LARGE", details={"filename": "big.jpg"})

    response = TestClient(application).get("/_test/too-large")

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "IMAGE_TOO_LARGE",
            "message": "Each image must be 10 MiB or smaller.",
            "retryable": False,
            "details": {"filename": "big.jpg"},
        }
    }


def test_app_error_response_omits_request_body() -> None:
    application = create_app()

    @application.post("/_test/no-images")
    def _no_images() -> None:
        raise AppError(
            "NO_IMAGES",
            details={"borrower_name": "Hidden", "loan_id": "LN1", "amount": 42000, "filename": "a.png"},
        )

    response = TestClient(application).post(
        "/_test/no-images",
        json={"borrower_name": "Hidden", "loan_id": "LN1", "amount": 42000},
    )

    assert response.status_code == 400
    assert response.json()["error"]["details"] == {"filename": "a.png"}
    assert "Hidden" not in response.text
    assert "LN1" not in response.text
    assert "42000" not in response.text


def test_unknown_path_is_404_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Not found.",
            "retryable": False,
            "details": {},
        }
    }


def test_validation_error_omits_request_body() -> None:
    application = create_app()

    class Payload(BaseModel):
        count: int

    @application.post("/_test/validate")
    def _validate(_payload: Payload) -> dict[str, str]:
        return {"status": "ok"}

    response = TestClient(application).post(
        "/_test/validate",
        json={"count": "nope", "borrower_name": "Hidden Name", "amount": 999},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "REQUEST_INVALID"
    assert body["error"]["retryable"] is False
    assert body["error"]["details"] == {}
    assert "Hidden Name" not in response.text
    assert "999" not in response.text


def test_unexpected_error_hides_exception_text() -> None:
    application = create_app()

    @application.get("/_test/boom")
    def _boom() -> None:
        raise RuntimeError("borrower_name Secret amount 99999")

    response = TestClient(application, raise_server_exceptions=False).get("/_test/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["retryable"] is True
    assert "Secret" not in response.text
    assert "99999" not in response.text


def test_safe_log_extra_drops_pii_keys() -> None:
    extra = safe_log_extra(
        {
            "analysis_id": "anl_01",
            "image_id": "img_01",
            "status": "failed",
            "duration_ms": 12,
            "provider": "fake",
            "model": "fake-model",
            "error_code": "VISION_TIMEOUT",
            "token_count": 3,
            "borrower_name": "Hidden",
            "loan_id": "LN-9",
            "amount": 15000,
            "Borrower_Name": "Also Hidden",
            "filename": "aadhaar.png",
        }
    )

    assert extra == {
        "analysis_id": "anl_01",
        "image_id": "img_01",
        "status": "failed",
        "duration_ms": 12,
        "provider": "fake",
        "model": "fake-model",
        "error_code": "VISION_TIMEOUT",
        "token_count": 3,
    }


def test_structured_log_records_ids_and_error_code_only() -> None:
    configure_logging("INFO")
    logger = logging.getLogger(LOGGER_NAME)
    previous_handlers = list(logger.handlers)
    logger.handlers.clear()
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(StructuredFormatter().format(record))

    logger.addHandler(_Capture())
    try:
        log_event(
            "vision_failed",
            analysis_id="anl_01",
            error_code="VISION_TIMEOUT",
            borrower_name="Hidden Person",
            loan_id="LN-9",
            amount=15000,
            filename="statement.png",
        )
        payload = json.loads(lines[0])
    finally:
        logger.handlers.clear()
        logger.handlers.extend(previous_handlers)

    assert payload["event"] == "vision_failed"
    assert payload["level"] == "INFO"
    assert "timestamp" in payload
    assert payload["analysis_id"] == "anl_01"
    assert payload["error_code"] == "VISION_TIMEOUT"
    assert "borrower_name" not in payload
    assert "loan_id" not in payload
    assert "amount" not in payload
    assert "filename" not in payload
    assert "Hidden" not in lines[0]
    assert "LN-9" not in lines[0]
    assert "15000" not in lines[0]
