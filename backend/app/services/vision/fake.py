"""Fixture-backed vision provider (SPEC §6.3).

Maps an image filename (via `image_id`) or SHA-256 checksum to checked-in JSON.
Missing keys raise. This provider does not invent amounts, dates, or loans.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from app.schemas.vision import VisionImage, VisionRequest, VisionResponse
from app.services.vision.base import VisionResponseInvalidError


class UnknownVisionFixtureError(Exception):
    """No checked-in fixture matches this image."""


def default_fixture_dir() -> Path:
    backend_dir = Path(__file__).resolve().parents[3]
    return backend_dir / "tests" / "fixtures" / "vision"


class FakeVisionProvider:
    """Returns fixture JSON only. `provider_name` and `model_name` are `fake`."""

    provider_name = "fake"
    model_name = "fake"

    def __init__(
        self,
        fixture_dir: Path | None = None,
        checksum_index: dict[str, str] | None = None,
    ) -> None:
        self._fixture_dir = (fixture_dir or default_fixture_dir()).resolve()
        self._checksum_index = dict(checksum_index or {})

    async def analyze_images(self, request: VisionRequest) -> VisionResponse:
        if not request.images:
            raise UnknownVisionFixtureError("Vision request has no images.")

        if len(request.images) == 1:
            raw_json = self._raw_json_for(request.images[0])
        else:
            raw_json = {
                image.image_id: self._raw_json_for(image) for image in request.images
            }

        return VisionResponse(
            raw_json=raw_json,
            model_name=self.model_name,
            provider_name=self.provider_name,
            usage=None,
        )

    def _raw_json_for(self, image: VisionImage) -> dict[str, Any]:
        path = self._fixture_path(image)
        if path is None:
            raise UnknownVisionFixtureError(
                f"No vision fixture for image {image.image_id!r}."
            )
        return _load_fixture_object(path)

    def _fixture_path(self, image: VisionImage) -> Path | None:
        filename = Path(image.image_id).name
        for key in (filename, Path(filename).stem):
            path = self._named_fixture(key)
            if path is not None:
                return path

        digest = hashlib.sha256(image.bytes).hexdigest()
        mapped = self._checksum_index.get(digest)
        if mapped is not None:
            return self._named_fixture(mapped)

        checksum_file = self._named_fixture(digest)
        if checksum_file is not None:
            return checksum_file
        return None

    def _named_fixture(self, key: str) -> Path | None:
        if not key or key in {".", ".."} or any(sep in key for sep in ("/", "\\")):
            return None
        stem = Path(key).stem
        if not stem or stem in {".", ".."}:
            return None
        path = (self._fixture_dir / f"{stem}.json").resolve()
        root = self._fixture_dir.resolve()
        if not path.is_relative_to(root):
            return None
        if path.is_file():
            return path
        return None


def _load_fixture_object(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VisionResponseInvalidError("Vision fixture is not valid JSON.") from exc
    if not isinstance(loaded, dict):
        raise VisionResponseInvalidError("Vision fixture JSON must be an object.")
    return loaded
