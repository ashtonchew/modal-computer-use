from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import textwrap
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = runpy.run_path(str(ROOT / "scripts" / "smoke_distribution_install.py"))
select_distributions = SCRIPT["select_distributions"]
clean_probe_environment = SCRIPT["clean_probe_environment"]
validate_sdist = SCRIPT["_validate_sdist"]
installed_daemon_protocol_probe = SCRIPT["installed_daemon_protocol_probe"]
UV_EXECUTABLE = os.environ.get("UV_EXECUTABLE") or shutil.which("uv") or "uv"

IMAGE_RUNTIME_FILES = (
    Path("modal_computer_use") / "_image_runtime" / "pyproject.toml",
    Path("modal_computer_use") / "_image_runtime" / "uv.lock",
)


def _build_distributions(output: Path) -> tuple[Path, Path]:
    subprocess.run(  # noqa: S603 - fixed uv command and repository build context.
        [UV_EXECUTABLE, "build", "--wheel", "--sdist", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        env=_clean_environment(),
    )
    return select_distributions(output)


def _clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"} or name.startswith(
            ("MODAL_", "OPENAI_", "ANTHROPIC_")
        ):
            environment.pop(name, None)
    return environment


def _archive_member_names(distribution: Path) -> set[Path]:
    if distribution.suffix == ".whl":
        with zipfile.ZipFile(distribution) as archive:
            return {Path(name) for name in archive.namelist()}

    with tarfile.open(distribution, mode="r:gz") as archive:
        members = {Path(member.name) for member in archive.getmembers()}
    return {Path(*path.parts[1:]) if len(path.parts) > 1 else path for path in members}


def _installed_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _install_without_dependencies(distribution: Path, venv: Path) -> Path:
    subprocess.run(  # noqa: S603 - fixed uv command and test-owned venv path.
        [UV_EXECUTABLE, "venv", str(venv), "--python", sys.executable],
        cwd=venv.parent,
        check=True,
        env=_clean_environment(),
    )
    installed_python = _installed_python(venv)
    subprocess.run(  # noqa: S603 - fixed uv command and test-owned distribution path.
        [
            UV_EXECUTABLE,
            "pip",
            "install",
            "--python",
            str(installed_python),
            "--no-deps",
            str(distribution),
        ],
        cwd=venv.parent,
        check=True,
        env=_clean_environment(),
    )
    return installed_python


def _probe_installed_image_runtime() -> str:
    return textwrap.dedent(
        f"""
        from importlib.metadata import distribution
        from pathlib import Path
        import subprocess
        import tomllib

        package_root = Path(distribution("modal-computer-use").locate_file("modal_computer_use"))
        runtime_root = package_root / "_image_runtime"
        project = tomllib.loads((runtime_root / "pyproject.toml").read_text(encoding="utf-8"))
        assert project["project"]["dependencies"]
        assert (runtime_root / "uv.lock").is_file()

        completed = subprocess.run(
            [{UV_EXECUTABLE!r}, "lock", "--check", "--project", str(runtime_root)],
            cwd=Path.cwd(),
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        """
    )


def test_select_distributions_requires_exact_pair(tmp_path: Path) -> None:
    wheel = tmp_path / "modal_computer_use-1.1.0-py3-none-any.whl"
    sdist = tmp_path / "modal_computer_use-1.1.0.tar.gz"
    wheel.touch()
    sdist.touch()

    assert select_distributions(tmp_path) == (wheel, sdist)


@pytest.mark.parametrize("extra_name", [None, "duplicate.whl", "duplicate.tar.gz"])
def test_select_distributions_rejects_missing_or_duplicate_files(
    tmp_path: Path, extra_name: str | None
) -> None:
    if extra_name is not None:
        (tmp_path / "modal_computer_use-1.1.0-py3-none-any.whl").touch()
        (tmp_path / "modal_computer_use-1.1.0.tar.gz").touch()
        (tmp_path / extra_name).touch()

    with pytest.raises(ValueError, match="expected one wheel and one sdist"):
        select_distributions(tmp_path)


def test_clean_probe_environment_removes_checkout_and_provider_state() -> None:
    source = {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/private/checkout",
        "VIRTUAL_ENV": "/private/editable",
        "MODAL_TOKEN_ID": "secret",
        "MODAL_TOKEN_SECRET": "secret",
        "OPENAI_API_KEY": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "UNRELATED_SETTING": "retained",
    }

    assert clean_probe_environment(source) == {
        "PATH": "/usr/bin",
        "UNRELATED_SETTING": "retained",
    }


def test_sdist_probe_uses_the_clean_environment(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    installed_python = tmp_path / "sdist-venv" / "bin" / "python"
    globals_ = validate_sdist.__globals__

    monkeypatch.setitem(
        globals_,
        "_create_venv",
        lambda *_args, **_kwargs: installed_python,
    )

    def record_run(*_args: str, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setitem(globals_, "_run", record_run)
    monkeypatch.setitem(
        globals_,
        "clean_probe_environment",
        lambda: {"PATH": "/usr/bin", "UNRELATED_SETTING": "retained"},
    )
    monkeypatch.setitem(globals_, "_unused_loopback_port", lambda: 43123)

    class _FinishedDaemon:
        pid = 1

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(
        globals_["subprocess"],
        "Popen",
        lambda *_args, **_kwargs: _FinishedDaemon(),
    )

    validate_sdist(
        tmp_path / "modal_computer_use-2.0.0.tar.gz",
        root=tmp_path,
        python="3.12",
    )

    assert calls[-1]["env"] == {
        "PATH": "/usr/bin",
        "UNRELATED_SETTING": "retained",
    }


def test_distribution_daemon_probe_covers_optimized_protocol_seam() -> None:
    probe = installed_daemon_protocol_probe(port=43123)

    assert 'base_url = "http://127.0.0.1:43123"' in probe

    for route in (
        "/v1/leases/acquire",
        "/v1/screenshots/full/raw",
        "/v1/actions/run",
        "/v1/steps",
        "/v1/receipts/status",
        "/v1/leases/release",
    ):
        assert route in probe
    for header in (
        "x-computer-use-lease-id",
        "x-computer-use-lease-epoch",
        "x-computer-use-lease-fence",
        "x-computer-use-lease-token",
        "x-computer-use-operation-sequence",
        "x-computer-use-sha256",
        "x-computer-use-size-bytes",
        "x-computer-use-capture-backend",
        "x-computer-use-step-protocol",
    ):
        assert header in probe
    assert "computer-step-envelope-v1" in probe
    assert "decode_step_envelope" in probe
    assert "ComputerStepResult" in probe
    assert "result.actions.ok" in probe
    assert "result.screenshot.as_bytes()" in probe
    assert "result.timing.action_ms" in probe
    source = (ROOT / "scripts" / "smoke_distribution_install.py").read_text(
        encoding="utf-8"
    )
    assert source.count("assert callable(BorrowedComputer.step)") == 2
    assert source.count("assert callable(AsyncBorrowedComputer.step)") == 2

    # The clean distribution check must never echo response bodies or secrets.
    assert "print(" not in probe
    assert "response.text" not in probe


def test_distribution_daemon_uses_an_isolated_runtime_directory() -> None:
    source = (ROOT / "scripts" / "smoke_distribution_install.py").read_text(encoding="utf-8")

    assert '"COMPUTER_USE_RUNTIME_DIR": str(probe / "daemon-runtime")' in source


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return _build_distributions(tmp_path_factory.mktemp("distribution") / "dist")


def test_built_distributions_include_the_image_runtime_project(
    built_distributions: tuple[Path, Path],
) -> None:
    distributions = built_distributions

    for distribution in distributions:
        names = _archive_member_names(distribution)
        if distribution.suffix == ".whl":
            assert set(IMAGE_RUNTIME_FILES) <= names
        else:
            assert set(Path("src") / path for path in IMAGE_RUNTIME_FILES) <= names


def test_installed_distributions_use_the_image_runtime_project_from_any_cwd(
    built_distributions: tuple[Path, Path], tmp_path: Path
) -> None:
    for distribution in built_distributions:
        venv = tmp_path / distribution.suffix.removeprefix(".")
        installed_python = _install_without_dependencies(distribution, venv)
        unrelated_cwd = tmp_path / f"{distribution.stem}-empty"
        unrelated_cwd.mkdir()
        subprocess.run(  # noqa: S603 - fixed interpreter and test-owned probe.
            [str(installed_python), "-c", _probe_installed_image_runtime()],
            cwd=unrelated_cwd,
            check=True,
            env=_clean_environment(),
        )


def test_image_runtime_dependencies_match_root_core_dependencies() -> None:
    root_project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime_project_path = ROOT / "src" / "modal_computer_use" / "_image_runtime" / "pyproject.toml"
    runtime_project = tomllib.loads(runtime_project_path.read_text(encoding="utf-8"))

    assert set(runtime_project["project"]["dependencies"]) == set(
        root_project["project"]["dependencies"]
    )
    assert runtime_project["tool"]["uv"]["required-version"] == root_project["tool"]["uv"][
        "required-version"
    ]
