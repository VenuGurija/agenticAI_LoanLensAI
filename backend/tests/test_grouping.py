"""Loan grouping and field merge (SPEC §10)."""

import json

from app.schemas.extraction import ImageExtractResult
from app.services.extraction import lender_groups_for_assignment, lender_groups_from_extracts
from app.services.grouping.group import (
    FIELD_CONFLICT,
    GROUPING_CONFLICT,
    apply_image_assignment,
    group_raw_extracts,
)


def _money(value: str | None, confidence: str = "high") -> dict:
    return {"value": value, "currency": None if value is None else "INR", "confidence": confidence}


def _text(value: str | None, confidence: str = "high") -> dict:
    return {"value": value, "confidence": confidence}


def _date(value: str | None, confidence: str = "high") -> dict:
    return {"value": value, "confidence": confidence}


def _page(image_id: str, **fields: object) -> dict:
    loan = {
        "lender_brand": _text("Dhanrishi"),
        "loan_id": _text("LN-1"),
    }
    loan.update(fields)
    return ImageExtractResult.model_validate({"image_id": image_id, "loans": [loan]}).model_dump(
        mode="json"
    )


def _loans(groups: list[dict]) -> list[dict]:
    return [loan for group in groups for loan in group["loans"]]


def test_two_pages_with_the_same_loan_id_merge() -> None:
    pages = [
        _page(
            "img_a",
            sanctioned_amount=_money("15000.00"),
            processing_fee=_money(None, "unknown"),
        ),
        _page(
            "img_b",
            lender_brand=_text("dhanrishi"),
            sanctioned_amount=_money(None, "unknown"),
            disbursed_amount=_money("13230.00"),
            processing_fee=_money("100.00"),
            penalties=[
                {
                    "name": "Late fee",
                    "amount": _money("50.00"),
                }
            ],
        ),
    ]

    groups = group_raw_extracts(pages)

    assert len(groups) == 1
    assert groups[0]["lender_brand"] == "Dhanrishi"
    assert groups[0]["lender_group_id"].startswith("ldg_")
    loans = groups[0]["loans"]
    assert len(loans) == 1
    loan = loans[0]
    assert loan["loan_internal_id"].startswith("lon_")
    assert loan["external_loan_id"] == "LN-1"
    assert loan["image_ids"] == ["img_a", "img_b"]
    assert loan["review_flags"] == []
    assert loan["fields"]["sanctioned_amount"]["value"] == "15000.00"
    assert loan["fields"]["sanctioned_amount"]["source"] == "extracted"
    assert loan["fields"]["disbursed_amount"]["value"] == "13230.00"
    assert loan["fields"]["processing_fee"]["value"] == "100.00"
    assert loan["fields"]["penalties"][0]["name"] == "Late fee"
    assert loan["fields"]["penalties"][0]["amount"]["value"] == "50.00"
    assert "same_loan_as_image_ids" not in loan["fields"]
    assert "calculated" not in loan
    assert "implied_fees_total" not in loan["fields"]


def test_conflicting_high_sanctioned_amounts_clear_instead_of_merging() -> None:
    pages = [
        _page("img_a", sanctioned_amount=_money("15000.00")),
        _page("img_b", sanctioned_amount=_money("18000.00")),
    ]

    groups = group_raw_extracts(pages)
    loan = _loans(groups)[0]
    serialized = json.dumps(groups)

    assert len(_loans(groups)) == 1
    assert loan["image_ids"] == ["img_a", "img_b"]
    assert loan["fields"]["sanctioned_amount"]["value"] is None
    assert loan["fields"]["sanctioned_amount"]["source"] == "unknown"
    assert loan["fields"]["sanctioned_amount"]["confidence"] == "unknown"
    assert loan["review_flags"] == [FIELD_CONFLICT]
    assert "15000.00" not in serialized
    assert "18000.00" not in serialized
    assert "16500.00" not in serialized
    assert "33000.00" not in serialized


def test_equal_confidence_disagreement_clears_the_field() -> None:
    pages = [
        _page("img_a", sanctioned_amount=_money("15000.00", "medium")),
        _page("img_b", sanctioned_amount=_money("18000.00", "medium")),
    ]

    loan = _loans(group_raw_extracts(pages))[0]

    assert loan["fields"]["sanctioned_amount"]["value"] is None
    assert loan["review_flags"] == [FIELD_CONFLICT]


def test_higher_confidence_wins_over_a_lower_one() -> None:
    pages = [
        _page("img_a", sanctioned_amount=_money("15000.00", "high")),
        _page("img_b", sanctioned_amount=_money("18000.00", "medium")),
    ]

    loan = _loans(group_raw_extracts(pages))[0]

    assert loan["fields"]["sanctioned_amount"]["value"] == "15000.00"
    assert loan["review_flags"] == []


def test_amount_conflict_does_not_merge_suspected_pages() -> None:
    pages = [
        _page(
            "img_a",
            loan_id=_text(None, "unknown"),
            sanctioned_amount=_money("15000.00"),
            disbursed_amount=_money("10000.00"),
            disbursement_date=_date("2026-06-17"),
        ),
        _page(
            "img_b",
            loan_id=_text(None, "unknown"),
            sanctioned_amount=_money("15000.00"),
            disbursed_amount=_money("12000.00"),
            disbursement_date=_date("2026-06-17"),
        ),
    ]

    loans = _loans(group_raw_extracts(pages))

    assert len(loans) == 2
    assert {loan["image_ids"][0] for loan in loans} == {"img_a", "img_b"}
    disbursed = {loan["fields"]["disbursed_amount"]["value"] for loan in loans}
    assert disbursed == {"10000.00", "12000.00"}
    assert all(loan["fields"]["sanctioned_amount"]["value"] == "15000.00" for loan in loans)
    assert all(loan["review_flags"] == [GROUPING_CONFLICT] for loan in loans)


def test_same_loan_hint_is_accepted_only_without_a_field_conflict() -> None:
    compatible = [
        _page(
            "img_a",
            loan_id=_text(None, "unknown"),
            sanctioned_amount=_money("15000.00"),
            same_loan_as_image_ids=["img_b"],
        ),
        _page(
            "img_b",
            loan_id=_text(None, "unknown"),
            sanctioned_amount=_money(None, "unknown"),
            disbursed_amount=_money("13230.00"),
        ),
    ]
    conflicting = [
        _page(
            "img_a",
            loan_id=_text(None, "unknown"),
            sanctioned_amount=_money("15000.00"),
            same_loan_as_image_ids=["img_b"],
        ),
        _page(
            "img_b",
            loan_id=_text(None, "unknown"),
            sanctioned_amount=_money("18000.00"),
        ),
    ]

    merged = _loans(group_raw_extracts(compatible))
    refused = _loans(group_raw_extracts(conflicting))

    assert len(merged) == 1
    assert merged[0]["image_ids"] == ["img_a", "img_b"]
    assert merged[0]["fields"]["sanctioned_amount"]["value"] == "15000.00"
    assert merged[0]["fields"]["disbursed_amount"]["value"] == "13230.00"
    assert merged[0]["review_flags"] == []

    assert len(refused) == 2
    assert {loan["fields"]["sanctioned_amount"]["value"] for loan in refused} == {
        "15000.00",
        "18000.00",
    }
    assert all(GROUPING_CONFLICT in loan["review_flags"] for loan in refused)
    assert all(FIELD_CONFLICT not in loan["review_flags"] for loan in refused)


def test_different_sanctioned_amounts_stay_separate_loans() -> None:
    pages = [
        _page(
            "img_a",
            loan_id=_text(None, "unknown"),
            sanctioned_amount=_money("15000.00"),
            disbursement_date=_date("2026-06-17"),
        ),
        _page(
            "img_b",
            loan_id=_text(None, "unknown"),
            sanctioned_amount=_money("18000.00"),
            disbursement_date=_date("2026-06-17"),
        ),
    ]

    loans = _loans(group_raw_extracts(pages))

    assert len(loans) == 2
    assert {loan["fields"]["sanctioned_amount"]["value"] for loan in loans} == {
        "15000.00",
        "18000.00",
    }
    assert all(loan["review_flags"] == [] for loan in loans)


def test_different_lenders_stay_in_different_groups() -> None:
    pages = [
        _page("img_a", lender_brand=_text("Dhanrishi"), loan_id=_text("LN-1")),
        _page("img_b", lender_brand=_text("Other"), loan_id=_text("LN-1")),
    ]

    groups = group_raw_extracts(pages)

    assert len(groups) == 2
    assert [group["lender_brand"] for group in groups] == ["Dhanrishi", "Other"]
    assert all(len(group["loans"]) == 1 for group in groups)


def test_groups_and_loans_follow_display_order() -> None:
    pages = [
        _page(
            "img_zeta",
            lender_brand=_text("Zeta"),
            loan_id=_text("Z"),
            disbursement_date=_date("2026-03-01"),
            sanctioned_amount=_money("3000.00"),
        ),
        _page(
            "img_may",
            lender_brand=_text("Alpha"),
            loan_id=_text("B"),
            disbursement_date=_date("2026-05-01"),
            sanctioned_amount=_money("2000.00"),
        ),
        _page(
            "img_jan",
            lender_brand=_text("Alpha"),
            loan_id=_text("A"),
            disbursement_date=_date("2026-01-15"),
            sanctioned_amount=_money("1000.00"),
        ),
        _page(
            "img_undated",
            lender_brand=_text("Alpha"),
            loan_id=_text("C"),
            sanctioned_amount=_money("4000.00"),
        ),
        _page(
            "img_unknown",
            lender_brand=_text(None, "unknown"),
            legal_lending_entity=_text(None, "unknown"),
            loan_id=_text("U"),
            sanctioned_amount=_money("5000.00"),
        ),
    ]

    groups = group_raw_extracts(pages)

    assert [group["lender_brand"] for group in groups] == ["Alpha", "Zeta", None]
    assert groups[-1]["legal_lending_entity"] is None
    alpha_ids = [loan["external_loan_id"] for loan in groups[0]["loans"]]
    assert alpha_ids == ["A", "B", "C"]


def test_alias_collapses_brand_spelling_into_one_group() -> None:
    pages = [
        _page(
            "img_a",
            lender_brand=_text("Dhan Rishi"),
            loan_id=_text("LN-1"),
            sanctioned_amount=_money("15000.00"),
        ),
        _page(
            "img_b",
            lender_brand=_text("Dhanrishi"),
            loan_id=_text("LN-2"),
            sanctioned_amount=_money("18000.00"),
        ),
    ]
    aliases = {"Dhan Rishi": "Dhanrishi"}

    aliased = group_raw_extracts(pages, aliases=aliases)
    separate = group_raw_extracts(pages, aliases={})

    assert len(aliased) == 1
    assert len(aliased[0]["loans"]) == 2
    assert aliased[0]["lender_brand"] == "Dhan Rishi"
    assert len(separate) == 2


def test_assignment_remerges_without_inventing_an_amount() -> None:
    conflict_pages = [
        _page("img_a", loan_id=_text("LN-A"), sanctioned_amount=_money("15000.00")),
        _page("img_b", loan_id=_text("LN-B"), sanctioned_amount=_money("18000.00")),
    ]
    combined = apply_image_assignment(
        conflict_pages,
        [{"loan_internal_id": "lon_user", "image_ids": ["img_a", "img_b"]}],
    )
    combined_loan = _loans(combined)[0]
    combined_text = json.dumps(combined)

    assert len(_loans(combined)) == 1
    assert combined_loan["loan_internal_id"] == "lon_user"
    assert combined_loan["fields"]["sanctioned_amount"]["value"] is None
    assert combined_loan["review_flags"] == [FIELD_CONFLICT]
    assert "15000.00" not in combined_text
    assert "18000.00" not in combined_text
    assert "16500.00" not in combined_text

    split_pages = [
        _page("img_a", sanctioned_amount=_money("15000.00"), disbursed_amount=_money(None, "unknown")),
        _page(
            "img_b",
            sanctioned_amount=_money(None, "unknown"),
            disbursed_amount=_money("13230.00"),
        ),
    ]
    assert len(_loans(group_raw_extracts(split_pages))) == 1
    split = lender_groups_for_assignment(
        split_pages,
        [
            {"loan_internal_id": "lon_a", "image_ids": ["img_a"]},
            {"loan_internal_id": "lon_b", "image_ids": ["img_b"]},
        ],
    )
    split_loans = {loan["loan_internal_id"]: loan for loan in _loans(split)}

    assert set(split_loans) == {"lon_a", "lon_b"}
    assert split_loans["lon_a"]["fields"]["sanctioned_amount"]["value"] == "15000.00"
    assert split_loans["lon_a"]["fields"]["disbursed_amount"]["value"] is None
    assert split_loans["lon_b"]["fields"]["disbursed_amount"]["value"] == "13230.00"
    assert split_loans["lon_b"]["fields"]["sanctioned_amount"]["value"] is None
    assert split_loans["lon_a"]["review_flags"] == []
    assert split_loans["lon_b"]["review_flags"] == []


def test_preview_shape_comes_from_extraction() -> None:
    pages = [_page("img_a", sanctioned_amount=_money("15000.00"))]

    assert lender_groups_from_extracts(pages)[0]["loans"][0]["fields"]["sanctioned_amount"][
        "value"
    ] == "15000.00"
    assert "implied_cost" not in lender_groups_from_extracts(pages)[0]["loans"][0]
