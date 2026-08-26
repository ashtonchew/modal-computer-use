from modal_computer_use._regions import (
    MODAL_NARROW_REGION_SELECTORS,
    is_modal_region_selector,
    is_modal_runtime_region_compatible,
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


def test_public_narrow_selector_accepts_provider_native_runtime_regions() -> None:
    assert is_modal_runtime_region_compatible("us-west-2", "us-west") is True
    assert is_modal_runtime_region_compatible("us-west1", "us-west") is True
    assert is_modal_runtime_region_compatible("westus3", "us-west") is True
    assert is_modal_runtime_region_compatible("ap-southeast-1", "ap-southeast") is True
    assert is_modal_runtime_region_compatible("asia-northeast3", "ap-northeast") is True


def test_region_membership_rejects_non_concrete_or_broad_selectors() -> None:
    assert is_modal_runtime_region_compatible("us-west-2", "us") is False
    assert is_modal_runtime_region_compatible("us-west", "us-west-2") is False
    assert is_modal_runtime_region_compatible("us-west", "us-west") is False


def test_granted_granular_selector_requires_exact_runtime_region() -> None:
    assert is_modal_runtime_region_compatible("us-west-2", "us-west-2") is True
    assert is_modal_runtime_region_compatible("us-west-2a", "us-west-2") is False


def test_granted_gcp_granular_selector_is_verifiable() -> None:
    assert is_verifiable_modal_region_selector("us-west1") is True
    assert is_modal_runtime_region_compatible("us-west1", "us-west1") is True


def test_granted_azure_granular_selector_is_verifiable() -> None:
    assert is_verifiable_modal_region_selector("westus3") is True
    assert is_modal_runtime_region_compatible("westus3", "westus3") is True
