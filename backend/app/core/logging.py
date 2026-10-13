"""PII-safe structured logging (SPEC §16, §23)."""

import json
import logging
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

LOGGER_NAME = "loanlens"
# One append-only file for vision rejections. Repo-root data/logs, not the process cwd.
VISION_LOG_PATH = Path(__file__).resolve().parents[3] / "data" / "logs" / "vision.log"
_VISION_LOG_LOCK = threading.Lock()

# SPEC §16: do not log borrower, external loan id, or amounts.
_FORBIDDEN_LOG_KEYS = frozenset({"borrower_name", "loan_id", "amount"})

# SPEC §23 fields, plus ids and token counts from §16.
# `model_answer` is not allowlisted here. Only `log_vision_response_rejected`
# attaches it, and only when LOANLENS_ENV=development.
_ALLOWED_LOG_KEYS = frozenset(
    {
        "analysis_id",
        "image_id",
        "book_id",
        "loan_internal_id",
        "lender_group_id",
        "status",
        "duration_ms",
        "provider",
        "model",
        "error_code",
        "token_count",
        "task",
        "attempt",
        "schema_name",
    }
)


def safe_log_extra(fields: Mapping[str, object]) -> dict[str, object]:
    """Keep ids, status, and error codes. Drop `borrower_name`, `loan_id`, and `amount`."""
    safe: dict[str, object] = {}
    for key, value in fields.items():
        if key.lower() in _FORBIDDEN_LOG_KEYS or key not in _ALLOWED_LOG_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
    return safe


def validation_failure_items(exc: ValidationError) -> list[dict[str, str]]:
    """Locations and error types only. The rejected value stays out of this list."""

    items: list[dict[str, str]] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        if not isinstance(loc, tuple):
            loc = ()
        err_type = err.get("type")
        if not isinstance(err_type, str) or not err_type:
            continue
        items.append({"loc": ".".join(str(part) for part in loc), "type": err_type})
    if items:
        return items
    return [{"loc": "", "type": "value_error"}]


def model_answer_text(raw: object) -> str | None:
    """Text of a rejected model payload. Bytes are not converted into a log string."""

    if isinstance(raw, str):
        return raw
    if isinstance(raw, (dict, list)):
        try:
            return json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
    return None


def _public_failures(failures: object) -> list[dict[str, str]]:
    if not isinstance(failures, Sequence) or isinstance(failures, (str, bytes)):
        return []
    public: list[dict[str, str]] = []
    for item in failures:
        if not isinstance(item, Mapping):
            continue
        loc = item.get("loc")
        err_type = item.get("type")
        if isinstance(loc, str) and isinstance(err_type, str) and err_type:
            public.append({"loc": loc, "type": err_type})
    return public


def _clean_model_answer(
    text: str,
    secrets: Sequence[str],
    image_markers: Sequence[str],
) -> str:
    cleaned = text
    for marker in sorted((item for item in image_markers if item), key=len, reverse=True):
        cleaned = cleaned.replace(marker, "[REDACTED]")
    return redact_secrets(cleaned, secrets)


def log_vision_response_rejected(
    *,
    analysis_id: str | None,
    task: str,
    attempt: int | None,
    schema_name: str,
    failures: Sequence[Mapping[str, object]],
    model_answer: str | None,
    environment: str,
    secrets: Sequence[str] = (),
    image_markers: Sequence[str] = (),
) -> None:
    """Log one rejected vision answer.

    `model_answer` is kept only when `environment` is `development`.
    Image markers and secrets are removed before the record is built.
    """

    fields: dict[str, object] = {
        "analysis_id": analysis_id,
        "task": task,
        "attempt": attempt,
        "schema_name": schema_name,
        "validation_failures": _public_failures(failures),
    }
    if environment == "development" and isinstance(model_answer, str):
        fields["model_answer"] = _clean_model_answer(model_answer, secrets, image_markers)
    logging.getLogger(LOGGER_NAME).log(
        logging.WARNING,
        "vision_response_rejected",
        extra={"safe_extra": fields},
    )
    append_vision_log("vision_response_rejected", fields)


def append_vision_log(event: str, fields: Mapping[str, object]) -> None:
    """Append one JSON line to data/logs/vision.log. A write failure does not stop analysis."""

    record: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "event": event,
    }
    for key, value in fields.items():
        if isinstance(value, (str, int, float, bool)) or value is None or isinstance(value, list):
            record[key] = value
    try:
        VISION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with _VISION_LOG_LOCK:
            with VISION_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError:
        return


def redact_secrets(text: str, secrets: Sequence[str]) -> str:
    """Remove secret values from a log or error string."""
    redacted = text
    for secret in secrets:
        if not secret:
            continue
        redacted = redacted.replace(secret, "[REDACTED]")
        escaped = json.dumps(secret)[1:-1]
        if escaped and escaped != secret:
            redacted = redacted.replace(escaped, "[REDACTED]")
    return redacted


def redact_payload(value: object, secrets: Sequence[str]) -> object:
    """Redact secret strings inside JSON-ready error bodies."""
    if isinstance(value, str):
        return redact_secrets(value, secrets)
    if isinstance(value, dict):
        cleaned: dict[object, object] = {}
        for key, item in value.items():
            redacted_key = redact_secrets(key, secrets) if isinstance(key, str) else key
            cleaned[redacted_key] = redact_payload(item, secrets)
        return cleaned
    if isinstance(value, list):
        return [redact_payload(item, secrets) for item in value]
    return value


def _utc_timestamp(created: float) -> str:
    moment = datetime.fromtimestamp(created, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class StructuredFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, event, and safe fields only."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": _utc_timestamp(record.created),
            "level": record.levelname,
            "event": redact_secrets(record.getMessage(), self.secrets),
        }
        extra = getattr(record, "safe_extra", None)
        if isinstance(extra, Mapping):
            for key, value in _structured_fields(extra).items():
                payload[key] = _redact_logged_value(value, self.secrets)
        return redact_secrets(json.dumps(payload, separators=(",", ":")), self.secrets)


def configure_logging(level: str, *, secrets: Sequence[str] | None = None) -> None:
    numeric = getattr(logging, str(level).upper(), None)
    if not isinstance(numeric, int):
        numeric = logging.INFO
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(numeric)
    logger.propagate = False
    redacted = tuple(secret for secret in (secrets or ()) if secret)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredFormatter(redacted))
        logger.addHandler(handler)
        return
    for handler in logger.handlers:
        formatter = handler.formatter
        if isinstance(formatter, StructuredFormatter):
            formatter.secrets = redacted


def _structured_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """Scalar allowlist, plus rejection locations and a development-only model answer."""

    safe = safe_log_extra(fields)
    if "validation_failures" in fields:
        safe["validation_failures"] = _public_failures(fields.get("validation_failures"))
    answer = fields.get("model_answer")
    if isinstance(answer, str):
        safe["model_answer"] = answer
    return safe


def _redact_logged_value(value: object, secrets: Sequence[str]) -> object:
    if isinstance(value, str):
        return redact_secrets(value, secrets)
    if isinstance(value, list):
        return [_redact_logged_value(item, secrets) for item in value]
    if isinstance(value, dict):
        redacted: dict[object, object] = {}
        for key, item in value.items():
            redacted_key = redact_secrets(key, secrets) if isinstance(key, str) else key
            redacted[redacted_key] = _redact_logged_value(item, secrets)
        return redacted
    return value


def image_log_markers(chunks: Sequence[bytes]) -> list[str]:
    """UTF-8 text and base64 for image bytes. Callers pass these so logs can drop them."""

    import base64

    markers: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        encoded = base64.b64encode(chunk).decode("ascii")
        if encoded:
            markers.append(encoded)
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text:
            markers.append(text)
    return markers


def vision_log_environment(explicit: str | None) -> str:
    """Use the caller's environment, or the process `LOANLENS_ENV`."""

    if explicit is not None:
        return explicit
    from app.core.config import get_settings

    return get_settings().loanlens_env


def vision_log_secrets() -> tuple[str, ...]:
    """API key for redaction only. The value is not a log field."""

    from app.core.config import get_settings

    key = get_settings().vision_api_key.strip()
    return (key,) if key else ()


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Log an event. Forbidden and non-allowlisted fields are dropped before the record is built."""
    logging.getLogger(LOGGER_NAME).log(level, event, extra={"safe_extra": safe_log_extra(fields)})
