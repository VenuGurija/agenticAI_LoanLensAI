"""Vision request and response types (SPEC §6.2)."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class VisionImage(BaseModel):
    """One preprocessed image. `bytes` are not logged."""

    model_config = ConfigDict(extra="forbid")

    image_id: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    bytes: bytes


class VisionUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int
    output_tokens: int


class VisionRequest(BaseModel):
    """Correlation and task for one provider call.

    `analysis_id` stays in the app. V1 does not send it to a vendor.
    `hint` is a non-sensitive grouping hint, never prior-analysis PII.
    """

    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(min_length=1)
    task: Literal["classify", "extract"]
    images: list[VisionImage]
    response_schema_name: str = Field(min_length=1)
    hint: str | None = None


class VisionResponse(BaseModel):
    """Structured JSON from a provider. Callers do not fill missing money."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    raw_json: dict[str, Any]
    model_name: str
    provider_name: str
    usage: VisionUsage | None = None
