#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["cairosvg==2.7.1"]
# ///

"""Export a draft's diagrams to PNG, numbered in the order they appear.

The article references SVGs from docs/assets. Publishing surfaces often want
raster images, and they want them in reading order. This walks the draft, finds
every image reference in document order, and writes a PNG per reference into a
folder beside the draft.

    uv run --script scripts/export_article_images.py
    uv run --script scripts/export_article_images.py --source docs/drafts/other.md

Names are the position followed by the source stem with the article prefix
removed, so docs/assets/modal-optimized-agent-loop.svg becomes 1_agent-loop.png.
The bundle has a manifest of files created by this tool. Renumbering removes
only stale PNGs listed in that manifest; unrelated files are never pruned.

The folder also gets paste.md, the draft with every image reference replaced by
a visible placeholder naming the PNG that belongs there. Editors that cannot
resolve relative paths, which is most of them, need the prose and the uploads
separately, and the placeholder says which file goes where.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO / "docs" / "drafts" / "modal-optimized-low-latency.md"
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
STRIP_PREFIX = "modal-optimized-"
PUBLICATION_BRANCH = "draft/modal-computer-use-latency-article"
BLOB_URL = f"https://github.com/ashtonchew/modal-computer-use/blob/{PUBLICATION_BRANCH}/"
EXTERNAL_SCHEMES = ("http://", "https://", "data:", "mailto:", "ftp:", "tel:")
BUNDLE_MANIFEST = ".article-image-export.json"
BUNDLE_VERSION = 1


class BundleError(RuntimeError):
    """The output directory is not a bundle this script can safely update."""


def ensure_cairo() -> None:
    """Re-exec with DYLD_LIBRARY_PATH set if cairocffi cannot find libcairo.

    Homebrew installs libcairo outside the dyld search path, and cairocffi
    resolves it by soname at import time. Preloading with ctypes does not help,
    because cairocffi calls dlopen itself. The variable has to be present when
    the process starts, so the only fix is to set it and start again.
    """
    if sys.platform != "darwin" or os.environ.get("_ARTICLE_IMAGES_REEXEC"):
        return
    try:
        import cairosvg  # noqa: F401

        return
    except OSError:
        pass

    roots = []
    brew = shutil.which("brew")
    if brew:
        try:
            prefix = subprocess.run(  # noqa: S603 - resolved brew binary and a fixed argument.
                [brew, "--prefix"], capture_output=True, text=True, timeout=15, check=False
            )
            if prefix.returncode == 0 and prefix.stdout.strip():
                roots.append(Path(prefix.stdout.strip()) / "lib")
        except (OSError, subprocess.SubprocessError):
            pass
    roots += [Path("/opt/homebrew/lib"), Path("/usr/local/lib")]

    for root in roots:
        if (root / "libcairo.2.dylib").exists():
            existing = os.environ.get("DYLD_LIBRARY_PATH")
            os.environ["DYLD_LIBRARY_PATH"] = f"{root}:{existing}" if existing else str(root)
            os.environ["_ARTICLE_IMAGES_REEXEC"] = "1"
            # Replacing this process is the point: dyld reads the variable at startup.
            os.execv(sys.executable, [sys.executable, *sys.argv])  # noqa: S606

    sys.exit(
        "cairosvg cannot load libcairo. Install it with `brew install cairo`, "
        "or set DYLD_LIBRARY_PATH to the directory holding libcairo.2.dylib."
    )


def references(source: Path) -> list[Path]:
    """Every local image the draft references, in document order, deduplicated."""
    seen: set[Path] = set()
    found: list[Path] = []
    for match in IMAGE.finditer(source.read_text(encoding="utf-8")):
        target = match.group(1)
        if target.startswith(("http://", "https://", "data:")):
            continue
        resolved = (source.parent / target).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    return found


def png_name(position: int, asset: Path) -> str:
    stem = asset.stem.removeprefix(STRIP_PREFIX)
    return f"{position}_{stem}.png"


def default_output_for(source: Path) -> Path:
    return source.with_name(f"{source.stem}-images").absolute()


def safe_output_path(requested: Path) -> Path:
    output = requested.absolute()
    if output.is_symlink():
        raise BundleError(f"Refusing symlinked output directory: {output}")
    if output.exists() and not output.is_dir():
        raise BundleError(f"Output path is not a directory: {output}")
    return output


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def source_identity(source: Path) -> str:
    try:
        return source.relative_to(REPO).as_posix()
    except ValueError:
        return str(source)


def owned_bundle_files(output: Path, source: Path) -> tuple[set[str], bool]:
    """Return files the bundle owns and whether this is the canonical legacy bundle."""
    manifest = output / BUNDLE_MANIFEST
    if manifest.is_symlink():
        raise BundleError(f"Refusing symlinked bundle manifest: {manifest}")
    if not manifest.exists():
        if not output.exists() or not any(output.iterdir()):
            return set(), False
        if output == default_output_for(source):
            # Older canonical bundles predate the manifest. The first managed run may
            # overwrite current export names, but it never deletes an unlisted file.
            return set(), True
        raise BundleError(
            f"Refusing non-empty unowned output directory: {output}. "
            "Choose an empty directory or a bundle created by this script."
        )

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleError(f"Cannot read bundle manifest {manifest}: {error}") from error
    if not isinstance(payload, dict):
        raise BundleError(f"Bundle manifest is not an object: {manifest}")
    if payload.get("version") != BUNDLE_VERSION:
        raise BundleError(f"Unsupported bundle manifest version in {manifest}")
    if payload.get("source") != source_identity(source):
        raise BundleError(f"Bundle manifest belongs to a different source: {manifest}")
    files = payload.get("files")
    if not isinstance(files, list) or not all(isinstance(name, str) for name in files):
        raise BundleError(f"Bundle manifest has an invalid file list: {manifest}")
    if any(Path(name).name != name or name == BUNDLE_MANIFEST for name in files):
        raise BundleError(f"Bundle manifest contains an unsafe file name: {manifest}")
    return set(files), False


def write_bundle_manifest(output: Path, source: Path, files: set[str]) -> None:
    manifest = output / BUNDLE_MANIFEST
    payload = {
        "version": BUNDLE_VERSION,
        "source": source_identity(source),
        "files": sorted(files),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="x",
            encoding="utf-8",
            dir=output,
            prefix=f"{BUNDLE_MANIFEST}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, indent=2) + "\n")
        temporary.replace(manifest)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def remove_stale_bundle_files(
    output: Path, owned_files: set[str], current_files: set[str]
) -> list[Path]:
    removed: list[Path] = []
    for name in sorted(owned_files - current_files):
        stale = output / name
        if stale.suffix == ".png" and (stale.exists() or stale.is_symlink()):
            stale.unlink()
            removed.append(stale)
    return removed


def unsafe_bundle_destinations(output: Path, files: set[str]) -> list[Path]:
    return sorted(
        (
            destination
            for name in files
            if (destination := output / name).is_symlink()
            or (destination.exists() and not destination.is_file())
        ),
        key=str,
    )


def absolute_link(source: Path, target: str) -> str | None:
    """The GitHub URL for a link the draft states relative to its own folder.

    The draft cites artifacts by relative path, which resolves where the draft
    lives and nowhere else. paste.md sits one folder deeper, and the published
    copy sits on a site that has no repository under it at all, so a relative
    path is wrong in both places. Returns None for anything already absolute or
    pointing outside the repository, leaving the original text alone.
    """
    if target.startswith(EXTERNAL_SCHEMES) or target.startswith(("/", "#")):
        return None
    path, _, fragment = target.partition("#")
    if not path:
        return None
    resolved = (source.parent / path).resolve()
    try:
        relative = resolved.relative_to(REPO)
    except ValueError:
        return None
    if not resolved.exists():
        return None
    return f"{BLOB_URL}{relative}" + (f"#{fragment}" if fragment else "")


def paste_copy(source: Path, names: dict[Path, str]) -> str:
    """The draft rewritten for pasting into a publishing surface.

    Image references become placeholders naming the PNG that belongs there, and
    the alt text is kept, because it is the caption a reader would want and it
    says which figure the placeholder stands for. Every remaining relative link
    becomes a GitHub URL, so the citations survive the move off disk.
    """

    def swap_image(match: re.Match[str]) -> str:
        target = match.group(1)
        if target.startswith(EXTERNAL_SCHEMES):
            return match.group(0)
        resolved = (source.parent / target).resolve()
        name = names.get(resolved)
        if name is None:
            return match.group(0)
        alt = match.group(0)[2 : match.group(0).index("](")]
        return f"[{name}  ::  {alt}]"

    def swap_link(match: re.Match[str]) -> str:
        url = absolute_link(source, match.group(1))
        if url is None:
            return match.group(0)
        return match.group(0).replace(f"({match.group(1)})", f"({url})")

    # Images first: the placeholder has no parentheses, so the link pass cannot
    # see what is left of one and try to rewrite it.
    text = IMAGE.sub(swap_image, source.read_text(encoding="utf-8"))
    return LINK.sub(swap_link, text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_file():
        sys.exit(f"No draft at {source}")

    assets = references(source)
    if not assets:
        sys.exit(f"{source.name} references no local images")

    missing = [a for a in assets if not a.is_file()]
    if missing:
        sys.exit("Referenced image is missing: " + ", ".join(str(m) for m in missing))

    try:
        output = safe_output_path(args.output or default_output_for(source))
        owned_files, legacy_bundle = owned_bundle_files(output, source)
    except BundleError as error:
        sys.exit(str(error))
    output.mkdir(parents=True, exist_ok=True)

    import cairosvg

    names = {asset: png_name(position, asset) for position, asset in enumerate(assets, start=1)}
    current_files = {*names.values(), "paste.md"}
    if not legacy_bundle:
        conflicts = sorted(
            name for name in current_files if (output / name).exists() and name not in owned_files
        )
        if conflicts:
            sys.exit("Refusing to overwrite unowned bundle files: " + ", ".join(conflicts))
    unsafe = unsafe_bundle_destinations(output, current_files)
    if unsafe:
        sys.exit("Refusing unsafe bundle destinations: " + ", ".join(map(str, unsafe)))

    written: set[Path] = set()
    for asset in assets:
        name = names[asset]
        destination = output / name
        cairosvg.svg2png(url=str(asset), write_to=str(destination), scale=args.scale)
        written.add(destination)
        print(f"{display_path(destination)}  <-  {display_path(asset)}")

    for stale in remove_stale_bundle_files(output, owned_files, current_files):
        print(f"removed stale {display_path(stale)}")

    paste = output / "paste.md"
    paste.write_text(paste_copy(source, names), encoding="utf-8")
    write_bundle_manifest(output, source, current_files)
    print(display_path(paste))

    print(f"{len(written)} images at scale {args.scale:g} in {display_path(output)}")
    return 0


if __name__ == "__main__":
    ensure_cairo()
    raise SystemExit(main())
