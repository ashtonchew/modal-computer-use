from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
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
    roots = [ROOT / "README.md", ROOT / "SECURITY.md"]
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


def test_active_and_archived_specifications_have_one_owner() -> None:
    active_names = {path.name for path in (DOCS / "spec").glob("*.md")}
    archived_names = {path.name for path in (ARCHIVE / "spec").glob("*.md")}

    assert "modal_computer_use_spec_v7.md" in active_names
    assert active_names.isdisjoint(archived_names)
