"""Feedback sent back to the vision model after a rejected answer.

A rejection is one failed attempt. The next call includes this text so the
model can revise that answer. Five retries is the maximum. Callers do not
invent a lender, amount, date, or identity.
"""

MAX_VISION_FEEDBACK_RETRIES = 5
JSON_ONLY_INSTRUCTION = "return JSON only"
SCHEMA_ECHO_INSTRUCTION = "Fill results from the image. Do not repeat the schema."
_PREVIOUS_ANSWER_LIMIT = 8000

_INTRO = (
    "The previous answer was rejected.",
    "Change that answer so the listed fields match the required shape.",
    "Do not invent a lender, amount, date, or identity.",
    "Money value is digits with an optional decimal point. "
    "Put the currency code in currency, not in the value.",
)


def vision_feedback_text(
    failures: list[dict[str, str]],
    *,
    previous_answer: str | None,
    json_object: bool,
    schema_echo: bool,
) -> str:
    """Instructions for the next vision call. A schema echo is not pasted back."""

    lines = list(_INTRO)
    if not json_object:
        lines.append(JSON_ONLY_INSTRUCTION)
    if schema_echo:
        lines.append(SCHEMA_ECHO_INSTRUCTION)
    for item in failures:
        loc = item.get("loc") or ""
        err_type = item.get("type") or "value_error"
        lines.append(f"{loc} ({err_type})" if loc else f"({err_type})")
    if previous_answer and not schema_echo:
        clipped = previous_answer[:_PREVIOUS_ANSWER_LIMIT]
        lines.append("Previous answer:")
        lines.append(clipped)
    return "\n".join(lines)
