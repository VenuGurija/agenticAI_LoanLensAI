"""Canonical extraction fields (SPEC §9).

Money is a two-place decimal string or null. Floats are rejected.
Omitted loan fields stay null; this module does not invent 0.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_TWO_PLACES = Decimal("0.01")


class FieldSource(StrEnum):
    EXTRACTED = "extracted"
    CALCULATED = "calculated"
    USER_CORRECTED = "user_corrected"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class RepaymentFrequency(StrEnum):
    ONE_TIME = "one_time"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    UNKNOWN = "unknown"


class FieldEvidence(BaseModel):
    """Optional pointer back to the screenshot. Not required to keep a value."""

    model_config = ConfigDict(extra="forbid")

    image_id: str | None = None
    label_text: str | None = None


def _with_default_source(data: Any) -> Any:
    """LLM extract JSON may omit source. Null value → unknown; otherwise extracted."""

    if not isinstance(data, dict) or "source" in data:
        return data
    source = "unknown" if data.get("value") is None else "extracted"
    return {**data, "source": source}


def _reject_unknown_with_value(source: FieldSource, value: Any) -> None:
    if source == FieldSource.UNKNOWN and value is not None:
        raise ValueError("source unknown requires a null value")


class _SourcedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: FieldSource
    confidence: Confidence = Confidence.UNKNOWN
    evidence: FieldEvidence | None = None

    @model_validator(mode="before")
    @classmethod
    def fill_source(cls, data: Any) -> Any:
        return _with_default_source(data)


def _parse_decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{label} must be a decimal string or integer, not a float")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if text == "" or "e" in text.lower():
            raise ValueError(f"{label} must be a decimal string or integer")
        try:
            amount = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"{label} must be a decimal string or integer") from exc
        if not amount.is_finite():
            raise ValueError(f"{label} must be a finite decimal")
        return amount
    raise ValueError(f"{label} must be a decimal string, integer, or null")


def parse_money_value(value: Any) -> str | None:
    if value is None:
        return None
    amount = _parse_decimal(value, label="money value")
    quantized = amount.quantize(_TWO_PLACES)
    if quantized != amount:
        raise ValueError("money value must have at most 2 decimal places")
    return format(quantized, "f")


def parse_rate_value(value: Any) -> str | None:
    if value is None:
        return None
    amount = _parse_decimal(value, label="rate value")
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_string_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ValueError("string field value must be a string or null")


def parse_tenure_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("tenure value must be an integer or null")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        digits = text[1:] if text.startswith("-") else text
        if digits.isdigit():
            return int(text)
    raise ValueError("tenure value must be an integer or null")


class StringField(_SourcedField):
    value: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def string_value(cls, value: Any) -> str | None:
        return parse_string_value(value)

    @model_validator(mode="after")
    def unknown_value_is_null(self) -> Self:
        _reject_unknown_with_value(self.source, self.value)
        return self


class MoneyField(_SourcedField):
    value: str | None = None
    currency: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def money_value(cls, value: Any) -> str | None:
        return parse_money_value(value)

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 3 or not value.isalpha() or not value.isupper():
            raise ValueError("currency must be an ISO 4217 code or null")
        return value

    @model_validator(mode="after")
    def unknown_value_is_null(self) -> Self:
        _reject_unknown_with_value(self.source, self.value)
        return self


class DateField(_SourcedField):
    value: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def date_value(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("date must be YYYY-MM-DD or null")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date must be YYYY-MM-DD or null") from exc
        if parsed.isoformat() != value:
            raise ValueError("date must be YYYY-MM-DD or null")
        return value

    @model_validator(mode="after")
    def unknown_value_is_null(self) -> Self:
        _reject_unknown_with_value(self.source, self.value)
        return self


class EnumField(_SourcedField):
    value: RepaymentFrequency | None = None

    @model_validator(mode="after")
    def unknown_value_is_null(self) -> Self:
        _reject_unknown_with_value(self.source, self.value)
        return self


class RateField(_SourcedField):
    value: str | None = None
    unit: Literal["percent"] | None = None

    @field_validator("value", mode="before")
    @classmethod
    def rate_value(cls, value: Any) -> str | None:
        return parse_rate_value(value)

    @model_validator(mode="after")
    def unknown_value_is_null(self) -> Self:
        _reject_unknown_with_value(self.source, self.value)
        return self


class TenureField(_SourcedField):
    value: int | None = None
    unit: Literal["days", "months"] | None = None

    @field_validator("value", mode="before")
    @classmethod
    def tenure_value(cls, value: Any) -> int | None:
        return parse_tenure_value(value)

    @model_validator(mode="after")
    def unknown_value_is_null(self) -> Self:
        _reject_unknown_with_value(self.source, self.value)
        return self


class PenaltyField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    amount: MoneyField | None = None
    rate: RateField | None = None
    description: str | None = None


def _null_string() -> StringField:
    return StringField(value=None, source=FieldSource.UNKNOWN, confidence=Confidence.UNKNOWN)


def _null_money() -> MoneyField:
    return MoneyField(
        value=None,
        currency=None,
        source=FieldSource.UNKNOWN,
        confidence=Confidence.UNKNOWN,
    )


def _null_date() -> DateField:
    return DateField(value=None, source=FieldSource.UNKNOWN, confidence=Confidence.UNKNOWN)


def _null_enum() -> EnumField:
    return EnumField(value=None, source=FieldSource.UNKNOWN, confidence=Confidence.UNKNOWN)


def _null_rate() -> RateField:
    return RateField(
        value=None,
        unit=None,
        source=FieldSource.UNKNOWN,
        confidence=Confidence.UNKNOWN,
    )


def _null_tenure() -> TenureField:
    return TenureField(
        value=None,
        unit=None,
        source=FieldSource.UNKNOWN,
        confidence=Confidence.UNKNOWN,
    )


class ExtractedLoan(BaseModel):
    """One loan object from an image extract. Missing money stays null."""

    model_config = ConfigDict(extra="forbid")

    lender_brand: StringField = Field(default_factory=_null_string)
    legal_lending_entity: StringField = Field(default_factory=_null_string)
    borrower_name: StringField = Field(default_factory=_null_string)
    loan_id: StringField = Field(default_factory=_null_string)
    sanctioned_amount: MoneyField = Field(default_factory=_null_money)
    disbursed_amount: MoneyField = Field(default_factory=_null_money)
    processing_fee: MoneyField = Field(default_factory=_null_money)
    gst_amount: MoneyField = Field(default_factory=_null_money)
    other_fees_total: MoneyField = Field(default_factory=_null_money)
    interest_rate: RateField = Field(default_factory=_null_rate)
    apr: RateField = Field(default_factory=_null_rate)
    tenure: TenureField = Field(default_factory=_null_tenure)
    sanction_date: DateField = Field(default_factory=_null_date)
    disbursement_date: DateField = Field(default_factory=_null_date)
    repayment_amount: MoneyField = Field(default_factory=_null_money)
    repayment_date: DateField = Field(default_factory=_null_date)
    repayment_frequency: EnumField = Field(default_factory=_null_enum)
    penalties: list[PenaltyField] = Field(default_factory=list)
    same_loan_as_image_ids: list[str] = Field(default_factory=list)


class ImageExtractResult(BaseModel):
    """Per-image extract JSON (SPEC §9.7). No loans means nothing was extracted."""

    model_config = ConfigDict(extra="forbid")

    image_id: str = Field(min_length=1)
    loans: list[ExtractedLoan] = Field(default_factory=list)
