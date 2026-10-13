"""Lender identity, page grouping, and field merge (SPEC §10).

The grouping key is a canonical brand. Display keeps the original extracted
string. Aliases apply only after trim, casefold, and whitespace collapse.
Pages become loans, then lender groups. A conflicting amount is not replaced
with a third value.
"""

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.ids import new_lender_group_id, new_loan_id
from app.schemas.extraction import ImageExtractResult

UNKNOWN_LENDER_KEY = "_unknown_lender"
_ALIAS_FILE = Path(__file__).with_name("lender_aliases.json")
_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
_LENDER_FIELDS = ("lender_brand", "legal_lending_entity")
_MONEY_FIELDS = (
    "sanctioned_amount",
    "disbursed_amount",
    "processing_fee",
    "gst_amount",
    "other_fees_total",
    "repayment_amount",
)
_DATE_FIELDS = ("sanction_date", "disbursement_date", "repayment_date")
_MERGED_FIELDS = (
    "lender_brand",
    "legal_lending_entity",
    "borrower_name",
    "loan_id",
    *_MONEY_FIELDS,
    "interest_rate",
    "apr",
    "tenure",
    *_DATE_FIELDS,
    "repayment_frequency",
)
FIELD_CONFLICT = "FIELD_CONFLICT"
GROUPING_CONFLICT = "GROUPING_CONFLICT"


@dataclass(frozen=True)
class LenderIdentity:
    """Canonical group key plus the extracted label used for display."""

    grouping_key: str
    display_name: str | None


def normalize_lender_text(value: str | None) -> str | None:
    """Trim, casefold, and collapse whitespace. Blank text becomes null."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("lender text must be a string or null")
    collapsed = " ".join(value.split())
    if collapsed == "":
        return None
    return collapsed.casefold()


def load_lender_aliases(path: Path | None = None) -> dict[str, str]:
    """Load spelling → canonical brand. Keys and values are normalized.

    The file is a JSON list. Each object has ``spelling`` and ``canonical``.
    V1 ships an empty list.
    """

    alias_path = _ALIAS_FILE if path is None else path
    raw = json.loads(alias_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("lender aliases must be a list")

    mapping: dict[str, str] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"spelling", "canonical"}:
            raise ValueError(
                f"lender alias {index} must be an object with spelling and canonical"
            )
        spelling = normalize_lender_text(_required_alias_text(item["spelling"], index))
        canonical = normalize_lender_text(
            _required_alias_text(item["canonical"], index)
        )
        if spelling is None or canonical is None:
            raise ValueError(f"lender alias {index} must be non-blank")
        if spelling in mapping:
            raise ValueError(f"duplicate lender alias {spelling!r}")
        mapping[spelling] = canonical
    return mapping


def lender_display_name(
    lender_brand: str | None,
    legal_lending_entity: str | None,
) -> str | None:
    """Original extracted brand, or the legal entity when the brand is blank."""

    brand = _original_if_present(lender_brand)
    if brand is not None:
        return brand
    return _original_if_present(legal_lending_entity)


def lender_grouping_key(
    lender_brand: str | None,
    legal_lending_entity: str | None,
    aliases: Mapping[str, str] | None = None,
) -> str:
    """Canonical lender group key. ``aliases=None`` reads the alias file."""

    brand = normalize_lender_text(lender_brand)
    entity = normalize_lender_text(legal_lending_entity)
    chosen = brand if brand is not None else entity
    if chosen is None:
        return UNKNOWN_LENDER_KEY
    return _alias_table(aliases).get(chosen, chosen)


def lender_identity(
    lender_brand: str | None,
    legal_lending_entity: str | None,
    aliases: Mapping[str, str] | None = None,
) -> LenderIdentity:
    return LenderIdentity(
        grouping_key=lender_grouping_key(
            lender_brand,
            legal_lending_entity,
            aliases=aliases,
        ),
        display_name=lender_display_name(lender_brand, legal_lending_entity),
    )


def lender_group_sort_key(canonical_brand: str) -> tuple[int, str]:
    """Alphabetical by canonical brand, with ``_unknown_lender`` last."""

    if canonical_brand == UNKNOWN_LENDER_KEY:
        return (1, "")
    return (0, canonical_brand)


def _original_if_present(value: str | None) -> str | None:
    if normalize_lender_text(value) is None:
        return None
    return value


def _required_alias_text(value: object, index: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"lender alias {index} spelling and canonical must be strings")
    return value


def _alias_table(aliases: Mapping[str, str] | None) -> dict[str, str]:
    source = load_lender_aliases() if aliases is None else aliases
    table: dict[str, str] = {}
    for spelling, canonical in source.items():
        if not isinstance(spelling, str) or not isinstance(canonical, str):
            raise ValueError("lender alias spelling and canonical must be strings")
        key = normalize_lender_text(spelling)
        value = normalize_lender_text(canonical)
        if key is None or value is None:
            raise ValueError("lender alias spelling and canonical must be non-blank")
        if key in table:
            raise ValueError(f"duplicate lender alias {key!r}")
        table[key] = value
    return table


@dataclass
class _Fragment:
    image_id: str
    index: int
    grouping_key: str
    loan: dict[str, Any]


def group_raw_extracts(
    raw_extracts: list[dict[str, Any]],
    *,
    aliases: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Group extracted pages into lender groups in draft GET shape.

    Exact ``loan_id`` within a lender merges those pages. Otherwise pages
    merge only when the non-null identity fields agree. ``same_loan_as_image_ids``
    is a hint and is kept only when the pages do not conflict.
    """

    fragments = _parse_fragments(raw_extracts, aliases)
    return _groups_from_records(_auto_records(fragments), aliases)


def apply_image_assignment(
    raw_extracts: list[dict[str, Any]],
    assignment: list[dict[str, Any]],
    *,
    aliases: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Rebuild loans from a user image-to-loan assignment and re-merge fields.

    Images named in ``assignment`` are merged as given, including when amounts
    disagree. Disagreement clears the field. It does not invent a replacement
    amount. Images left out are grouped on their own by the automatic rules.
    """

    fragments = _parse_fragments(raw_extracts, aliases)
    by_image: dict[str, list[_Fragment]] = {}
    for fragment in fragments:
        by_image.setdefault(fragment.image_id, []).append(fragment)

    seen: set[str] = set()
    records: list[tuple[list[_Fragment], dict[str, Any]]] = []
    for loan_id, image_ids in _parse_assignment(assignment):
        chosen: list[_Fragment] = []
        for image_id in image_ids:
            seen.add(image_id)
            chosen.extend(by_image.get(image_id, []))
        if chosen:
            records.append((chosen, _loan_from_fragments(chosen, loan_id, set())))

    remainder = [fragment for fragment in fragments if fragment.image_id not in seen]
    records.extend(_auto_records(remainder))
    return _groups_from_records(records, aliases)


def _parse_fragments(
    raw_extracts: list[dict[str, Any]],
    aliases: Mapping[str, str] | None,
) -> list[_Fragment]:
    if not isinstance(raw_extracts, list):
        raise TypeError("extracts must be a list")
    fragments: list[_Fragment] = []
    for raw in raw_extracts:
        parsed = ImageExtractResult.model_validate(raw).model_dump(mode="json")
        for loan in parsed["loans"]:
            identity = lender_identity(
                loan["lender_brand"]["value"],
                loan["legal_lending_entity"]["value"],
                aliases=aliases,
            )
            fragments.append(
                _Fragment(
                    image_id=parsed["image_id"],
                    index=len(fragments),
                    grouping_key=identity.grouping_key,
                    loan=loan,
                )
            )
    return fragments


def _parse_assignment(assignment: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    if not isinstance(assignment, list):
        raise TypeError("assignment must be a list")
    order: list[str] = []
    images_by_loan: dict[str, list[str]] = {}
    seen_images: set[str] = set()
    for item in assignment:
        if not isinstance(item, dict):
            raise TypeError("assignment loan must be an object")
        loan_id = item.get("loan_internal_id")
        image_ids = item.get("image_ids")
        if not isinstance(loan_id, str) or loan_id == "":
            raise ValueError("loan_internal_id is required")
        if not isinstance(image_ids, list) or any(not isinstance(image_id, str) for image_id in image_ids):
            raise ValueError("image_ids must be a list of strings")
        if loan_id not in images_by_loan:
            order.append(loan_id)
            images_by_loan[loan_id] = []
        for image_id in image_ids:
            if image_id in seen_images:
                raise ValueError("image assigned more than once")
            seen_images.add(image_id)
            images_by_loan[loan_id].append(image_id)
    return [(loan_id, images_by_loan[loan_id]) for loan_id in order]


def _auto_records(
    fragments: list[_Fragment],
) -> list[tuple[list[_Fragment], dict[str, Any]]]:
    buckets: dict[str, list[_Fragment]] = {}
    for fragment in fragments:
        buckets.setdefault(fragment.grouping_key, []).append(fragment)
    records: list[tuple[list[_Fragment], dict[str, Any]]] = []
    for bucket in buckets.values():
        clusters, suspected = _cluster(bucket)
        for cluster in clusters:
            records.append((cluster, _loan_from_fragments(cluster, new_loan_id(), suspected)))
    return records


def _cluster(fragments: list[_Fragment]) -> tuple[list[list[_Fragment]], set[int]]:
    """Union pages by loan id, then by matching fields, then by a hint."""

    count = len(fragments)
    parent = list(range(count))

    def find(position: int) -> int:
        while parent[position] != position:
            parent[position] = parent[parent[position]]
            position = parent[position]
        return position

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    def members(position: int) -> list[int]:
        root = find(position)
        return [index for index in range(count) if find(index) == root]

    by_loan_id: dict[str, list[int]] = {}
    for position, fragment in enumerate(fragments):
        loan_id = _exact_loan_id(fragment.loan)
        if loan_id is None:
            continue
        by_loan_id.setdefault(loan_id, []).append(position)
    for positions in by_loan_id.values():
        for other in positions[1:]:
            union(positions[0], other)

    suspected: set[int] = set()
    for left in range(count):
        for right in range(left + 1, count):
            hinted = _hint_links(fragments[left], fragments[right])
            similar = _else_signals(fragments[left].loan, fragments[right].loan)
            if not hinted and not similar:
                continue
            if find(left) == find(right):
                continue
            if _components_conflict(fragments, members(left), members(right)):
                suspected.add(fragments[left].index)
                suspected.add(fragments[right].index)
                continue
            union(left, right)

    grouped: dict[int, list[_Fragment]] = {}
    for position, fragment in enumerate(fragments):
        grouped.setdefault(find(position), []).append(fragment)
    return [grouped[key] for key in sorted(grouped)], suspected


def _hint_links(left: _Fragment, right: _Fragment) -> bool:
    if left.image_id == right.image_id:
        return False
    return (
        right.image_id in left.loan["same_loan_as_image_ids"]
        or left.image_id in right.loan["same_loan_as_image_ids"]
    )


def _else_signals(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Same sanctioned amount and one shared date, among non-null fields."""

    if not _both_present_and_equal(left, right, "sanctioned_amount"):
        return False
    return _both_present_and_equal(left, right, "disbursement_date") or _both_present_and_equal(
        left, right, "repayment_date"
    )


def _components_conflict(
    fragments: list[_Fragment],
    left_members: list[int],
    right_members: list[int],
) -> bool:
    for left in left_members:
        for right in right_members:
            if _identity_conflict(fragments[left].loan, fragments[right].loan):
                return True
    return False


def _identity_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_id = _exact_loan_id(left)
    right_id = _exact_loan_id(right)
    if left_id is not None and right_id is not None and left_id != right_id:
        return True
    for name in _MONEY_FIELDS:
        if _money_conflict(left, right, name):
            return True
    for name in _DATE_FIELDS:
        if _date_conflict(left, right, name):
            return True
    return False


def _money_conflict(left: dict[str, Any], right: dict[str, Any], name: str) -> bool:
    left_value = _present_value(left[name])
    right_value = _present_value(right[name])
    if left_value is None or right_value is None:
        return False
    if left_value != right_value:
        return True
    left_currency = left[name].get("currency")
    right_currency = right[name].get("currency")
    return (
        isinstance(left_currency, str)
        and isinstance(right_currency, str)
        and left_currency != right_currency
    )


def _date_conflict(left: dict[str, Any], right: dict[str, Any], name: str) -> bool:
    left_value = _present_value(left[name])
    right_value = _present_value(right[name])
    return left_value is not None and right_value is not None and left_value != right_value


def _both_present_and_equal(left: dict[str, Any], right: dict[str, Any], name: str) -> bool:
    left_value = _present_value(left[name])
    right_value = _present_value(right[name])
    return left_value is not None and left_value == right_value


def _exact_loan_id(loan: dict[str, Any]) -> str | None:
    value = _present_value(loan["loan_id"])
    if isinstance(value, str):
        return value
    return None


def _loan_from_fragments(
    fragments: list[_Fragment],
    loan_internal_id: str,
    suspected: set[int],
) -> dict[str, Any]:
    fields, field_conflict = _merge_loan_fields([fragment.loan for fragment in fragments])
    flags: list[str] = []
    if field_conflict:
        flags.append(FIELD_CONFLICT)
    if any(fragment.index in suspected for fragment in fragments):
        flags.append(GROUPING_CONFLICT)
    image_ids: list[str] = []
    for fragment in fragments:
        if fragment.image_id not in image_ids:
            image_ids.append(fragment.image_id)
    return {
        "loan_internal_id": loan_internal_id,
        "external_loan_id": fields["loan_id"]["value"],
        "image_ids": image_ids,
        "fields": fields,
        "review_flags": flags,
    }


def _merge_loan_fields(loans: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    merged = copy.deepcopy(loans[0])
    merged.pop("same_loan_as_image_ids", None)
    conflict = False
    for name in _MERGED_FIELDS:
        value, flagged = _merge_fields([loan[name] for loan in loans], field_name=name)
        merged[name] = value
        conflict = conflict or flagged
    penalties, penalty_conflict = _merge_penalties([loan.get("penalties") or [] for loan in loans])
    merged["penalties"] = penalties
    return merged, conflict or penalty_conflict


def _merge_fields(fields: list[dict[str, Any]], *, field_name: str) -> tuple[dict[str, Any], bool]:
    present = [field for field in fields if _is_present(field)]
    if not present:
        return _cleared(fields[0]), False
    top_rank = max(_rank(field) for field in present)
    top = [field for field in present if _rank(field) == top_rank]
    if any(not _same_field_value(top[0], other, field_name) for other in top[1:]):
        return _cleared(fields[0]), True
    winner = copy.deepcopy(top[0])
    for companion in ("currency", "unit"):
        if companion not in winner or winner.get(companion) is not None:
            continue
        choices: list[Any] = []
        for field in present:
            if not _same_field_value(winner, field, field_name):
                continue
            companion_value = field.get(companion)
            if companion_value is not None and companion_value not in choices:
                choices.append(companion_value)
        if len(choices) == 1:
            winner[companion] = choices[0]
    return winner, False


def _merge_penalties(lists: list[list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], bool]:
    conflict = False
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for penalties in lists:
        for penalty in penalties:
            name = penalty.get("name")
            if not isinstance(name, str) or name.strip() == "":
                continue
            key = " ".join(name.split()).casefold()
            if key not in grouped:
                order.append(key)
                grouped[key] = []
            grouped[key].append(penalty)

    merged: list[dict[str, Any]] = []
    for key in order:
        items = grouped[key]
        amounts = [item["amount"] for item in items if isinstance(item.get("amount"), dict)]
        rates = [item["rate"] for item in items if isinstance(item.get("rate"), dict)]
        amount = None
        rate = None
        if amounts:
            amount, amount_conflict = _merge_fields(amounts, field_name="amount")
            conflict = conflict or amount_conflict
        if rates:
            rate, rate_conflict = _merge_fields(rates, field_name="rate")
            conflict = conflict or rate_conflict
        descriptions = [
            item["description"]
            for item in items
            if isinstance(item.get("description"), str) and item["description"].strip() != ""
        ]
        unique_descriptions = list(dict.fromkeys(descriptions))
        description = unique_descriptions[0] if len(unique_descriptions) == 1 else None
        if len(unique_descriptions) > 1:
            conflict = True
        merged.append(
            {
                "name": items[0]["name"],
                "amount": amount,
                "rate": rate,
                "description": description,
            }
        )
    return merged, conflict


def _cleared(template: dict[str, Any]) -> dict[str, Any]:
    cleared = {
        "value": None,
        "source": "unknown",
        "confidence": "unknown",
        "evidence": None,
    }
    if "currency" in template:
        cleared = {
            "value": None,
            "currency": None,
            "source": "unknown",
            "confidence": "unknown",
            "evidence": None,
        }
    elif "unit" in template:
        cleared = {
            "value": None,
            "unit": None,
            "source": "unknown",
            "confidence": "unknown",
            "evidence": None,
        }
    return cleared


def _same_field_value(left: dict[str, Any], right: dict[str, Any], field_name: str) -> bool:
    if field_name in _LENDER_FIELDS:
        left_key = normalize_lender_text(left.get("value") if isinstance(left.get("value"), str) else None)
        right_key = normalize_lender_text(right.get("value") if isinstance(right.get("value"), str) else None)
        if left_key is None or left_key != right_key:
            return False
    elif left.get("value") != right.get("value"):
        return False
    for companion in ("currency", "unit"):
        if companion not in left and companion not in right:
            continue
        left_companion = left.get(companion)
        right_companion = right.get(companion)
        if (
            left_companion is not None
            and right_companion is not None
            and left_companion != right_companion
        ):
            return False
    return True


def _groups_from_records(
    records: list[tuple[list[_Fragment], dict[str, Any]]],
    aliases: Mapping[str, str] | None,
) -> list[dict[str, Any]]:
    buckets: dict[str, list[tuple[list[_Fragment], dict[str, Any]]]] = {}
    for fragments, loan in records:
        buckets.setdefault(_loan_group_key(fragments, loan, aliases), []).append((fragments, loan))

    groups: list[dict[str, Any]] = []
    for key in sorted(buckets, key=lender_group_sort_key):
        items = sorted(buckets[key], key=lambda item: _loan_sort_key(item[1]))
        label_fragments = [
            fragment
            for fragments, _loan in items
            for fragment in fragments
            if fragment.grouping_key == key
        ]
        brand, entity = _group_labels(key, label_fragments)
        groups.append(
            {
                "lender_group_id": new_lender_group_id(),
                "lender_brand": brand,
                "legal_lending_entity": entity,
                "loans": [loan for _fragments, loan in items],
            }
        )
    return groups


def _loan_group_key(
    fragments: list[_Fragment],
    loan: dict[str, Any],
    aliases: Mapping[str, str] | None,
) -> str:
    keys = {fragment.grouping_key for fragment in fragments}
    if len(keys) == 1:
        return next(iter(keys))
    fields = loan["fields"]
    return lender_grouping_key(
        fields["lender_brand"]["value"],
        fields["legal_lending_entity"]["value"],
        aliases=aliases,
    )


def _group_labels(key: str, fragments: list[_Fragment]) -> tuple[str | None, str | None]:
    if key == UNKNOWN_LENDER_KEY or not fragments:
        return None, None
    return _best_original(fragments, "lender_brand"), _best_original(fragments, "legal_lending_entity")


def _best_original(fragments: list[_Fragment], field_name: str) -> str | None:
    best_value: str | None = None
    best_rank = -1
    best_index = 10**9
    for fragment in fragments:
        field = fragment.loan[field_name]
        if not _is_present(field) or not isinstance(field.get("value"), str):
            continue
        rank = _rank(field)
        if rank > best_rank or (rank == best_rank and fragment.index < best_index):
            best_value = field["value"]
            best_rank = rank
            best_index = fragment.index
    return best_value


def _loan_sort_key(loan: dict[str, Any]) -> tuple[tuple[int, str], tuple[int, str], tuple[int, str], tuple[int, str]]:
    fields = loan["fields"]

    def date_part(name: str) -> tuple[int, str]:
        value = fields[name]["value"]
        if not isinstance(value, str) or value == "":
            return (1, "")
        return (0, value)

    external = loan.get("external_loan_id")
    if not isinstance(external, str) or external == "":
        id_part = (1, "")
    else:
        id_part = (0, external)
    return (
        date_part("disbursement_date"),
        date_part("sanction_date"),
        date_part("repayment_date"),
        id_part,
    )


def _is_present(field: dict[str, Any]) -> bool:
    value = field.get("value")
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    return True


def _present_value(field: dict[str, Any]) -> Any:
    if not _is_present(field):
        return None
    return field.get("value")


def _rank(field: dict[str, Any]) -> int:
    confidence = field.get("confidence") or "unknown"
    if not isinstance(confidence, str):
        return 0
    return _CONFIDENCE_RANK.get(confidence, 0)
