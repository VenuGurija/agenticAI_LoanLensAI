"""Vision provider protocol (SPEC §6.1, §6.2).

Application services depend on this protocol, not on a vendor SDK.
"""

from typing import Protocol

from app.schemas.vision import VisionRequest, VisionResponse


class VisionResponseInvalidError(Exception):
    """The model returned non-JSON or JSON that failed schema validation."""

    def __init__(
        self,
        message: str = "The vision service returned an invalid response.",
        *,
        failures: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.failures = [] if failures is None else list(failures)


class VisionProvider(Protocol):
    async def analyze_images(self, request: VisionRequest) -> VisionResponse: ...
