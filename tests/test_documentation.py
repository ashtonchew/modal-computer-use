from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CONTEXT = ROOT / "CONTEXT.md"
CHANGELOG = ROOT / "CHANGELOG.md"
DOC_MAP = DOCS / "README.md"
ARCHIVE_MAP = DOCS / "archive" / "README.md"
ARCHIVE = DOCS / "archive"

LINK_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*(?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)",
    re.MULTILINE,
)
FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+(?P<heading>.*?)(?:\s+#+\s*)?$")
EXTERNAL_SCHEMES = {"data", "ftp", "http", "https", "mailto", "tel"}
REPOSITORY_WEB_PREFIX = "/ashtonchew/modal-computer-use/"
REPOSITORY_BLOB_PREFIX = f"{REPOSITORY_WEB_PREFIX}blob/main/"
ARCHIVE_CATEGORY_RE = re.compile(
    r"^>\s+\*\*Archive category:\*\*\s+"
    r"(?:Superseded|Rejected|Incomplete|Diagnostic|Historical)\b",
    re.IGNORECASE | re.MULTILINE,
)
ARCHIVE_DATE_OR_REVISION_RE = re.compile(
    r"^>\s+\*\*Date or revision:\*\*\s+\S", re.MULTILINE
)
ARCHIVE_QUESTION_RE = re.compile(r"^>\s+\*\*Question:\*\*\s+\S", re.MULTILINE)
ARCHIVE_DISPOSITION_RE = re.compile(r"^>\s+\*\*Disposition:\*\*\s+\S", re.MULTILINE)


def _markdown_files() -> list[Path]:
    """Return user-facing Markdown, excluding non-document trees by construction."""
    roots = [
        ROOT / "README.md",
        ROOT / "SECURITY.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "CODE_OF_CONDUCT.md",
    ]
    return [path for path in [*roots, *sorted(DOCS.rglob("*.md"))] if path.is_file()]


def _without_fenced_code(source: str) -> str:
    kept: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in source.splitlines(keepends=True):
        match = FENCE_RE.match(line)
        if fence_character is None:
            if match is None:
                kept.append(line)
                continue
            marker = match.group("marker")
            fence_character = marker[0]
            fence_length = len(marker)
            kept.append("\n")
            continue
        if match is not None:
            marker = match.group("marker")
            if marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = None
                fence_length = 0
        kept.append("\n")
    return "".join(kept)


def _link_targets(path: Path) -> list[str]:
    source = _without_fenced_code(path.read_text(encoding="utf-8"))
    return [match.group("target").strip("<>") for match in LINK_RE.finditer(source)]


def _github_slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!?(?:\[([^\]]+)\])\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("*", "")
    text = text.strip().lower()
    characters: list[str] = []
    for character in text:
        if character in {"-", "_", " "} or character.isalnum():
            characters.append(character)
    return "".join(characters).replace(" ", "-")


def _heading_anchors(path: Path) -> set[str]:
    source = _without_fenced_code(path.read_text(encoding="utf-8"))
    anchors: set[str] = set()
    occurrences: Counter[str] = Counter()
    for line in source.splitlines():
        match = HEADING_RE.match(line)
        if match is None:
            continue
        base = _github_slug(match.group("heading"))
        suffix = occurrences[base]
        occurrences[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return anchors


def _local_target(source: Path, target: str) -> tuple[Path, str]:
    split = urlsplit(target)
    fragment = unquote(split.fragment)
    raw_path = unquote(split.path)
    if not raw_path:
        return source, fragment
    if raw_path.startswith("/"):
        return ROOT / raw_path.lstrip("/"), fragment
    return (source.parent / raw_path).resolve(), fragment


def _repository_web_target(target: str) -> tuple[Path, str] | None:
    split = urlsplit(target)
    if split.netloc.lower() != "github.com" or not split.path.startswith(REPOSITORY_BLOB_PREFIX):
        return None
    repository_path = unquote(split.path.removeprefix(REPOSITORY_BLOB_PREFIX))
    return (ROOT / repository_path).resolve(), unquote(split.fragment)


def _is_within_repository(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return False
    return True


def test_local_markdown_links_and_images_resolve() -> None:
    failures: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}
    for source in _markdown_files():
        for target in _link_targets(source):
            split = urlsplit(target)
            repository_target = _repository_web_target(target)
            if repository_target is not None:
                destination, fragment = repository_target
            else:
                if target.startswith("//") or split.scheme.lower() in EXTERNAL_SCHEMES:
                    continue
                if split.scheme:
                    continue
                destination, fragment = _local_target(source, target)
            label = f"{source.relative_to(ROOT)} -> {target}"
            if not _is_within_repository(destination):
                failures.append(f"target leaves repository: {label}")
                continue
            if not destination.exists():
                failures.append(f"missing target: {label}")
                continue
            if fragment and destination.is_file() and destination.suffix.lower() == ".md":
                anchors = anchor_cache.setdefault(destination, _heading_anchors(destination))
                if fragment not in anchors:
                    failures.append(f"missing heading #{fragment}: {label}")
    assert not failures, "\n" + "\n".join(failures)


def test_repository_link_containment_rejects_parent_paths() -> None:
    assert _is_within_repository(ROOT / "docs" / "README.md")
    assert not _is_within_repository(ROOT.parent / "outside.md")


def test_root_readme_repository_links_are_absolute_https() -> None:
    failures: list[str] = []
    for target in _link_targets(ROOT / "README.md"):
        split = urlsplit(target)
        is_repository_web_link = (
            split.netloc.lower() == "github.com" and split.path.startswith(REPOSITORY_WEB_PREFIX)
        )
        is_relative_repository_link = not split.scheme and not target.startswith("#")
        if is_repository_web_link and split.scheme.lower() != "https":
            failures.append(target)
        elif is_relative_repository_link:
            destination, _fragment = _local_target(ROOT / "README.md", target)
            if destination.exists():
                failures.append(target)
    assert not failures, (
        "README.md is package metadata; repository-document links must use absolute HTTPS: "
        + ", ".join(failures)
    )


def test_onboarding_docs_use_canonical_install_and_setup_guidance() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Python 3.12 or later" in readme
    assert "quickstart.py" in readme
    assert "uv run python quickstart.py" in readme
    assert 'uv add "modal-computer-use[modal]"' in readme
    assert "git+https://github.com/ashtonchew/modal-computer-use.git" not in readme
    assert "uv run modal setup" not in readme

    onboarding_docs = [DOCS / "benchmarking.md"]
    for path in onboarding_docs:
        source = path.read_text(encoding="utf-8")
        assert "uv run modal setup" in source, path.relative_to(ROOT)
        assert "uv run modal token new" not in source, path.relative_to(ROOT)

    deployment = (DOCS / "modal-deployment.md").read_text(encoding="utf-8")
    assert "https://modal-computer-use.mintlify.app/operate/deploy" in deployment


def test_public_documentation_urls_use_the_hosted_site() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    doc_map = DOC_MAP.read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    issue_config = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(
        encoding="utf-8"
    )

    public_url = "https://modal-computer-use.mintlify.app"
    assert public_url in readme
    assert public_url in doc_map
    assert f'Documentation = "{public_url}"' in pyproject
    assert f"url: {public_url}" in issue_config


def test_public_documentation_links_use_expected_routes() -> None:
    site = "https://modal-computer-use.mintlify.app"
    expected_routes = {
        ROOT / "README.md": {
            site,
            f"{site}/start/quickstart",
            f"{site}/benchmarks/overview",
            f"{site}/benchmarks/current-results",
            f"{site}/reference/overview",
            f"{site}/operate/security",
        },
        DOC_MAP: {site},
        DOCS / "anthropic-adapter.md": {f"{site}/integrate/anthropic"},
        DOCS / "artifacts.md": {f"{site}/build/artifacts-storage"},
        DOCS / "modal-deployment.md": {f"{site}/operate/deploy"},
        DOCS / "modal-optimization.md": {f"{site}/operate/performance"},
        DOCS / "openai-adapter.md": {f"{site}/integrate/openai"},
        DOCS / "troubleshooting.md": {f"{site}/operate/troubleshooting"},
    }

    for path, expected in expected_routes.items():
        hosted_links = {
            target for target in _link_targets(path) if target.startswith(site)
        }
        assert hosted_links == expected, path.relative_to(ROOT)


def test_release_docs_identify_v1_1_release() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")
    specification = (DOCS / "spec" / "product-spec.md").read_text(encoding="utf-8")

    assert "git@v1.1.0" not in readme
    assert "## Unreleased\n\n## 1.1.0 - 2026-08-03" in changelog
    assert "active specification for the `1.1.0` release" in specification
    assert "**Released:** 2026-08-03" in specification
    assert "release identity `v1.1.0`" in specification


def test_optimized_provider_docs_name_lifecycle_command() -> None:
    source = (DOCS / "benchmarking.md").read_text(encoding="utf-8")
    start = source.index(
        "Use `modal-optimized-provider` for the optimized provider evidence"
    )
    section = source[start : source.index("## Run the provider-default comparison")]

    assert "uv run computer-use benchmark modal-optimized-provider" in section


def test_external_provider_benchmarks_are_benchmark_only() -> None:
    source = CONTEXT.read_text(encoding="utf-8")

    assert "may live in this repository" in source
    assert "`benchmark compare` is a maintained benchmark-only workflow" in source
    assert "`benchmark provider-results`\n  remains available to verify archived" in source
    assert "Neither is a public SDK compatibility\n  contract" in source
    assert "branch-only `benchmark compare`" not in source
    assert "lives outside the SDK release path" not in source


def test_historical_modal_optimization_evidence_is_commit_pinned() -> None:
    source = (DOCS / "benchmarking.md").read_text(encoding="utf-8")

    assert "modal-optimization-results-2026-07-19.json" in source
    assert "8c21cf1338fd747dca57bca6941c307270069712" in source
    assert "6f860de38df716c7cfdc0a23b186049751f34cd8" in source
    assert "37f977f80de93800c005caeec7ead5222b00b040" in source
    assert "not a current workflow" in source
    assert "current checkout is not a valid reproduction environment" in source
    for replacement in (
        "modal-optimized-provider",
        "benchmark compare",
        "provider-results",
        "daemon-observation-stream",
        "modal-region-ab",
        "run_modal_v2_candidate_benchmark.py",
        "run_modal_optimized_frontier_benchmark.py",
    ):
        assert replacement in source


def test_v1_changelog_has_compatibility_migrations() -> None:
    source = CHANGELOG.read_text(encoding="utf-8")
    release = source[source.index("## 1.0.0 - 2026-07-31") : source.index("## 0.1.0")]

    for removed, replacement in (
        ("SandboxManager", "ComputerSandboxManager"),
        ("modal_workspace_billing_report", "modal_billing_report"),
        ("XTestPointerController", "X11InputSession"),
    ):
        assert f"| `{removed}` | `{replacement}` |" in release
    assert "without a deprecation window" in release


def test_documentation_map_links_each_current_top_level_doc_once() -> None:
    canonical = {
        path.resolve()
        for path in DOCS.glob("*.md")
        if path.name != "README.md"
    }
    linked = Counter()
    for target in _link_targets(DOC_MAP):
        split = urlsplit(target)
        if split.scheme or target.startswith("#"):
            continue
        destination, _fragment = _local_target(DOC_MAP, target)
        if destination in canonical:
            linked[destination] += 1

    missing = sorted(path.name for path in canonical if linked[path] == 0)
    repeated = sorted(path.name for path in canonical if linked[path] > 1)
    assert not missing and not repeated, (
        f"docs/README.md canonical links: missing={missing}, repeated={repeated}"
    )


def test_archived_documents_have_one_index_entry_and_structured_disposition() -> None:
    archived = {
        path.resolve() for path in ARCHIVE.rglob("*.md") if path.resolve() != ARCHIVE_MAP.resolve()
    }
    archive_links = Counter()
    for target in _link_targets(ARCHIVE_MAP):
        split = urlsplit(target)
        if split.scheme:
            continue
        destination, _fragment = _local_target(ARCHIVE_MAP, target)
        archive_links[destination] += 1

    assert archived
    for path in archived:
        assert archive_links[path.resolve()] == 1
        source = _without_fenced_code(path.read_text(encoding="utf-8"))
        relative = path.relative_to(ROOT)
        assert ARCHIVE_CATEGORY_RE.search(source), f"{relative} needs an archive category"
        assert ARCHIVE_DATE_OR_REVISION_RE.search(source), f"{relative} needs a date or revision"
        assert ARCHIVE_QUESTION_RE.search(source), f"{relative} needs a question"
        assert ARCHIVE_DISPOSITION_RE.search(source), f"{relative} needs a disposition"


def test_product_specification_has_one_stable_owner() -> None:
    active_names = {path.name for path in (DOCS / "spec").glob("*.md")}
    archived_names = {path.name for path in (ARCHIVE / "spec").glob("*.md")}

    assert active_names == {"product-spec.md"}
    assert not archived_names
