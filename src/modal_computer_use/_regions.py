from __future__ import annotations

import re

# Current public narrow selectors from Modal's region-selection contract. Broad
# selectors such as ``us`` and ``eu`` intentionally do not prove handoff locality.
MODAL_NARROW_REGION_SELECTORS = frozenset(
    {
        "us-east",
        "us-central",
        "us-south",
        "us-west",
        "eu-west",
        "eu-north",
        "eu-south",
        "ap-northeast",
        "ap-southeast",
        "ap-south",
        "ap-melbourne",
        "jp",
        "au",
    }
)


def is_modal_region_selector(value: str) -> bool:
    """Return whether *value* has Modal's region-selector shape."""

    return re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value) is not None


def is_verifiable_modal_region_selector(value: str) -> bool:
    """Accept current narrow selectors and Workspace-granted granular selectors."""

    if value in MODAL_NARROW_REGION_SELECTORS:
        return True
    return re.fullmatch(
        r"[a-z][a-z0-9]*-[a-z][a-z0-9]*-[0-9][a-z0-9]*", value
    ) is not None
