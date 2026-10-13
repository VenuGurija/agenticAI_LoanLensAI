"""Persistence status enums (SPEC §15.2)."""

from enum import StrEnum


class AnalysisStatus(StrEnum):
    CREATED = "created"
    IMAGES_PENDING = "images_pending"
    QUEUED = "queued"
    RUNNING = "running"
    PREPROCESSING = "preprocessing"
    CLASSIFYING = "classifying"
    EXTRACTING = "extracting"
    GROUPING = "grouping"
    VALIDATING = "validating"
    COMPLETED = "completed"
    COMPLETED_NEEDS_REVIEW = "completed_needs_review"
    ACCEPTED = "accepted"
    FAILED = "failed"
    EXPIRED = "expired"


class ImageStatus(StrEnum):
    UPLOADED = "uploaded"
    REJECTED = "rejected"
    PREPROCESSED = "preprocessed"
    CLASSIFIED = "classified"
    FAILED = "failed"
