from modal_computer_use._regions import (
    MODAL_NARROW_REGION_SELECTORS,
    is_modal_region_selector,
    is_verifiable_modal_region_selector,
)


def test_current_modal_narrow_regions_are_verifiable() -> None:
    assert MODAL_NARROW_REGION_SELECTORS
    assert all(
        is_verifiable_modal_region_selector(region)
        for region in MODAL_NARROW_REGION_SELECTORS
    )


def test_modal_region_verification_rejects_broad_selectors() -> None:
    assert is_modal_region_selector("us") is True
    assert is_modal_region_selector("eu") is True
    assert is_verifiable_modal_region_selector("us") is False
    assert is_verifiable_modal_region_selector("eu") is False


def test_modal_region_verification_accepts_workspace_granted_granular_selector() -> None:
    assert is_verifiable_modal_region_selector("us-west-2") is True


def test_modal_region_shape_rejects_malformed_values() -> None:
    for value in ("", "US-WEST", "us_west", "-us-west", "us-west-"):
        assert is_modal_region_selector(value) is False
        assert is_verifiable_modal_region_selector(value) is False
