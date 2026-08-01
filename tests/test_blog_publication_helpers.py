from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import edit_blog_preview  # noqa: E402
import export_article_images  # noqa: E402


def test_export_refuses_a_nonempty_unowned_output_directory(tmp_path) -> None:
    source = tmp_path / "article.md"
    source.write_text("draft", encoding="utf-8")
    output = tmp_path / "uploads"
    output.mkdir()
    personal = output / "personal.png"
    personal.write_bytes(b"not an export")

    with pytest.raises(export_article_images.BundleError, match="non-empty unowned"):
        export_article_images.owned_bundle_files(output, source)

    assert personal.read_bytes() == b"not an export"


def test_export_removes_only_manifest_owned_stale_pngs(tmp_path) -> None:
    owned_stale = tmp_path / "1_old.png"
    unrelated = tmp_path / "notes.png"
    owned_stale.write_bytes(b"old")
    unrelated.write_bytes(b"keep")

    removed = export_article_images.remove_stale_bundle_files(
        tmp_path,
        {owned_stale.name},
        {"1_current.png", "paste.md"},
    )

    assert removed == [owned_stale]
    assert not owned_stale.exists()
    assert unrelated.read_bytes() == b"keep"


def test_export_refuses_a_symlinked_output_directory(tmp_path) -> None:
    target = tmp_path / "unrelated"
    target.mkdir()
    output = tmp_path / "article-images"
    output.symlink_to(target, target_is_directory=True)

    with pytest.raises(export_article_images.BundleError, match="symlinked output"):
        export_article_images.safe_output_path(output)


def test_manifest_write_does_not_consume_the_old_fixed_temp_name(tmp_path) -> None:
    source = tmp_path / "article.md"
    source.write_text("draft", encoding="utf-8")
    unrelated = tmp_path / ".article-image-export.json.tmp"
    unrelated.write_text("keep", encoding="utf-8")

    export_article_images.write_bundle_manifest(tmp_path, source, {"paste.md"})

    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert export_article_images.owned_bundle_files(tmp_path, source) == ({"paste.md"}, False)


def test_export_preflights_every_destination_before_rendering(tmp_path) -> None:
    safe_existing = tmp_path / "1_current.png"
    safe_existing.write_bytes(b"unchanged")
    target = tmp_path / "outside.md"
    target.write_text("outside", encoding="utf-8")
    (tmp_path / "paste.md").symlink_to(target)

    unsafe = export_article_images.unsafe_bundle_destinations(
        tmp_path, {safe_existing.name, "paste.md"}
    )

    assert unsafe == [tmp_path / "paste.md"]
    assert safe_existing.read_bytes() == b"unchanged"
    assert target.read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "localhost"])
def test_blog_editor_accepts_loopback_hosts(host) -> None:
    assert edit_blog_preview.is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.10", "editor.example"],  # noqa: S104
)
def test_blog_editor_rejects_remote_hosts_without_override(host, capsys) -> None:
    result = edit_blog_preview.main(["--host", host, "--source", "missing.md"])

    assert result == 2
    assert "--allow-remote" in capsys.readouterr().err


def test_blog_editor_rejects_ipv6_even_with_remote_override(capsys) -> None:
    result = edit_blog_preview.main(
        ["--host", "::1", "--allow-remote", "--source", "missing.md"]
    )

    assert result == 2
    assert "IPv6 hosts are not supported" in capsys.readouterr().err


def test_blog_editor_parses_explicit_remote_override() -> None:
    args = edit_blog_preview.parse_args(["--host", "192.0.2.10", "--allow-remote"])

    assert args.host == "192.0.2.10"
    assert args.allow_remote is True
