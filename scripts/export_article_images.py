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
Renumbering after a reorder would otherwise leave the old files behind, so any
PNG in the folder that this run did not write is deleted.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO / "docs" / "drafts" / "modal-optimized-low-latency.md"
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
STRIP_PREFIX = "modal-optimized-"


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

    output = args.output or source.with_name(f"{source.stem}-images")
    output.mkdir(parents=True, exist_ok=True)

    import cairosvg

    written: set[Path] = set()
    for position, asset in enumerate(assets, start=1):
        destination = output / png_name(position, asset)
        cairosvg.svg2png(url=str(asset), write_to=str(destination), scale=args.scale)
        written.add(destination)
        print(f"{destination.relative_to(REPO)}  <-  {asset.relative_to(REPO)}")

    for stale in sorted(output.glob("*.png")):
        if stale not in written:
            stale.unlink()
            print(f"removed stale {stale.relative_to(REPO)}")

    print(f"{len(written)} images at scale {args.scale:g} in {output.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    ensure_cairo()
    raise SystemExit(main())
