"""Prefixed ULID identifiers (SPEC §14.2)."""

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

BOOK_PREFIX = "bok_"
ANALYSIS_PREFIX = "anl_"
IMAGE_PREFIX = "img_"
LOAN_PREFIX = "lon_"
LENDER_GROUP_PREFIX = "ldg_"


def _encode_crockford(value: int, bit_length: int) -> str:
    if value < 0 or value >= (1 << bit_length):
        raise ValueError("ULID component out of range")
    # 48-bit timestamps occupy 10 Crockford characters (50 bits, top bits zero).
    pad = (5 - (bit_length % 5)) % 5
    width = bit_length + pad
    chars: list[str] = []
    for shift in range(width - 5, -1, -5):
        chars.append(_CROCKFORD[(value >> shift) & 31])
    return "".join(chars)


def format_ulid(timestamp_ms: int, randomness: int) -> str:
    """Encode a 48-bit timestamp and 80-bit randomness as a 26-character ULID."""
    return _encode_crockford(timestamp_ms, 48) + _encode_crockford(randomness, 80)


def new_ulid() -> str:
    timestamp_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return format_ulid(timestamp_ms, randomness)


def new_book_id() -> str:
    return BOOK_PREFIX + new_ulid()


def new_analysis_id() -> str:
    return ANALYSIS_PREFIX + new_ulid()


def new_image_id() -> str:
    return IMAGE_PREFIX + new_ulid()


def new_loan_id() -> str:
    return LOAN_PREFIX + new_ulid()


def new_lender_group_id() -> str:
    return LENDER_GROUP_PREFIX + new_ulid()
