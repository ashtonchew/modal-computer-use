from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "hosted-documentation-handoff.json"
SCRIPT = runpy.run_path(
    str(ROOT / "scripts" / "validate_hosted_documentation_handoff.py")
)
validate_manifest = SCRIPT["validate_manifest"]
validate_package_version = SCRIPT["validate_package_version"]

EXPECTED_ROUTES = {
    "/index",
    "/start/quickstart",
    "/start/examples",
    "/start/installation",
    "/build/action-batches",
    "/build/browser-automation",
    "/build/create-and-attach",
    "/build/screenshots-recordings",
    "/build/session-handoff",
    "/build/artifacts-storage",
    "/integrate/openai",
    "/integrate/anthropic",
    "/operate/configure",
    "/operate/deploy",
    "/operate/performance",
    "/operate/security",
    "/operate/troubleshooting",
    "/operate/warm-capacity",
    "/reference/configuration",
    "/reference/errors-and-models",
    "/reference/namespaces",
    "/reference/overview",
    "/reference/openapi",
    "/reference/migration-v2",
    "/benchmarks/current-results",
    "/benchmarks/input-capacity",
    "/benchmarks/latency-evidence",
    "/benchmarks/overview",
    "/benchmarks/reproducibility",
    "/benchmarks/run",
}


def _manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_handoff_manifest_covers_every_hosted_route() -> None:
    manifest = _manifest()
    pages = manifest["pages"]
    assert isinstance(pages, list)

    assert {page["route"] for page in pages} == EXPECTED_ROUTES
    assert validate_manifest(manifest) == []


def test_handoff_manifest_maps_each_page_to_release_controls() -> None:
    pages = _manifest()["pages"]
    required_fields = {
        "route",
        "page_class",
        "source_local",
        "v2_behavior",
        "v1_preservation",
        "tests",
        "redirects",
        "owner",
        "preview",
        "publish_order",
        "rollback",
    }

    for page in pages:
        assert required_fields <= page.keys(), page["route"]
        assert all((ROOT / source).is_file() for source in page["source_local"]), page[
            "route"
        ]
        assert page["v1_preservation"]["route"].startswith("/v1/"), page["route"]
        assert page["owner"] == "@ashtonchew", page["route"]
        assert page["preview"] == "Mintlify pull-request preview", page["route"]
        assert page["publish_order"] == 3, page["route"]


def test_handoff_manifest_requires_safe_docs_release_controls() -> None:
    manifest = _manifest()
    repository = manifest["documentation_repository"]
    release = manifest["release"]

    assert repository["slug"] == "ashtonchew/modal-computer-use-docs"
    assert repository["deployment_branch"] == "main"
    assert repository["branch_protection"]["required"] is True
    assert repository["branch_protection"]["required_check"] == "Docs"
    assert repository["branch_protection"]["pull_request_required"] is True
    assert release["required_package_version"] == "2.0.0"
    assert release["last_known_good"]["tag"] == "docs-v1.1.0-last-known-good"
    assert release["last_known_good"]["revision"] == (
        "5d0f4e2f82ef0906d4cb4a6cc4eeafe018dceb2e"
    )
    assert release["version_navigation"]["latest"] == "2.x"
    assert release["version_navigation"]["previous"] == "1.x"
    assert release["publication_order"] == [
        "runtime artifacts",
        "Python package 2.0.0",
        "hosted documentation",
    ]


def test_package_version_check_accepts_only_the_manifest_version(tmp_path: Path) -> None:
    manifest = _manifest()
    matching = tmp_path / "matching.toml"
    matching.write_text('[project]\nversion = "2.0.0"\n', encoding="utf-8")
    mismatch = tmp_path / "mismatch.toml"
    mismatch.write_text('[project]\nversion = "1.1.0"\n', encoding="utf-8")

    assert validate_package_version(manifest, matching) == []
    assert validate_package_version(manifest, mismatch) == [
        "package version is 1.1.0; hosted docs require 2.0.0"
    ]


def test_handoff_validator_rejects_missing_routes() -> None:
    manifest = _manifest()
    manifest["pages"] = manifest["pages"][1:]

    errors = validate_manifest(manifest)

    assert any("missing hosted routes" in error for error in errors)


def test_handoff_validator_rejects_unprotected_main() -> None:
    manifest = _manifest()
    manifest["documentation_repository"]["branch_protection"]["required"] = False

    errors = validate_manifest(manifest)

    assert "documentation main must be protected before publication" in errors


def test_handoff_validator_does_not_read_secrets(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.toml"
    invalid.write_text('[project]\nversion = "not-a-version"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="package version must be a stable semantic version"):
        validate_package_version(_manifest(), invalid)
