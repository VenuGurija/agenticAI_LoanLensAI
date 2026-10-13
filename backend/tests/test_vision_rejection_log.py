"""Rejected vision answers are logged without secrets or image bytes (SPEC §16, §23)."""

import asyncio
import base64
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

from app.core.config import Settings
from app.core.logging import LOGGER_NAME, StructuredFormatter
from app.schemas.vision import VisionImage, VisionRequest
from app.services.classification import parse_classification
from app.services.extraction import parse_extract_response
from app.services.vision.base import VisionResponseInvalidError
from app.services.vision.openai_compatible import OpenAICompatibleVisionProvider

SECRET = "sk-test-secret-value"
IMAGE_SENTINEL = b"PLAINTEXT-IMAGE-SENTINEL"
ANALYSIS_ID = "anl_reject_log"
TOKEN = "rejected-model-token"
BAD_CLASS = "not_a_real_class_zz"


@contextmanager
def _captured_lines() -> Iterator[list[str]]:
    logger = logging.getLogger(LOGGER_NAME)
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(StructuredFormatter(secrets=(SECRET,)).format(record))

    logger.addHandler(_Capture())
    try:
        yield lines
    finally:
        logger.handlers.clear()
        logger.handlers.extend(previous_handlers)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def _events(lines: list[str]) -> list[dict]:
    return [json.loads(line) for line in lines if '"vision_response_rejected"' in line]


def _assert_line_hides_secrets(line: str) -> None:
    encoded = base64.b64encode(IMAGE_SENTINEL).decode("ascii")
    assert SECRET not in line
    assert IMAGE_SENTINEL.decode() not in line
    assert encoded not in line
    assert "Authorization" not in line
    assert "Bearer" not in line


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "vision_provider": "openai_compatible",
        "vision_api_key": SECRET,
        "vision_base_url": "https://vision.example.test/v1",
        "vision_model": "configured-model",
        "loanlens_env": "development",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _request() -> VisionRequest:
    return VisionRequest(
        analysis_id=ANALYSIS_ID,
        task="extract",
        images=[
            VisionImage(image_id="img_a", media_type="image/png", bytes=IMAGE_SENTINEL),
        ],
        response_schema_name="extract",
        hint=None,
    )


def _provider(content: str, **overrides: object) -> OpenAICompatibleVisionProvider:
    encoded = base64.b64encode(IMAGE_SENTINEL).decode("ascii")
    body = f"{content} {SECRET} {IMAGE_SENTINEL.decode()} {encoded}"

    def handler(_request: httpx.Request) -> httpx.Response:
        payload = {
            "model": "vendor-model",
            "choices": [{"message": {"role": "assistant", "content": body}}],
        }
        return httpx.Response(200, json=payload)

    return OpenAICompatibleVisionProvider(
        _settings(**overrides),
        transport=httpx.MockTransport(handler),
    )


def test_provider_logs_rejected_answer_in_development() -> None:
    provider = _provider(TOKEN)
    with _captured_lines() as lines:
        with pytest.raises(VisionResponseInvalidError):
            asyncio.run(provider.analyze_images(_request()))

    events = _events(lines)
    assert [event["attempt"] for event in events] == [1, 2, 3, 4, 5, 6]
    for event in events:
        assert event["event"] == "vision_response_rejected"
        assert event["level"] == "WARNING"
        assert event["analysis_id"] == ANALYSIS_ID
        assert event["task"] == "extract"
        assert event["schema_name"] == "extract"
        assert event["validation_failures"] == [
            {"loc": "message.content", "type": "json_invalid"}
        ]
        assert TOKEN in event["model_answer"]
    for line in lines:
        _assert_line_hides_secrets(line)


def test_provider_omits_rejected_answer_in_production() -> None:
    provider = _provider(TOKEN, loanlens_env="production")
    with _captured_lines() as lines:
        with pytest.raises(VisionResponseInvalidError):
            asyncio.run(provider.analyze_images(_request()))

    events = _events(lines)
    assert len(events) == 6
    for event in events:
        assert event["analysis_id"] == ANALYSIS_ID
        assert event["task"] == "extract"
        assert "model_answer" not in event
        assert event["validation_failures"]
    blob = "\n".join(lines)
    assert TOKEN not in blob
    _assert_line_hides_secrets(blob)


def test_provider_logs_schema_failure_location_and_type() -> None:
    bad = json.dumps(
        {"results": [{"image_id": "img_a", "document_class": BAD_CLASS}]}
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        payload = {
            "choices": [{"message": {"content": bad}}],
        }
        return httpx.Response(200, json=payload)

    provider = OpenAICompatibleVisionProvider(
        _settings(),
        transport=httpx.MockTransport(handler),
    )
    request = VisionRequest(
        analysis_id=ANALYSIS_ID,
        task="classify",
        images=[VisionImage(image_id="img_a", media_type="image/png", bytes=IMAGE_SENTINEL)],
        response_schema_name="classify",
        hint=None,
    )
    with _captured_lines() as lines:
        with pytest.raises(VisionResponseInvalidError):
            asyncio.run(provider.analyze_images(request))

    event = _events(lines)[0]
    assert event["task"] == "classify"
    assert event["schema_name"] == "classify"
    assert {"loc": "results.0.document_class", "type": "enum"} in event["validation_failures"]
    assert BAD_CLASS in event["model_answer"]
    for failure in event["validation_failures"]:
        assert set(failure) == {"loc", "type"}
        assert BAD_CLASS not in failure["loc"]
        assert BAD_CLASS not in failure["type"]
    _assert_line_hides_secrets("\n".join(lines))


def test_parse_classification_logs_answer_only_in_development() -> None:
    raw = {
        "document_class": BAD_CLASS,
        "notes": f"{TOKEN} {SECRET} {IMAGE_SENTINEL.decode()}",
    }
    _assert_parse_environments(
        lambda environment: parse_classification(
            raw,
            "img_server",
            analysis_id=ANALYSIS_ID,
            environment=environment,
            image_bytes=(IMAGE_SENTINEL,),
        ),
        task="classify",
        schema_name="classify",
        loc="document_class",
        err_type="enum",
    )


def test_parse_extract_logs_answer_only_in_development() -> None:
    raw = {
        "results": [
            {
                "image_id": "page-1",
                "loans": [
                    {
                        "borrower_name": {
                            "value": f"{TOKEN} {SECRET} {IMAGE_SENTINEL.decode()}"
                        },
                        "sanctioned_amount": {"value": "10.999", "currency": "INR"},
                    }
                ],
            }
        ]
    }
    _assert_parse_environments(
        lambda environment: parse_extract_response(
            raw,
            "img_server",
            analysis_id=ANALYSIS_ID,
            environment=environment,
            image_bytes=(IMAGE_SENTINEL,),
        ),
        task="extract",
        schema_name="extract",
        loc="loans.0.sanctioned_amount.value",
        err_type="value_error",
    )


def _assert_parse_environments(
    call,
    *,
    task: str,
    schema_name: str,
    loc: str,
    err_type: str,
) -> None:
    with _captured_lines() as dev_lines:
        with pytest.raises(VisionResponseInvalidError):
            call("development")
    dev = _events(dev_lines)[0]
    assert dev["analysis_id"] == ANALYSIS_ID
    assert dev["task"] == task
    assert dev["attempt"] is None
    assert dev["schema_name"] == schema_name
    assert {"loc": loc, "type": err_type} in dev["validation_failures"]
    assert TOKEN in dev["model_answer"]
    for failure in dev["validation_failures"]:
        assert set(failure) == {"loc", "type"}
    _assert_line_hides_secrets("\n".join(dev_lines))

    with _captured_lines() as prod_lines:
        with pytest.raises(VisionResponseInvalidError):
            call("production")
    prod = _events(prod_lines)[0]
    assert "model_answer" not in prod
    assert {"loc": loc, "type": err_type} in prod["validation_failures"]
    blob = "\n".join(prod_lines)
    assert TOKEN not in blob
    assert "10.999" not in blob
    _assert_line_hides_secrets(blob)
