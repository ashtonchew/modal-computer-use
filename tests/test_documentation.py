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
PYTHON_FENCE_RE = re.compile(r"```python\n(?P<source>.*?)\n```", re.DOTALL)
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
        ROOT / "examples" / "README.md",
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


def test_root_readme_logo_resolves_to_a_tracked_asset() -> None:
    source = (ROOT / "README.md").read_text(encoding="utf-8")
    logo_path = Path("docs/assets/modal-computer-use-logo.png")

    assert f'src="./{logo_path.as_posix()}"' in source
    resolved_logo_path = (ROOT / logo_path).resolve()
    assert _is_within_repository(resolved_logo_path)
    assert resolved_logo_path.is_file()


def test_onboarding_docs_use_canonical_install_and_setup_guidance() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Python 3.12 or later" in readme
    assert "quickstart.py" in readme
    assert "uv run modal run --env main quickstart.py" in readme
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


def test_release_docs_identify_v2_release_candidate() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")
    specification = (DOCS / "spec" / "product-spec.md").read_text(encoding="utf-8")

    assert "git@v1.1.0" not in readme
    assert "## Unreleased" in changelog
    assert "## 2.0.0 - 2026-08-08" in changelog
    assert "active specification for the `2.0.0` release candidate" in specification
    assert "**Previous released baseline:** `v1.1.0`" in specification
    assert "**Release identity:** `v2.0.0`" in specification


def test_hosted_documentation_release_record_names_control_points() -> None:
    source = (DOCS / "hosted-documentation-release.md").read_text(encoding="utf-8")

    for heading in (
        "## System of record",
        "## Current production baseline",
        "## Preview a change",
        "## Publish",
        "## Version navigation",
        "## Roll back",
    ):
        assert heading in source

    assert "ashtonchew/modal-computer-use-docs" in source
    assert "**Default and deployment branch:** `main`" in source
    assert "**Production owner:** Ashton Chew" in source
    assert "npm run check" in source
    assert "navigation.versions" in source
    assert "docs-v1.1.0-last-known-good" in source
    assert "5d0f4e2f82ef0906d4cb4a6cc4eeafe018dceb2e" in source
    assert "Do not force-push" in source


def test_api_docs_distinguish_authentication_reuse_from_ingress_routing() -> None:
    source = " ".join((DOCS / "api.md").read_text(encoding="utf-8").split())

    assert "exchanges the attested token once when the borrow starts" in source
    assert "Every request still crosses authenticated Modal ingress" in source


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


def _section(source: str, heading: str, next_heading: str) -> str:
    start = source.index(heading)
    end = source.index(next_heading, start)
    return source[start:end]


def test_readme_primary_path_is_one_placed_borrowed_trajectory() -> None:
    source = (ROOT / "README.md").read_text(encoding="utf-8")
    quickstart = _section(source, "## Quick start", "## Core API")

    for contract in (
        "AsyncComputerSandbox.create",
        "session_handle()",
        "@app.function",
        "region=REGION",
        "async with handle.borrow_async",
        "await computer.step",
        "uv run modal run --env main quickstart.py",
    ):
        assert contract in quickstart

    assert quickstart.count("handle.borrow_async(") == 1
    assert re.search(r"(?<!Async)ComputerSandbox\.create", quickstart) is None
    assert "full_bytes(" not in quickstart
    assert "await computer.actions.run" not in quickstart
    assert "external caller" not in quickstart.lower()

    code_blocks = [
        match.group("source") for match in PYTHON_FENCE_RE.finditer(quickstart)
    ]
    assert len(code_blocks) == 1
    compile(code_blocks[0], "README.md quickstart", "exec")


def test_local_guides_define_the_optimized_default_and_low_level_compatibility() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            DOCS / "api.md",
            DOCS / "modal-deployment.md",
            DOCS / "modal-optimization.md",
        )
    )

    for contract in (
        "async owner",
        "versioned session handle",
        "application-owned Modal Function",
        "one `borrow_async()` context",
        "pooled async HTTP client",
        "byte-backed `Screenshot`",
        "one ordered action batch",
        "`computer.step()`",
        "immediate post-action frame",
        "Low-level compatibility",
    ):
        assert contract in source

    assert "There is no `optimized=True`" in source
    assert "no performance-profile toggle" in source.lower()


def test_v2_migration_guide_covers_each_cutover_contract() -> None:
    source = (DOCS / "migration-v2.md").read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    for contract in (
        "semantic-version major",
        "`ComputerSandbox.create()`",
        "`AsyncComputerSandbox.create()`",
        "`AsyncComputerSandbox.create_unplaced()`",
        "`owner.session_handle()`",
        "`handle.borrow_async()`",
        "`computer.step()`",
        "`ComputerStepResult`",
        "`screenshots.full()`",
        "`Screenshot.bytes`",
        "`Screenshot.to_base64()`",
        "`Screenshot.data_base64`",
        "`screenshots.full_bytes()`",
        "JSON/base64 and REST routes remain available",
        "does not silently fall back",
    ):
        assert contract in normalized

    assert "| Version 1 pattern | Version 2 default | Required migration |" in source


def test_v2_migration_table_matches_the_changelog() -> None:
    migration = (DOCS / "migration-v2.md").read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")

    migration_table = _section(migration, "| Version 1 pattern", "## Screenshot compatibility")
    changelog_table = _section(
        changelog,
        "| Version 1 pattern",
        "Version 1.1 clients remain supported",
    )

    assert migration_table.strip() == changelog_table.strip()


def test_default_path_docs_preserve_cost_and_measurement_boundaries() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "README.md",
            DOCS / "performance.md",
            DOCS / "benchmarking.md",
            DOCS / "troubleshooting.md",
        )
    )

    for contract in (
        "warm capacity is off",
        "cold allocation",
        "dispatch",
        "borrow",
        "warm operation",
        "47 ms",
        "arithmetic",
        "not a measured fused turn",
        "trailing-screenshot option is a retained low-level capability",
        "not application readiness",
    ):
        assert contract.lower() in source.lower()


def test_benchmarking_uses_the_public_interleaved_measurement_seam() -> None:
    source = " ".join(
        (DOCS / "benchmarking.md").read_text(encoding="utf-8").lower().split()
    )

    for contract in (
        "measure_interleaved_promotion()",
        'screenshots.full(storage="inline")',
        "retained inline json/base64 route",
        "same pooled async client",
        "stops after the first operation failure",
        "never replaces or replays a sample",
    ):
        assert contract in source


def test_benchmarking_documents_the_executable_live_promotion_runner() -> None:
    source = (DOCS / "benchmarking.md").read_text(encoding="utf-8")

    assert "scripts/run_optimized_default_promotion.py" in source
    assert "--sample-count 30" in source
    assert "one async owner" in source
    assert "enters one borrow" in source
    assert "zero warm capacity" in source


def test_benchmarking_has_a_distinct_computer_step_promotion_gate() -> None:
    source = (DOCS / "benchmarking.md").read_text(encoding="utf-8")
    step = " ".join(
        _section(source, "## Promote Computer Step", "## Choose a command").split()
    )

    assert "scripts/run_step_promotion.py" in step
    assert "at least 100 complete paired samples" in step
    assert "actions.run(...)` followed by `screenshots.full(...)" in step
    assert "computer.step(...)" in step
    assert "deterministic causality check" in step
    assert "daemon capture timestamp after its baseline" in step
    assert "without comparing clocks across" in step
    assert "Do not wait for a browser paint" in step
    assert "does not claim per-sample MSS/XShm attribution" in step
    assert "paired bootstrap 95% confidence interval" in step
    assert "candidate p95" in step
    assert "47.10 ms" in step
    assert "not a measured fused turn" in step
    assert "non-gating engineering goal and distance metric" in step
    assert "a mutation-free placement probe and one measurement invocation" in step


def test_benchmarking_has_an_executable_weighted_input_capacity_gate() -> None:
    source = (DOCS / "benchmarking.md").read_text(encoding="utf-8")
    capacity = " ".join(
        _section(source, "## Weighted input capacity gate", "## Choose a command").split()
    )

    assert "scripts/run_input_capacity_gate.py" in capacity
    assert "200-token-per-second refill and 400-token burst" in capacity
    assert "at least 400 representative normalized input-work tokens per second" in capacity
    assert "2,000-token refill and 4,000-token burst" in capacity
    assert "0.01 aggregate cgroup CPU-seconds per normalized token" in capacity
    assert "64 MiB of RSS" in capacity
    assert "does not redefine that default" in capacity


def test_performance_requires_exact_placement_for_the_primary_trajectory() -> None:
    source = " ".join((DOCS / "performance.md").read_text(encoding="utf-8").split())

    assert "primary placed trajectory requires one exact requested region" in source
    assert "missing or broad region fails before lease acquisition" in source
    assert "unset or broad selector remains available only to explicit low-level" in source
    assert "General SDK usage | Leave `runtime.modal_region=None`" not in source


def test_named_image_documentation_matches_browser_prewarm_validation() -> None:
    source = " ".join((DOCS / "configuration.md").read_text(encoding="utf-8").split())

    assert "`browser.prewarm` remains an explicit application choice and may be `false`" in source
    assert "require an explicit `browser.kind` plus `browser.prewarm=true`" not in source


def test_article_attributes_the_screenshot_transport_and_corrects_the_opening_math() -> None:
    source = (DOCS / "drafts/modal-optimized-low-latency.md").read_text(encoding="utf-8")

    assert "separate warm screenshot and click medians added up to 47 ms" in source
    assert "This is not a measured fused turn" in source
    assert "37.25 ms benchmark request also used the raw binary HTTP endpoint" in source
    assert "persistent" in source
    assert "encoded to PNG in memory" in source


def test_examples_index_promotes_only_the_complete_trajectory() -> None:
    source = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    primary = _section(source, "## Primary path", "## Low-level compatibility")

    assert "modal_function_session_handoff.py" in primary
    assert "one borrow" in primary.lower()
    assert "pooled async HTTP" in primary
    assert "computer.step" in primary
    assert "full_bytes(" not in primary
    assert "external caller" not in primary.lower()


def test_hosted_docs_handoff_cuts_provider_loops_over_to_computer_step() -> None:
    source = (DOCS / "hosted-documentation-handoff.json").read_text(encoding="utf-8")

    assert "computer.step()" in source
    assert "computer-step-envelope-v1" in source
    assert "immediate post-action frame" in source
    assert "application-owned readiness" in source
    assert "47.10 ms" in source
    assert "not a measured fused turn" in source


def test_api_trajectory_example_uses_one_exact_requested_region() -> None:
    source = (DOCS / "api.md").read_text(encoding="utf-8")
    section = _section(
        source,
        "Use the native-async borrow context inside an async user-owned Modal Function:",
        "Constructing the context does not contact Modal",
    )

    assert 'FUNCTION_REGION = "us-west-2"' in section
    assert "Replace this with one exact region measured for your workload." in section
    assert 'FUNCTION_REGION = "us-west"' not in section
    assert section.count("handle.borrow_async(") == 1
    assert "await computer.step(" in section
    code_blocks = [match.group("source") for match in PYTHON_FENCE_RE.finditer(section)]
    assert len(code_blocks) == 1
    compile(code_blocks[0], "docs/api.md optimized trajectory", "exec")
