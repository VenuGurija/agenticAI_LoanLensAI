"""OpenAI-compatible Chat Completions vision adapter (SPEC §6.3, §17, §22).

One provider, one endpoint. Other vendors are not implemented.
`analysis_id` is never sent. The API key is not placed in exception messages.
A rejected answer is sent back with feedback, up to five retries.
HTTP 5xx and timeouts are not retried.
"""

import base64
import copy
import json
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import (
    append_vision_log,
    image_log_markers,
    log_vision_response_rejected,
    model_answer_text,
    validation_failure_items,
)
from app.schemas.extraction import ImageExtractResult
from app.services.vision.feedback import MAX_VISION_FEEDBACK_RETRIES, vision_feedback_text
from app.schemas.vision import VisionImage, VisionRequest, VisionResponse, VisionUsage
from app.services.extraction import normalize_printed_extract
from app.services.vision.base import VisionResponseInvalidError
from app.services.vision.contracts import EXTRACT_SCHEMA_NAME, json_schema_for, validate_contract
from app.services.vision.prompts import prompt_for

DEFAULT_BASE_URL = "https://api.openai.com/v1"
PROVIDER_NAME = "openai_compatible"
_MAX_EXTRA_STRIPS = 64
# Shape only. Nulls are not sample loans.
_EMPTY_EXAMPLE = {
    "classify": '{"results":[{"image_id":null,"document_class":null}]}',
    "extract": (
        '{"results":[{"image_id":null,"loans":'
        '[{"sanctioned_amount":{"value":null,"currency":null}}]}]}'
    ),
}

_SHORT_TASK = {
    "classify": "Classify each image as loan_document, not_loan_document, or unclear.",
    "extract": (
        "Extract only loan fields that are clearly visible. "
        "Leave missing fields null. Do not compute fees, GST, APR, or remaining principal."
    ),
}
_EXTRACT_FIELDS = (
    "lender_brand, legal_lending_entity, borrower_name, loan_id, "
    "sanctioned_amount, disbursed_amount, processing_fee, gst_amount, "
    "other_fees_total, interest_rate, apr, tenure, sanction_date, "
    "disbursement_date, repayment_amount, repayment_date, "
    "repayment_frequency, penalties"
)
_EXTRACT_SHAPE = (
    "Each field is an object. Do not return a bare string, a bare number, or null for the field itself. "
    "Money value is a string of the printed digits, never a JSON number. "
    "interest_rate and apr use unit percent and a number string, with no percent sign and no extra words. "
    "tenure value is an integer and unit is days or months. "
    "Dates use YYYY-MM-DD. "
    "repayment_frequency value is one_time, weekly, biweekly, monthly, unknown, or null. "
    "penalties is an array. Use [] when none are printed."
)
_EXTRACT_LABELS = (
    "The page may use different words. Fit the printed line to the field with the same meaning. "
    "Do not calculate a number that is not printed. "
    "sanctioned_amount is the loan amount, amount sanctioned, or facility amount. "
    "disbursed_amount is the net amount received, net disbursed, or amount ((i)-(iii)). "
    "Do not put that net amount in sanctioned_amount. "
    "repayment_amount is the total amount to be paid by the borrower, amount payable, or total repayment. "
    "apr is only a line labeled Annual Percentage Rate or APR. Do not copy that number into interest_rate. "
    "interest_rate is a printed interest rate that is not labeled APR. "
    "tenure is tenor, tenure, or loan period. Use days when the label says days, and months when it says months. "
    "repayment_date is the repayment date or due date. "
    "repayment_frequency Bullet, single instalment, or one instalment is one_time. "
    "Number of instalments is not its own field. "
    "A label in Rupees means currency INR."
)


class VisionUnavailableError(Exception):
    """Upstream 5xx or a transport failure. API code `VISION_UNAVAILABLE`."""

    code = "VISION_UNAVAILABLE"

    def __init__(self, message: str = "The vision service is unavailable.") -> None:
        super().__init__(message)


class VisionTimeoutError(Exception):
    """The vision call exceeded the configured timeout, or the upstream returned 408/504."""

    code = "VISION_TIMEOUT"

    def __init__(self, message: str = "The vision service timed out.") -> None:
        super().__init__(message)


class InvalidVisionResponse(VisionResponseInvalidError):
    """Non-JSON or schema-invalid model output after the one allowed retry."""

    code = "VISION_INVALID_RESPONSE"

    def __init__(self, failures: list[dict[str, str]] | None = None) -> None:
        super().__init__(
            "The vision service returned an invalid response.",
            failures=failures,
        )


class OpenAICompatibleVisionProvider:
    """Chat Completions with image inputs. Tests inject an httpx transport; CI does not call a live API."""

    provider_name = PROVIDER_NAME

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = settings.vision_api_key.strip()
        self._model = settings.vision_model.strip()
        self._environment = settings.loanlens_env
        base = settings.vision_base_url.strip() or DEFAULT_BASE_URL
        self._base_url = base.rstrip("/")
        self._timeout_seconds = float(settings.loanlens_vision_timeout_seconds)
        self._transport = transport

    def __repr__(self) -> str:
        return (
            "OpenAICompatibleVisionProvider("
            f"base_url={self._base_url!r}, model={self._model!r})"
        )

    async def analyze_images(self, request: VisionRequest) -> VisionResponse:
        self._ensure_ready(request)
        feedback: str | None = None
        last_failures: list[dict[str, str]] = []
        image_markers = image_log_markers(tuple(image.bytes for image in request.images))
        for attempt in range(MAX_VISION_FEEDBACK_RETRIES + 1):
            payload = await self._post(request, feedback=feedback)
            parsed = _interpret(request, payload, self._model)
            if not isinstance(parsed, _RejectedAnswer):
                raw_json, model_name, usage = parsed
                return VisionResponse(
                    raw_json=raw_json,
                    model_name=model_name,
                    provider_name=self.provider_name,
                    usage=usage,
                )
            log_vision_response_rejected(
                analysis_id=request.analysis_id,
                task=request.task,
                attempt=attempt + 1,
                schema_name=request.response_schema_name,
                failures=parsed.failures,
                model_answer=parsed.model_answer,
                environment=self._environment,
                secrets=(self._api_key,),
                image_markers=image_markers,
            )
            last_failures = list(parsed.failures)
            if attempt == MAX_VISION_FEEDBACK_RETRIES:
                break
            feedback = vision_feedback_text(
                parsed.failures,
                previous_answer=parsed.model_answer,
                json_object=parsed.json_object,
                schema_echo=parsed.schema_echo,
            )
            append_vision_log(
                "vision_feedback_sent",
                {
                    "analysis_id": request.analysis_id,
                    "task": request.task,
                    "attempt": attempt + 1,
                    "schema_name": request.response_schema_name,
                    "validation_failures": parsed.failures,
                },
            )
        raise InvalidVisionResponse(last_failures)

    def _ensure_ready(self, request: VisionRequest) -> None:
        if not self._api_key or not self._model:
            raise VisionUnavailableError("The vision service is not configured.")
        known = True
        try:
            json_schema_for(request.response_schema_name)
        except ValueError:
            known = False
        if not known:
            raise InvalidVisionResponse()

    async def _post(self, request: VisionRequest, *, feedback: str | None) -> object | None:
        failure: str | None = None
        response: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self._timeout_seconds),
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=_body(request, self._model, feedback=feedback),
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.TimeoutException:
            failure = "timeout"
        except httpx.HTTPError:
            failure = "unavailable"
        if failure == "timeout":
            raise VisionTimeoutError()
        if failure is not None or response is None:
            raise VisionUnavailableError()
        if response.status_code in (408, 504):
            raise VisionTimeoutError()
        if response.status_code != 200:
            raise VisionUnavailableError()
        try:
            return response.json()
        except ValueError:
            return response.text


def _body(request: VisionRequest, model: str, *, feedback: str | None) -> dict[str, Any]:
    """Vendor payload. `analysis_id` is omitted on purpose (SPEC §6.2).

    The Pydantic schema stays in the app. Sending it makes a small model
    copy the schema back instead of reading the image.
    """

    image_ids = ", ".join(image.image_id for image in request.images)
    lines = [
        _SHORT_TASK[request.task],
        "Return one JSON object with a results array filled from the image.",
        "Do not return the schema.",
        f"Use these image_id values, in image order: {image_ids}",
        "Use the server image_id values already named in the prompt.",
    ]
    if request.task == "extract":
        lines.append(
            "Return exactly one result for each named image_id, and do not append an extra result."
        )
        lines.append(f"Fill each of these fields from the image: {_EXTRACT_FIELDS}.")
        lines.append("Use null only when that field is not clearly printed.")
        lines.append(
            "Do not compute fees, GST, APR, or remaining principal unless that figure is printed."
        )
        lines.append(_EXTRACT_LABELS)
        lines.append(_EXTRACT_SHAPE)
    lines.extend(
        [
            "Example shape only, with nulls. Not sample loan data.",
            f"Example: {_EMPTY_EXAMPLE[request.task]}",
        ]
    )
    if request.hint and request.hint.strip():
        lines.append(request.hint.strip())
    if feedback and feedback.strip():
        lines.append(feedback.strip())
    content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}]
    content.extend(_image_part(image) for image in request.images)
    return {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt_for(request.task)},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
    }


def _image_part(image: VisionImage) -> dict[str, Any]:
    encoded = base64.b64encode(image.bytes).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{image.media_type};base64,{encoded}",
            "detail": "high",
        },
    }


class _RejectedAnswer:
    """A provider payload that cannot be returned as a vision response."""

    def __init__(
        self,
        failures: list[dict[str, str]],
        model_answer: str | None,
        *,
        json_object: bool,
        schema_echo: bool = False,
    ) -> None:
        self.failures = failures
        self.model_answer = model_answer
        self.json_object = json_object
        self.schema_echo = schema_echo
        self.retryable = (not json_object) or schema_echo


def _interpret(
    request: VisionRequest,
    payload: object | None,
    fallback_model: str,
) -> tuple[dict[str, Any], str, VisionUsage | None] | _RejectedAnswer:
    if isinstance(payload, str):
        return _RejectedAnswer(
            [{"loc": "", "type": "json_invalid"}],
            payload,
            json_object=False,
        )
    if not isinstance(payload, dict):
        return _RejectedAnswer(
            [{"loc": "", "type": "dict_type"}],
            model_answer_text(payload),
            json_object=False,
        )
    content = _message_content(payload)
    if content is None:
        return _RejectedAnswer(
            [{"loc": "choices.0.message.content", "type": "missing"}],
            None,
            json_object=False,
        )
    answer = _answer_from_content(content)
    data = _decode_json_object(content)
    if data is None:
        return _RejectedAnswer(
            [{"loc": "message.content", "type": "json_invalid"}],
            answer,
            json_object=False,
        )
    if _is_schema_echo(data):
        return _RejectedAnswer(
            [{"loc": "results", "type": "missing"}],
            answer,
            json_object=True,
            schema_echo=True,
        )
    raw, failures = _validate_raw(request.response_schema_name, data)
    if raw is None:
        return _RejectedAnswer(failures, answer, json_object=True)
    if request.response_schema_name == EXTRACT_SCHEMA_NAME:
        strict = _strict_extract_failures(raw)
        if strict:
            return _RejectedAnswer(strict, answer, json_object=True)
    reported = payload.get("model")
    model_name = reported.strip() if isinstance(reported, str) and reported.strip() else fallback_model
    return raw, model_name, _usage(payload.get("usage"))


def _is_schema_echo(data: dict[str, Any]) -> bool:
    """A copied JSON Schema is not a loan. A results payload is not an echo."""

    if "results" in data:
        return False
    return "$defs" in data or "properties" in data


def _message_content(payload: dict[str, Any]) -> object | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict) or "content" not in message:
        return None
    return message.get("content")


def _answer_from_content(content: object) -> str | None:
    """Assistant text only. Image parts in the message are not a model answer."""

    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        if texts:
            return "".join(texts)
        return None
    return model_answer_text(content)


def _decode_json_object(content: object) -> dict[str, Any] | None:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        if not texts:
            return None
        content = "".join(texts)
    if not isinstance(content, str):
        return None
    try:
        loaded = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _strict_extract_failures(raw: dict[str, Any]) -> list[dict[str, str]]:
    """Money and date rules that the vision contract still allows as plain text."""

    results = raw.get("results")
    if not isinstance(results, list):
        return []
    failures: list[dict[str, str]] = []
    for index, item in enumerate(results):
        try:
            ImageExtractResult.model_validate(item)
        except ValidationError as exc:
            for failure in validation_failure_items(exc):
                loc = failure["loc"]
                prefix = f"results.{index}"
                failure["loc"] = f"{prefix}.{loc}" if loc else prefix
                failures.append(failure)
    return failures


def _validate_raw(
    schema_name: str,
    data: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Drop unknown keys (SPEC §17), then reject wrong types."""

    current = copy.deepcopy(data)
    if schema_name == EXTRACT_SCHEMA_NAME:
        current = normalize_printed_extract(current)
    failures = [{"loc": "", "type": "value_error"}]
    for _ in range(_MAX_EXTRA_STRIPS):
        removed = False
        try:
            model = validate_contract(schema_name, current)
        except ValidationError as exc:
            failures = validation_failure_items(exc)
            removed = _drop_extra_forbidden(current, exc)
        except ValueError:
            return None, [{"loc": "", "type": "unknown_schema"}]
        else:
            dumped = model.model_dump(mode="json")
            if not isinstance(dumped, dict):
                return None, [{"loc": "", "type": "dict_type"}]
            return dumped, []
        if not removed:
            return None, failures
    return None, failures


def _drop_extra_forbidden(data: object, exc: ValidationError) -> bool:
    removed = False
    for err in exc.errors():
        if err.get("type") != "extra_forbidden":
            continue
        loc = err.get("loc")
        if isinstance(loc, tuple) and _delete_path(data, loc):
            removed = True
    return removed


def _delete_path(root: object, loc: tuple[object, ...]) -> bool:
    if not loc:
        return False
    parent: object = root
    for step in loc[:-1]:
        if isinstance(step, int):
            if not isinstance(parent, list) or step < 0 or step >= len(parent):
                return False
            parent = parent[step]
            continue
        if not isinstance(parent, dict) or step not in parent:
            return False
        parent = parent[step]
    last = loc[-1]
    if isinstance(parent, dict) and isinstance(last, str) and last in parent:
        del parent[last]
        return True
    return False


def _usage(raw: object) -> VisionUsage | None:
    if not isinstance(raw, dict):
        return None
    input_tokens = raw.get("prompt_tokens", raw.get("input_tokens"))
    output_tokens = raw.get("completion_tokens", raw.get("output_tokens"))
    try:
        return VisionUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    except ValidationError:
        return None
