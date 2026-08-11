from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from modal_computer_use.image import (
    _add_x11_shared_memory_capture,
    _managed_source_mount_ignore,
    _named_image_recipe,
    _native_screenshot_source,
    default_image,
)

ROOT = Path(__file__).resolve().parents[1]


class _FakeImage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    @classmethod
    def debian_slim(cls, *, python_version: str) -> _FakeImage:
        image = cls()
        image.calls.append(("debian_slim", python_version))
        return image

    def apt_install(self, *packages: str) -> _FakeImage:
        self.calls.append(("apt_install", packages))
        return self

    def pip_install_from_pyproject(self, path: str) -> _FakeImage:
        self.calls.append(("pip_install_from_pyproject", path))
        return self

    def uv_sync(
        self,
        uv_project_dir: str,
        *,
        frozen: bool,
        uv_version: str,
    ) -> _FakeImage:
        self.calls.append(("uv_sync", (uv_project_dir, frozen, uv_version)))
        return self

    def env(self, values: dict[str, str]) -> _FakeImage:
        self.calls.append(("env", values))
        return self

    def add_local_dir(
        self,
        local_path: str,
        *,
        remote_path: str,
        copy: bool,
        ignore: tuple[str, ...],
    ) -> _FakeImage:
        self.calls.append(("add_local_dir", (local_path, remote_path, copy, ignore)))
        return self

    def run_commands(self, *commands: str) -> _FakeImage:
        self.calls.append(("run_commands", commands))
        return self

    def add_local_python_source(
        self,
        package: str,
        *,
        copy: bool = False,
        ignore: object,
    ) -> _FakeImage:
        self.calls.append(("add_local_python_source", (package, copy, ignore)))
        return self


def _fake_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "modal_computer_use.image._modal",
        lambda: SimpleNamespace(Image=_FakeImage),
    )


def _recipe_calls(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recipe: _FakeImage,
) -> list[tuple[str, object]]:
    monkeypatch.setattr(
        "modal_computer_use.image._modal",
        lambda: SimpleNamespace(Image=SimpleNamespace(debian_slim=lambda **_: recipe)),
    )
    return recipe.calls


@pytest.mark.parametrize(
    ("profile", "browser"),
    [
        ("standard", None),
        ("browser", "firefox"),
        ("browser", "chromium"),
        ("browser-gpu", "chromium"),
    ],
)
def test_all_managed_inline_recipes_build_x11_shared_memory_extension(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    browser: str | None,
) -> None:
    _fake_modal(monkeypatch)

    image = default_image(profile=profile, browser=browser)

    assert isinstance(image, _FakeImage)
    source_call = next(value for name, value in image.calls if name == "add_local_dir")
    local_path, remote_path, copy, ignore = source_call
    assert Path(local_path) == _native_screenshot_source()
    assert Path(local_path).is_absolute()
    assert Path(local_path).name == "x11_shm"
    assert remote_path == "/opt/modal-computer-use/native/x11_shm"
    assert copy is True
    assert "target/**" in ignore

    commands = next(value for name, value in image.calls if name == "run_commands")
    joined = "\n".join(commands)
    assert "rustup" in joined
    assert "1.91.0" in joined
    assert "cargo build --locked --release --features extension-module" in joined
    assert "rustup target add" not in joined
    assert "/target/release/lib_modal_computer_use_x11_shm.so" in joined
    assert "_modal_computer_use_x11_shm.so" in joined
    assert "sysconfig.get_path(\"platlib\")" in joined
    assert "import _modal_computer_use_x11_shm" in joined
    assert "/opt/modal-computer-use/native/x11_shm/canary.py" in joined
    assert any(
        name == "add_local_python_source"
        and value == ("modal_computer_use", False, _managed_source_mount_ignore)
        for name, value in image.calls
    )


def test_native_build_helper_rejects_a_missing_packaged_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    image = _FakeImage()
    monkeypatch.setattr(
        "modal_computer_use.image._native_screenshot_source",
        lambda: tmp_path / "missing",
    )

    with pytest.raises(RuntimeError, match="missing the packaged X11 shared-memory"):
        _add_x11_shared_memory_capture(image)


def test_native_build_helper_can_select_stock_zlib_without_changing_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_modal(monkeypatch)
    image = _FakeImage()

    _add_x11_shared_memory_capture(
        image,
        cargo_features=("extension-module", "stock-zlib"),
    )

    packages = next(value for name, value in image.calls if name == "apt_install")
    assert "zlib1g-dev" in packages
    commands = next(value for name, value in image.calls if name == "run_commands")
    assert "cargo build --locked --release --features extension-module,stock-zlib" in "\n".join(
        commands
    )


@pytest.mark.parametrize("variant", ["standard", "firefox", "chromium"])
def test_all_named_image_variants_use_the_same_native_recipe(
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    recipe = _FakeImage()
    calls = _recipe_calls(monkeypatch, recipe=recipe)

    assert _named_image_recipe(variant=variant, window_manager="xfce") is recipe

    assert [name for name, _ in calls].count("add_local_dir") == 1
    assert [name for name, _ in calls].count("run_commands") == 1
    assert any(
        name == "add_local_python_source"
        and value == ("modal_computer_use", True, _managed_source_mount_ignore)
        for name, value in calls
    )


def test_root_artifacts_keep_universal_wheel_and_bundle_locked_native_source(
    tmp_path: Path,
) -> None:
    uv_binary = os.environ.get("UV_BIN")
    if uv_binary is None:
        local_uv = Path.home() / ".local" / "bin" / "uv"
        uv_binary = str(local_uv) if local_uv.is_file() else shutil.which("uv")
    if uv_binary is None:
        pytest.skip("uv is required for distribution archive verification")
    environment = os.environ.copy()
    environment.setdefault("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    completed = subprocess.run(  # noqa: S603 - fixed local build command
        [uv_binary, "build", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if completed.returncode != 0 and any(
        marker in completed.stderr
        for marker in ("Failed to fetch", "No solution found", "nodename nor servname")
    ):
        pytest.skip("uv build dependencies are unavailable in this environment")
    assert completed.returncode == 0, completed.stdout + completed.stderr

    wheels = sorted(tmp_path.glob("*.whl"))
    sdists = sorted(tmp_path.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    assert wheels[0].name.endswith("-py3-none-any.whl")

    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_members = set(archive.namelist())
    with tarfile.open(sdists[0], mode="r:gz") as archive:
        sdist_members = {member.name.split("/", 1)[1] for member in archive.getmembers()}

    required = {
        "src/modal_computer_use/_native/x11_shm/Cargo.toml",
        "src/modal_computer_use/_native/x11_shm/Cargo.lock",
        "src/modal_computer_use/_native/x11_shm/canary.py",
        "src/modal_computer_use/_native/x11_shm/src/lib.rs",
    }
    assert any(member.endswith("/_native/x11_shm/Cargo.toml") for member in wheel_members)
    assert any(member.endswith("/_native/x11_shm/Cargo.lock") for member in wheel_members)
    assert any(member.endswith("/_native/x11_shm/canary.py") for member in wheel_members)
    assert all(name in sdist_members for name in required)
    assert not any(member.endswith(".so") for member in wheel_members)
    assert not any("target/" in member for member in wheel_members)
