from __future__ import annotations

import argparse
import os
import signal
import subprocess
import tempfile
import textwrap
from pathlib import Path


def select_distributions(directory: Path) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(
            f"expected one wheel and one sdist in {directory}; "
            f"found wheel={len(wheels)}, sdist={len(sdists)}"
        )
    return wheels[0], sdists[0]


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)  # noqa: S603


def _installed_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _installed_script(venv: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return venv / ("Scripts" if os.name == "nt" else "bin") / f"{name}{suffix}"


def _create_venv(venv: Path, *, python: str) -> Path:
    _run("uv", "venv", str(venv), "--python", python)
    return _installed_python(venv)


def _validate_wheel(wheel: Path, *, root: Path, python: str) -> None:
    venv = root / "wheel-venv"
    installed_python = _create_venv(venv, python=python)
    _run("uv", "pip", "install", "--python", str(installed_python), str(wheel))

    probe = root / "probe"
    probe.mkdir()
    _run(
        str(installed_python),
        "-c",
        textwrap.dedent(
            """
            import sys
            from importlib.metadata import entry_points, version
            from pathlib import Path

            import modal_computer_use
            from modal_computer_use.daemon.app import create_app
            from modal_computer_use.daemon.settings import DaemonSettings

            module_path = Path(modal_computer_use.__file__).resolve()
            assert module_path.is_relative_to(Path(sys.prefix).resolve())
            assert version("modal-computer-use") == modal_computer_use.__version__

            scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
            assert scripts["computer-use"] == "modal_computer_use.cli:main"
            assert scripts["computer-use-daemon"] == "modal_computer_use.daemon.__main__:main"

            app = create_app(
                DaemonSettings(
                    backend="mock",
                    artifacts_dir=Path("artifacts"),
                    recordings_dir=Path("recordings"),
                    local_token="dev",
                )
            )
            assert app.title == "modal-computer-use daemon"
            assert app.version == modal_computer_use.__version__
            """
        ),
        cwd=probe,
    )

    computer_use = _installed_script(venv, "computer-use")
    _run(str(computer_use), "--help", cwd=probe)
    _run(str(computer_use), "trace", "--help", cwd=probe)
    _run(
        str(computer_use),
        "benchmark",
        "action-batch",
        "--mock-local",
        "--iterations",
        "1",
        cwd=probe,
    )

    daemon_env = os.environ.copy()
    daemon_env.update(
        {
            "COMPUTER_USE_BACKEND": "mock",
            "COMPUTER_USE_LOCAL_TOKEN": "dev",
            "COMPUTER_USE_REQUIRE_CONNECT_USER": "false",
            "COMPUTER_USE_ARTIFACTS_DIR": str(probe / "daemon-artifacts"),
            "COMPUTER_USE_RECORDINGS_DIR": str(probe / "daemon-recordings"),
            "COMPUTER_USE_TRACE_DIR": str(probe / "daemon-traces"),
        }
    )
    daemon = subprocess.Popen(  # noqa: S603
        [str(_installed_script(venv, "computer-use-daemon"))],
        cwd=probe,
        env=daemon_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _run(
            str(installed_python),
            "-c",
            textwrap.dedent(
                """
                import time

                import httpx

                headers = {"Authorization": "Bearer dev"}
                for _ in range(60):
                    try:
                        health = httpx.get("http://127.0.0.1:8080/healthz", timeout=1)
                        ready = httpx.get("http://127.0.0.1:8080/readyz", timeout=1)
                        assert health.status_code == 200
                        assert ready.status_code == 200
                        assert httpx.get(
                            "http://127.0.0.1:8080/v1/version", headers=headers, timeout=1
                        ).status_code == 200
                        assert httpx.get(
                            "http://127.0.0.1:8080/v1/capabilities", headers=headers, timeout=1
                        ).status_code == 200
                        break
                    except Exception:
                        time.sleep(0.25)
                else:
                    raise RuntimeError("installed daemon did not become ready")
                """
            ),
            cwd=probe,
        )
    finally:
        if daemon.poll() is None:
            os.killpg(daemon.pid, signal.SIGTERM)
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(daemon.pid, signal.SIGKILL)
                daemon.wait(timeout=5)


def _validate_sdist(sdist: Path, *, root: Path, python: str) -> None:
    venv = root / "sdist-venv"
    installed_python = _create_venv(venv, python=python)
    _run("uv", "pip", "install", "--python", str(installed_python), str(sdist))
    probe = root / "sdist-probe"
    probe.mkdir()
    _run(
        str(installed_python),
        "-c",
        textwrap.dedent(
            """
            import sys
            from importlib.metadata import version
            from pathlib import Path

            import modal_computer_use

            module_path = Path(modal_computer_use.__file__).resolve()
            assert module_path.is_relative_to(Path(sys.prefix).resolve())
            assert version("modal-computer-use") == modal_computer_use.__version__
            """
        ),
        cwd=probe,
    )


def smoke_distributions(directory: Path, *, python: str = "3.12") -> None:
    wheel, sdist = select_distributions(directory)
    with tempfile.TemporaryDirectory(prefix="modal-computer-use-release-") as temporary:
        root = Path(temporary)
        _validate_wheel(wheel.resolve(), root=root, python=python)
        _validate_sdist(sdist.resolve(), root=root, python=python)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install and smoke-test the built wheel and source distribution."
    )
    parser.add_argument("--distributions", required=True, type=Path)
    parser.add_argument("--python", default="3.12")
    args = parser.parse_args()
    smoke_distributions(args.distributions, python=args.python)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
