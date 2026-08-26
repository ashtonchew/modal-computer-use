from __future__ import annotations

import argparse
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STABLE_SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
EXPECTED_ROUTES = frozenset(
    {
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
)
REQUIRED_PAGE_FIELDS = frozenset(
    {
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
)


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def validate_manifest(manifest: object) -> list[str]:
    """Return plain-language publication blockers without reading external state."""
    errors: list[str] = []
    root = _mapping(manifest)
    if root is None:
        return ["handoff manifest must be a JSON object"]
    if root.get("schema_version") != 1:
        errors.append("handoff manifest schema_version must be 1")

    repository = _mapping(root.get("documentation_repository"))
    if repository is None:
        errors.append("documentation_repository must be an object")
    else:
        if repository.get("slug") != "ashtonchew/modal-computer-use-docs":
            errors.append("documentation repository must be ashtonchew/modal-computer-use-docs")
        if repository.get("deployment_branch") != "main":
            errors.append("documentation deployment branch must be main")
        protection = _mapping(repository.get("branch_protection"))
        if protection is None or protection.get("required") is not True:
            errors.append("documentation main must be protected before publication")
        elif (
            protection.get("pull_request_required") is not True
            or protection.get("direct_push_allowed") is not False
            or protection.get("required_check") != "Docs"
        ):
            errors.append(
                "documentation main protection must require a pull request and Docs check"
            )

    release = _mapping(root.get("release"))
    if release is None:
        errors.append("release must be an object")
    else:
        if release.get("publication_authorization_required") is not True:
            errors.append("publication must require explicit operator authorization")
        required_package_version = release.get("required_package_version")
        if not isinstance(required_package_version, str) or not STABLE_SEMVER.fullmatch(
            required_package_version
        ):
            errors.append("required package version must be stable semver")
        if release.get("publication_order") != [
            "runtime artifacts",
            f"Python package {required_package_version}",
            "hosted documentation",
        ]:
            errors.append(
                "publication order must be runtime artifacts, package, then documentation"
            )
        last_good = _mapping(release.get("last_known_good"))
        if last_good is None or last_good.get("must_exist_before_publication") is not True:
            errors.append("the last-known-good documentation target must exist before publication")
        navigation = _mapping(release.get("version_navigation"))
        if navigation is None or (
            navigation.get("latest") != "2.x"
            or navigation.get("previous") != "1.x"
            or navigation.get("preserve_previous_pages") is not True
        ):
            errors.append("version navigation must preserve 1.x beside the latest 2.x pages")

    pages = root.get("pages")
    if not isinstance(pages, list):
        return [*errors, "pages must be a list"]
    routes: list[str] = []
    for index, value in enumerate(pages):
        page = _mapping(value)
        if page is None:
            errors.append(f"page {index} must be an object")
            continue
        route = page.get("route")
        label = route if isinstance(route, str) else f"page {index}"
        missing_fields = sorted(REQUIRED_PAGE_FIELDS.difference(page))
        if missing_fields:
            errors.append(f"{label} is missing fields: {', '.join(missing_fields)}")
        if isinstance(route, str):
            routes.append(route)
        preservation = _mapping(page.get("v1_preservation"))
        if preservation is None or not str(preservation.get("route", "")).startswith("/v1/"):
            errors.append(f"{label} must preserve a version 1 route")
        if page.get("owner") != "@ashtonchew":
            errors.append(f"{label} must name the documentation owner")
        if page.get("preview") != "Mintlify pull-request preview":
            errors.append(f"{label} must use the Mintlify pull-request preview")
        if page.get("publish_order") != 3:
            errors.append(f"{label} must publish after runtime artifacts and the package")

    route_set = set(routes)
    missing_routes = sorted(EXPECTED_ROUTES - route_set)
    extra_routes = sorted(route_set - EXPECTED_ROUTES)
    if missing_routes:
        errors.append(f"missing hosted routes: {', '.join(missing_routes)}")
    if extra_routes:
        errors.append(f"unexpected hosted routes: {', '.join(extra_routes)}")
    if len(routes) != len(route_set):
        errors.append("hosted routes must be unique")
    return errors


def validate_package_version(manifest: object, pyproject_path: Path) -> list[str]:
    """Check the package identity required by the handoff without loading application code."""
    root = _mapping(manifest)
    release = _mapping(root.get("release")) if root is not None else None
    required = release.get("required_package_version") if release is not None else None
    if not isinstance(required, str) or STABLE_SEMVER.fullmatch(required) is None:
        raise ValueError("manifest package version must be a stable semantic version")

    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or STABLE_SEMVER.fullmatch(version) is None:
        raise ValueError("package version must be a stable semantic version")
    if version != required:
        return [f"package version is {version}; hosted docs require {required}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the offline hosted-documentation release handoff."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "docs/hosted-documentation-handoff.json",
    )
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    errors = [*validate_manifest(manifest), *validate_package_version(manifest, args.pyproject)]
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    print("hosted-documentation handoff is ready for an authorized preview")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
