"""Lender identity: canonical keys, original display, alias after normalize."""

import json
from pathlib import Path

import pytest

from app.services.grouping.group import (
    UNKNOWN_LENDER_KEY,
    lender_display_name,
    lender_group_sort_key,
    lender_grouping_key,
    lender_identity,
    load_lender_aliases,
    normalize_lender_text,
)


def test_spaced_dhanrishi_groups_with_canonical_brand() -> None:
    spaced = lender_identity(" DhanRishi ", None, aliases={})
    plain = lender_identity("dhanrishi", None, aliases={})

    assert spaced.grouping_key == "dhanrishi"
    assert plain.grouping_key == "dhanrishi"
    assert spaced.grouping_key == plain.grouping_key
    assert spaced.display_name == " DhanRishi "
    assert plain.display_name == "dhanrishi"


def test_normalize_trims_casefolds_and_collapses_whitespace() -> None:
    assert normalize_lender_text("  Dhan   Rishi\n") == "dhan rishi"
    assert normalize_lender_text("DHANRISHI") == "dhanrishi"
    assert normalize_lender_text("   ") is None
    assert normalize_lender_text(None) is None


def test_empty_brand_and_entity_is_unknown_lender() -> None:
    for brand, entity in (
        (None, None),
        ("", ""),
        ("   ", None),
        (None, " \n\t "),
        ("  ", "\t"),
    ):
        identity = lender_identity(brand, entity, aliases={})
        assert identity.grouping_key == UNKNOWN_LENDER_KEY
        assert identity.display_name is None


def test_brand_wins_over_legal_entity() -> None:
    identity = lender_identity(" DhanRishi ", " Gagandeep ", aliases={})

    assert identity.grouping_key == "dhanrishi"
    assert identity.display_name == " DhanRishi "


def test_legal_entity_is_used_when_brand_is_blank() -> None:
    identity = lender_identity(None, " Gagandeep ", aliases={})

    assert identity.grouping_key == "gagandeep"
    assert identity.display_name == " Gagandeep "
    assert lender_grouping_key("   ", "Gagandeep", aliases={}) == "gagandeep"


def test_alias_is_applied_after_normalize() -> None:
    aliases = {"Dhan Rishi": "DhanRishi"}
    identity = lender_identity("  Dhan   Rishi ", "Other Entity", aliases=aliases)

    assert normalize_lender_text("  Dhan   Rishi ") == "dhan rishi"
    assert identity.grouping_key == "dhanrishi"
    assert identity.display_name == "  Dhan   Rishi "
    assert lender_display_name("  Dhan   Rishi ", "Other Entity") == "  Dhan   Rishi "


def test_alias_does_not_replace_a_present_brand_with_the_entity() -> None:
    aliases = {"gagandeep": "dhanrishi"}

    assert lender_grouping_key("Other", "Gagandeep", aliases=aliases) == "other"


def test_shipped_alias_file_is_empty() -> None:
    alias_path = (
        Path(__file__).parents[1]
        / "app"
        / "services"
        / "grouping"
        / "lender_aliases.json"
    )
    assert json.loads(alias_path.read_text(encoding="utf-8")) == []
    assert load_lender_aliases() == {}
    assert lender_grouping_key(" DhanRishi ", None) == "dhanrishi"


def test_alias_file_maps_normalized_spelling_to_canonical_brand(tmp_path: Path) -> None:
    path = tmp_path / "lender_aliases.json"
    path.write_text(
        json.dumps([{"spelling": " Dhan  Rishi ", "canonical": "DhanRishi"}]),
        encoding="utf-8",
    )

    aliases = load_lender_aliases(path)

    assert aliases == {"dhan rishi": "dhanrishi"}
    assert lender_grouping_key("dhan rishi", None, aliases=aliases) == "dhanrishi"


def test_alias_file_rejects_a_non_list(tmp_path: Path) -> None:
    path = tmp_path / "lender_aliases.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a list"):
        load_lender_aliases(path)


def test_unknown_lender_sorts_last() -> None:
    brands = ["zeta", UNKNOWN_LENDER_KEY, "alpha", "dhanrishi"]

    assert sorted(brands, key=lender_group_sort_key) == [
        "alpha",
        "dhanrishi",
        "zeta",
        UNKNOWN_LENDER_KEY,
    ]
