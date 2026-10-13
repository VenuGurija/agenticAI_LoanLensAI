"""Select the vision adapter from `VISION_PROVIDER` (SPEC §6.3)."""

from app.core.config import Settings, get_settings
from app.services.vision.base import VisionProvider
from app.services.vision.fake import FakeVisionProvider
from app.services.vision.openai_compatible import OpenAICompatibleVisionProvider


def get_vision_provider(settings: Settings | None = None) -> VisionProvider:
    """`fake` is the default so tests do not call a live vision API."""
    selected = settings if settings is not None else get_settings()
    if selected.vision_provider == "fake":
        return FakeVisionProvider()
    return OpenAICompatibleVisionProvider(selected)
