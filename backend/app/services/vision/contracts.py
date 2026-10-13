"""JSON contracts for vision tasks (SPEC §6.5, §8.1, §9.7).

One classify or extract call returns a single object. Each entry in `results`
is the per-image payload from the spec. Calculated fields are not part of
either contract. Missing money stays null.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.extraction import Confidence, RepaymentFrequency

CLASSIFY_SCHEMA_NAME = "classify"
EXTRACT_SCHEMA_NAME = "extract"


class DocumentClass(StrEnum):
    LOAN_DOCUMENT = "loan_document"
    NOT_LOAN_DOCUMENT = "not_loan_document"
    UNCLEAR = "unclear"


class PageRole(StrEnum):
    UNKNOWN = "unknown"
    SANCTION = "sanction"
    DISBURSEMENT = "disbursement"
    REPAYMENT_SCHEDULE = "repayment_schedule"
    CHARGES = "charges"
    OTHER_LOAN_PAGE = "other_loan_page"


class ImageClassification(BaseModel):
    """One image in a classify response (SPEC §8.1)."""

    model_config = ConfigDict(extra="forbid")

    image_id: str = Field(min_length=1)
    document_class: DocumentClass
    confidence: Confidence = Confidence.UNKNOWN
    page_role: PageRole = PageRole.UNKNOWN
    notes: str | None = None


class ClassifyContract(BaseModel):
    """`classify` response: one result per image in the call."""

    model_config = ConfigDict(extra="forbid")

    results: list[ImageClassification]


class _VisionValue(BaseModel):
    """A printed value. No `source` and no calculated figure."""

    model_config = ConfigDict(extra="forbid")

    confidence: Confidence = Confidence.UNKNOWN


class VisionStringField(_VisionValue):
    value: str | None = None


class VisionMoneyField(_VisionValue):
    value: str | None = None
    currency: str | None = None


class VisionDateField(_VisionValue):
    value: str | None = None


class VisionRateField(_VisionValue):
    value: str | None = None
    unit: Literal["percent"] | None = None


class VisionTenureField(_VisionValue):
    value: int | None = None
    unit: Literal["days", "months"] | None = None


class VisionEnumField(_VisionValue):
    value: RepaymentFrequency | None = None


class VisionPenalty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    amount: VisionMoneyField | None = None
    rate: VisionRateField | None = None
    description: str | None = None


class VisionLoan(BaseModel):
    """One loan inside an extract result. Omitted fields stay null."""

    model_config = ConfigDict(extra="forbid")

    lender_brand: VisionStringField = Field(default_factory=VisionStringField)
    legal_lending_entity: VisionStringField = Field(default_factory=VisionStringField)
    borrower_name: VisionStringField = Field(default_factory=VisionStringField)
    loan_id: VisionStringField = Field(default_factory=VisionStringField)
    sanctioned_amount: VisionMoneyField = Field(default_factory=VisionMoneyField)
    disbursed_amount: VisionMoneyField = Field(default_factory=VisionMoneyField)
    processing_fee: VisionMoneyField = Field(default_factory=VisionMoneyField)
    gst_amount: VisionMoneyField = Field(default_factory=VisionMoneyField)
    other_fees_total: VisionMoneyField = Field(default_factory=VisionMoneyField)
    interest_rate: VisionRateField = Field(default_factory=VisionRateField)
    apr: VisionRateField = Field(default_factory=VisionRateField)
    tenure: VisionTenureField = Field(default_factory=VisionTenureField)
    sanction_date: VisionDateField = Field(default_factory=VisionDateField)
    disbursement_date: VisionDateField = Field(default_factory=VisionDateField)
    repayment_amount: VisionMoneyField = Field(default_factory=VisionMoneyField)
    repayment_date: VisionDateField = Field(default_factory=VisionDateField)
    repayment_frequency: VisionEnumField = Field(default_factory=VisionEnumField)
    penalties: list[VisionPenalty] = Field(default_factory=list)
    same_loan_as_image_ids: list[str] = Field(default_factory=list)


class VisionImageExtract(BaseModel):
    """One image in an extract response (SPEC §9.7)."""

    model_config = ConfigDict(extra="forbid")

    image_id: str = Field(min_length=1)
    loans: list[VisionLoan] = Field(default_factory=list)


class ExtractContract(BaseModel):
    """`extract` response after printed-shape normalization.

    `normalize_printed_extract` turns bare strings, tenure text, grouped money,
    a printed percent sign, and printed dates into this object before validation.
    A missing amount stays null.
    """

    model_config = ConfigDict(extra="forbid")

    results: list[VisionImageExtract]


_TASK_MODELS: dict[str, type[BaseModel]] = {
    CLASSIFY_SCHEMA_NAME: ClassifyContract,
    EXTRACT_SCHEMA_NAME: ExtractContract,
}


def json_schema_for(task: str) -> dict[str, Any]:
    """Pydantic JSON Schema for `classify` or `extract`."""

    model = _model_for(task)
    schema = model.model_json_schema()
    schema["title"] = task
    return schema


def classify_json_schema() -> dict[str, Any]:
    return json_schema_for(CLASSIFY_SCHEMA_NAME)


def extract_json_schema() -> dict[str, Any]:
    return json_schema_for(EXTRACT_SCHEMA_NAME)


def validate_contract(task: str, data: Any) -> ClassifyContract | ExtractContract:
    """Parse model JSON into the contract for `task`."""

    return _model_for(task).model_validate(data)


def _model_for(task: str) -> type[BaseModel]:
    try:
        return _TASK_MODELS[task]
    except KeyError as exc:
        raise ValueError(f"Unknown vision contract: {task}") from exc
