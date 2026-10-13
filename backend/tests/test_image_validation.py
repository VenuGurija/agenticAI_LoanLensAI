from io import BytesIO

import pytest
from PIL import Image

from app.core.errors import AppError
from app.services.images.preprocess import validate_image_bytes


def _image_bytes(width: int, height: int, image_format: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (12, 80, 160)).save(buffer, format=image_format)
    return buffer.getvalue()


def test_png_200_by_200_is_valid() -> None:
    result = validate_image_bytes(_image_bytes(200, 200, "PNG"))

    assert result.content_type == "image/png"
    assert result.width == 200
    assert result.height == 200


def test_png_1_by_1_is_too_small() -> None:
    with pytest.raises(AppError) as caught:
        validate_image_bytes(_image_bytes(1, 1, "PNG"))

    assert caught.value.code == "IMAGE_TOO_SMALL"


def test_random_bytes_are_unreadable() -> None:
    with pytest.raises(AppError) as caught:
        validate_image_bytes(b"\x00\x01\x02not-an-image\xff")

    assert caught.value.code == "IMAGE_UNREADABLE"


def test_short_edge_below_200_is_too_small() -> None:
    with pytest.raises(AppError) as caught:
        validate_image_bytes(_image_bytes(200, 199, "PNG"))

    assert caught.value.code == "IMAGE_TOO_SMALL"


def test_jpeg_200_by_200_is_valid() -> None:
    result = validate_image_bytes(_image_bytes(200, 200, "JPEG"), content_type="image/jpeg")

    assert result.content_type == "image/jpeg"
    assert result.width == 200
    assert result.height == 200


def test_gif_is_unsupported_type() -> None:
    with pytest.raises(AppError) as caught:
        validate_image_bytes(_image_bytes(200, 200, "GIF"))

    assert caught.value.code == "IMAGE_UNSUPPORTED_TYPE"


def test_declared_mime_must_be_allowlisted() -> None:
    png = _image_bytes(200, 200, "PNG")

    with pytest.raises(AppError) as caught:
        validate_image_bytes(png, content_type="image/gif")

    assert caught.value.code == "IMAGE_UNSUPPORTED_TYPE"


def test_declared_mime_must_match_decoded_type() -> None:
    png = _image_bytes(200, 200, "PNG")

    with pytest.raises(AppError) as caught:
        validate_image_bytes(png, content_type="image/jpeg")

    assert caught.value.code == "IMAGE_UNSUPPORTED_TYPE"


def test_bytes_over_max_are_too_large() -> None:
    png = _image_bytes(200, 200, "PNG")

    with pytest.raises(AppError) as caught:
        validate_image_bytes(png, max_bytes=len(png) - 1)

    assert caught.value.code == "IMAGE_TOO_LARGE"


def test_heic_without_decoder_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.images.preprocess.heic_decoder_available",
        lambda: False,
    )
    data = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 24

    with pytest.raises(AppError) as caught:
        validate_image_bytes(data)

    assert caught.value.code == "IMAGE_UNSUPPORTED_TYPE"
