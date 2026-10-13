"""Book, draft, upload, and inbox responses (SPEC §14.3, §14.4, §14.5, §14.6, §14.12)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import AnalysisStatus, ImageStatus

InboxBucket = Literal["queued", "running", "ready", "needs_review", "failed"]


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookCreated(BaseModel):
    book_id: str
    created_at: str


class DraftCreated(BaseModel):
    book_id: str
    analysis_id: str
    status: AnalysisStatus
    created_at: str
    expires_at: str


class DraftAccepted(BaseModel):
    book_id: str
    loan_internal_ids: list[str]


class BookGet(BaseModel):
    book_id: str
    created_at: str
    lender_groups: list[dict[str, object]] = Field(default_factory=list)


class AnalysisErrorBody(BaseModel):
    code: str
    message: str


class UploadedImage(BaseModel):
    image_id: str
    filename: str
    content_type: str
    byte_size: int
    status: ImageStatus


class ImageUploadResult(BaseModel):
    analysis_id: str
    status: AnalysisStatus
    images: list[UploadedImage]


class AnalysisGet(BaseModel):
    analysis_id: str
    status: AnalysisStatus
    created_at: str
    completed_at: str | None
    expires_at: str | None
    error: AnalysisErrorBody | None
    pages_done: int
    pages_total: int
    queued_at: str | None
    images: list[UploadedImage] = Field(default_factory=list)
    lender_groups: list[dict[str, object]] = Field(default_factory=list)


class InboxCounts(BaseModel):
    queued: int
    running: int
    ready: int
    needs_review: int
    failed: int


class InboxDraft(BaseModel):
    """One inbox row. Amounts, borrower names, and summary lines are not fields."""

    analysis_id: str
    status: AnalysisStatus
    inbox_bucket: InboxBucket
    pages_done: int
    pages_total: int
    image_count: int
    created_at: str
    queued_at: str | None
    retryable: bool
    error: AnalysisErrorBody | None


class BookInbox(BaseModel):
    book_id: str
    counts: InboxCounts
    drafts: list[InboxDraft]
