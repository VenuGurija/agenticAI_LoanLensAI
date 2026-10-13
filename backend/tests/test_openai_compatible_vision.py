"""OpenAI-compatible vision adapter. Network is mocked; no live API calls."""

import asyncio
import base64
import json

import httpx
import pytest

from app.core.config import Settings
from app.schemas.vision import VisionImage, VisionRequest, VisionResponse
from app.services.vision.base import VisionResponseInvalidError
from app.services.vision.openai_compatible import (
    DEFAULT_BASE_URL,
    OpenAICompatibleVisionProvider,
    VisionTimeoutError,
    VisionUnavailableError,
)

SECRET = "sk-test-secret-value"
ANALYSIS_ID = "anl_DO_NOT_SEND_9f3c"
IMAGE_SENTINEL = b"PLAINTEXT-IMAGE-SENTINEL"
_CLASSIFY = {
    "results": [
        {
            "image_id": "img_a",
            "document_class": "loan_document",
            "confidence": "high",
            "page_role": "sanction",
            "notes": None,
            "extra_note": "strip-me",
        },
        {"image_id": "img_b", "document_class": "unclear"},
    ],
    "debug": "strip-me",
}


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "vision_provider": "openai_compatible",
        "vision_api_key": SECRET,
        "vision_base_url": "https://vision.example.test/v1/",
        "vision_model": "configured-model",
        "loanlens_vision_timeout_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _request() -> VisionRequest:
    return VisionRequest(
        analysis_id=ANALYSIS_ID,
        task="classify",
        images=[
            VisionImage(image_id="img_a", media_type="image/png", bytes=IMAGE_SENTINEL),
            VisionImage(image_id="img_b", media_type="image/jpeg", bytes=b"second-page"),
        ],
        response_schema_name="classify",
        hint="same loan pages",
    )


def _chat(content: str, *, model: str = "vendor-model-name") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }


def _provider(handler, **overrides: object) -> tuple[OpenAICompatibleVisionProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    provider = OpenAICompatibleVisionProvider(
        _settings(**overrides),
        transport=httpx.MockTransport(transport_handler),
    )
    return provider, seen


def _user_text(request: httpx.Request) -> str:
    body = json.loads(request.content.decode())
    parts = body["messages"][1]["content"]
    return "\n".join(part["text"] for part in parts if part.get("type") == "text")


def test_success_validates_json_strips_extras_and_batches_images() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat(json.dumps(_CLASSIFY)))

    provider, seen = _provider(handler)
    response = asyncio.run(provider.analyze_images(_request()))

    assert isinstance(response, VisionResponse)
    assert response.provider_name == "openai_compatible"
    assert response.model_name == "vendor-model-name"
    assert response.usage is not None
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 4
    assert [item["image_id"] for item in response.raw_json["results"]] == ["img_a", "img_b"]
    assert response.raw_json["results"][0]["document_class"] == "loan_document"
    assert "extra_note" not in response.raw_json["results"][0]
    assert "debug" not in response.raw_json
    assert len(seen) == 1

    outbound = json.loads(seen[0].content.decode())
    assert seen[0].url == "https://vision.example.test/v1/chat/completions"
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in seen[0].content.decode()
    assert ANALYSIS_ID not in seen[0].content.decode()
    assert outbound["model"] == "configured-model"
    assert outbound["temperature"] == 0
    assert outbound["response_format"] == {"type": "json_object"}
    assert "same loan pages" in _user_text(seen[0])
    assert "return JSON only" not in _user_text(seen[0])
    image_parts = [part for part in outbound["messages"][1]["content"] if part["type"] == "image_url"]
    assert len(image_parts) == 2
    assert [part["image_url"]["detail"] for part in image_parts] == ["high", "high"]
    assert base64.b64decode(image_parts[0]["image_url"]["url"].split(",", 1)[1]) == IMAGE_SENTINEL
    assert IMAGE_SENTINEL not in seen[0].content
    timeout = seen[0].extensions["timeout"]
    assert timeout["connect"] == 60.0
    assert timeout["read"] == 60.0
    assert timeout["write"] == 60.0
    assert timeout["pool"] == 60.0
    assert provider._timeout_seconds == 60.0


def test_raised_vision_timeout_is_used_on_the_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat(json.dumps(_CLASSIFY)))

    provider, seen = _provider(handler, loanlens_vision_timeout_seconds=300)
    asyncio.run(provider.analyze_images(_request()))

    timeout = seen[0].extensions["timeout"]
    assert provider._timeout_seconds == 300.0
    assert timeout["connect"] == 300.0
    assert timeout["read"] == 300.0
    assert timeout["write"] == 300.0
    assert timeout["pool"] == 300.0


def test_non_json_retries_five_times_then_raises_invalid() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat("not json at all"))

    provider, seen = _provider(handler)
    with pytest.raises(VisionResponseInvalidError) as raised:
        asyncio.run(provider.analyze_images(_request()))

    assert len(seen) == 6
    assert "return JSON only" not in _user_text(seen[0])
    assert "return JSON only" in _user_text(seen[1])
    assert "The previous answer was rejected." in _user_text(seen[5])
    assert raised.value.code == "VISION_INVALID_RESPONSE"
    message = f"{raised.value} {raised.value!r}"
    assert SECRET not in message
    assert "PLAINTEXT-IMAGE-SENTINEL" not in message
    assert ANALYSIS_ID not in message


def test_non_json_then_valid_json_returns_the_retry() -> None:
    contents = iter(("not json", json.dumps(_CLASSIFY)))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat(next(contents)))

    provider, seen = _provider(handler)
    response = asyncio.run(provider.analyze_images(_request()))

    assert len(seen) == 2
    assert "return JSON only" in _user_text(seen[1])
    assert response.raw_json["results"][0]["document_class"] == "loan_document"


def test_json_object_wrong_type_is_sent_back_with_feedback() -> None:
    bad = json.dumps({"results": [{"image_id": "img_a", "document_class": "newspaper"}]})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat(bad))

    provider, seen = _provider(handler)
    with pytest.raises(VisionResponseInvalidError) as raised:
        asyncio.run(provider.analyze_images(_request()))

    assert len(seen) == 6
    assert "return JSON only" not in _user_text(seen[0])
    assert "document_class" in _user_text(seen[1])
    assert "Do not invent a lender, amount, date, or identity." in _user_text(seen[1])
    assert raised.value.code == "VISION_INVALID_RESPONSE"
    assert SECRET not in str(raised.value)
    assert SECRET not in repr(raised.value)


def test_503_raises_unavailable_without_retry_or_key() -> None:
    body = f"upstream failed {SECRET} PLAINTEXT-IMAGE-SENTINEL"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=body)

    provider, seen = _provider(handler, vision_base_url="")
    with pytest.raises(VisionUnavailableError) as raised:
        asyncio.run(provider.analyze_images(_request()))

    assert len(seen) == 1
    assert str(seen[0].url) == f"{DEFAULT_BASE_URL}/chat/completions"
    assert raised.value.code == "VISION_UNAVAILABLE"
    message = f"{raised.value} {raised.value!r}"
    assert SECRET not in message
    assert "upstream failed" not in message
    assert "PLAINTEXT-IMAGE-SENTINEL" not in message


def test_timeout_and_504_do_not_retry() -> None:
    def timeout_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out while holding the key")

    provider, seen = _provider(timeout_handler)
    with pytest.raises(VisionTimeoutError) as raised:
        asyncio.run(provider.analyze_images(_request()))

    assert len(seen) == 1
    assert raised.value.code == "VISION_TIMEOUT"
    assert SECRET not in str(raised.value)
    assert "timed out while holding the key" not in str(raised.value)

    def gateway_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text=SECRET)

    provider, seen = _provider(gateway_handler)
    with pytest.raises(VisionTimeoutError) as raised:
        asyncio.run(provider.analyze_images(_request()))

    assert len(seen) == 1
    assert SECRET not in str(raised.value)


def test_redirect_is_not_followed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    provider, seen = _provider(handler)
    with pytest.raises(VisionUnavailableError):
        asyncio.run(provider.analyze_images(_request()))

    assert len(seen) == 1


def test_missing_key_does_not_call_the_network() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network was called")

    provider, seen = _provider(handler, vision_api_key="  ")
    with pytest.raises(VisionUnavailableError) as raised:
        asyncio.run(provider.analyze_images(_request()))

    assert seen == []
    assert "not configured" in str(raised.value)
    assert SECRET not in repr(provider)
