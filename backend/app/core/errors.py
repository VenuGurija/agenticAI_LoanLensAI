"""API error envelope (SPEC §14.8, §14.9)."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

_LOC_OR_TYPE = re.compile(r"[A-Za-z0-9_.]*")

_DETAIL_FORBIDDEN = frozenset({"borrower_name", "loan_id", "amount", "body", "request_body", "image_bytes"})


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    status_code: int
    retryable: bool
    message: str


ERROR_SPECS: dict[str, ErrorSpec] = {
    "ANALYSIS_NOT_FOUND": ErrorSpec(404, False, "Analysis not found."),
    "BOOK_NOT_FOUND": ErrorSpec(404, False, "Book not found."),
    "LOAN_NOT_FOUND": ErrorSpec(404, False, "Loan not found."),
    "INVALID_STATUS": ErrorSpec(409, False, "This action is not allowed for the current status."),
    "NO_IMAGES": ErrorSpec(400, False, "At least one image is required."),
    "TOO_MANY_IMAGES": ErrorSpec(400, False, "Too many images were uploaded for one analysis."),
    "IMAGE_TOO_LARGE": ErrorSpec(400, False, "Each image must be 10 MiB or smaller."),
    "IMAGE_UNSUPPORTED_TYPE": ErrorSpec(400, False, "This image type is not supported."),
    "IMAGE_UNREADABLE": ErrorSpec(400, False, "The image could not be read."),
    "IMAGE_TOO_SMALL": ErrorSpec(400, False, "The image is too small to read."),
    "NO_LOAN_DOCUMENTS": ErrorSpec(422, False, "No loan documents were found."),
    "VISION_UNAVAILABLE": ErrorSpec(503, True, "The vision service is unavailable."),
    "VISION_INVALID_RESPONSE": ErrorSpec(502, True, "The vision service returned an invalid response."),
    "VISION_TIMEOUT": ErrorSpec(504, True, "The vision service timed out."),
    "INTERNAL_ERROR": ErrorSpec(500, True, "Something went wrong."),
}


def vision_invalid_message(failures: Sequence[Mapping[str, object]] | None) -> str:
    """Fixed sentence plus loc and type only. No rejected value and no PII."""

    sentence = ERROR_SPECS["VISION_INVALID_RESPONSE"].message
    lines = [sentence]
    if not failures:
        return sentence
    for item in failures:
        loc = item.get("loc")
        kind = item.get("type")
        if not isinstance(loc, str) or not isinstance(kind, str):
            continue
        loc = loc.strip()
        kind = kind.strip()
        if not kind or _LOC_OR_TYPE.fullmatch(loc) is None or _LOC_OR_TYPE.fullmatch(kind) is None:
            continue
        if loc:
            lines.append(f"{loc} ({kind})")
        else:
            lines.append(f"({kind})")
    return "\n".join(lines)


def sanitize_details(details: Mapping[str, object] | None) -> dict[str, object]:
    """Drop PII and non-scalar values from error `details`."""
    if not details:
        return {}
    clean: dict[str, object] = {}
    for key, value in details.items():
        if key.lower() in _DETAIL_FORBIDDEN:
            continue
        if isinstance(value, (bytes, bytearray)):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
    return clean


def error_envelope(
    code: str,
    message: str,
    retryable: bool,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": sanitize_details(details),
        }
    }


class AppError(Exception):
    """Domain error whose HTTP status and retryable flag come from SPEC §14.9."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        try:
            spec = ERROR_SPECS[code]
        except KeyError as exc:
            raise ValueError(f"Unknown error code: {code}") from exc
        self.code = code
        self.status_code = spec.status_code
        self.retryable = spec.retryable
        self.message = spec.message if message is None else message
        self.details = sanitize_details(details)
        super().__init__(self.message)

    def envelope(self) -> dict[str, object]:
        return error_envelope(self.code, self.message, self.retryable, self.details)
