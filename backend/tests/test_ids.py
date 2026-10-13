import re

from app.core.ids import (
    format_ulid,
    new_analysis_id,
    new_book_id,
    new_image_id,
    new_lender_group_id,
    new_loan_id,
)

_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_FACTORIES = (
    (new_book_id, "bok_"),
    (new_analysis_id, "anl_"),
    (new_image_id, "img_"),
    (new_loan_id, "lon_"),
    (new_lender_group_id, "ldg_"),
)


def test_ids_use_spec_prefixes_and_ulid() -> None:
    seen: set[str] = set()
    for factory, prefix in _FACTORIES:
        value = factory()
        assert value.startswith(prefix)
        assert _ULID.fullmatch(value.removeprefix(prefix))
        seen.add(value)
    assert len(seen) == len(_FACTORIES)


def test_ids_are_unique() -> None:
    values = [new_book_id() for _ in range(50)]
    assert len(set(values)) == 50


def test_ulid_known_timestamp() -> None:
    # Published Crockford encoding of 1469918176385 ms is 01ARYZ6S41.
    assert format_ulid(1469918176385, 0) == "01ARYZ6S41" + ("0" * 16)
