import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.schemas.vision import VisionImage, VisionRequest, VisionResponse
from app.services.vision.base import VisionResponseInvalidError
from app.services.vision.factory import get_vision_provider
from app.services.vision.fake import FakeVisionProvider, UnknownVisionFixtureError
from app.services.vision.openai_compatible import OpenAICompatibleVisionProvider

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "vision"
_ANALYSIS_ID = "anl_test_correlation"


def _load(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _request(
    *,
    task: str,
    image_id: str,
    image_bytes: bytes = b"png-bytes",
    schema_name: str = "fixture",
) -> VisionRequest:
    return VisionRequest(
        analysis_id=_ANALYSIS_ID,
        task=task,  # type: ignore[arg-type]
        images=[
            VisionImage(
                image_id=image_id,
                media_type="image/png",
                bytes=image_bytes,
            )
        ],
        response_schema_name=schema_name,
        hint="same loan pages",
    )


def test_settings_default_provider_is_fake() -> None:
    assert Settings.model_fields["vision_provider"].default == "fake"


def test_factory_returns_fake_and_classify_fixture() -> None:
    provider = get_vision_provider(Settings(vision_provider="fake"))
    assert isinstance(provider, FakeVisionProvider)

    response = asyncio.run(provider.analyze_images(_request(task="classify", image_id="classify_loan")))

    assert isinstance(response, VisionResponse)
    assert response.raw_json == _load("classify_loan.json")
    assert response.model_name == "fake"
    assert response.provider_name == "fake"
    assert response.usage is None
    assert _ANALYSIS_ID not in json.dumps(response.raw_json)


def test_extract_returns_fixture_without_extra_amounts() -> None:
    provider = FakeVisionProvider()
    response = asyncio.run(
        provider.analyze_images(
            _request(task="extract", image_id="extract_sample.json", schema_name="extract")
        )
    )

    fixture = _load("extract_sample.json")
    assert response.raw_json == fixture
    assert response.raw_json["sanctioned_amount"] is None
    assert response.raw_json["disbursed_amount"] is None
    assert response.raw_json["repayment_amount"] is None
    assert response.raw_json["fees"] is None
    assert "gst" not in response.raw_json
    assert "apr" not in response.raw_json


def test_checksum_maps_to_fixture_json() -> None:
    image_bytes = b"checked-in-image"
    digest = hashlib.sha256(image_bytes).hexdigest()
    provider = FakeVisionProvider(checksum_index={digest: "classify_loan"})

    response = asyncio.run(
        provider.analyze_images(
            _request(
                task="classify",
                image_id="img_not_a_fixture_name",
                image_bytes=image_bytes,
            )
        )
    )

    assert response.raw_json == _load("classify_loan.json")


def test_unknown_fixture_key_does_not_fabricate_a_loan() -> None:
    provider = FakeVisionProvider()

    with pytest.raises(UnknownVisionFixtureError) as raised:
        asyncio.run(
            provider.analyze_images(
                _request(task="extract", image_id="img_unknown", image_bytes=b"no-fixture")
            )
        )

    message = str(raised.value)
    assert "img_unknown" in message
    for invented in ("15000", "sanctioned", "disbursed", "borrower", "loan_document"):
        assert invented not in message.lower()


def test_invalid_fixture_json_raises_without_a_loan(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("not-json", encoding="utf-8")
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    provider = FakeVisionProvider(fixture_dir=tmp_path)

    with pytest.raises(VisionResponseInvalidError, match="not valid JSON"):
        asyncio.run(provider.analyze_images(_request(task="classify", image_id="broken")))
    with pytest.raises(VisionResponseInvalidError, match="must be an object"):
        asyncio.run(provider.analyze_images(_request(task="extract", image_id="list")))


def test_factory_openai_compatible_hides_the_key() -> None:
    secret = "sk-test-secret-value"
    provider = get_vision_provider(
        Settings(
            vision_provider="openai_compatible",
            vision_api_key=secret,
            vision_model="gpt-test",
        )
    )

    assert isinstance(provider, OpenAICompatibleVisionProvider)
    assert secret not in repr(provider)
    assert secret not in str(provider)


def test_request_rejects_unknown_task() -> None:
    with pytest.raises(ValidationError):
        VisionRequest(
            analysis_id=_ANALYSIS_ID,
            task="summarize",  # type: ignore[arg-type]
            images=[VisionImage(image_id="classify_loan", media_type="image/png", bytes=b"x")],
            response_schema_name="classify",
        )
