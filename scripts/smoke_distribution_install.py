from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import tempfile
import textwrap
from pathlib import Path


def clean_probe_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment without checkout paths or provider credentials."""
    environment = dict(os.environ if source is None else source)
    for name in tuple(environment):
        if name in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"} or name.startswith(
            ("MODAL_", "OPENAI_", "ANTHROPIC_")
        ):
            environment.pop(name, None)
    return environment


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


def installed_daemon_protocol_probe(*, port: int = 8080) -> str:
    """Return the public daemon protocol probe used by clean distributions."""
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("daemon probe port must be between 1 and 65535")
    return textwrap.dedent(
        """
        import hashlib
        import json
        import time
        from datetime import datetime

        import httpx

        base_url = "http://127.0.0.1:__DAEMON_PORT__"
        auth_headers = {"Authorization": "Bearer dev"}

        def expect(response, status):
            if response.status_code != status:
                raise RuntimeError(
                    f"installed daemon protocol check failed with status {response.status_code}"
                )

        with httpx.Client(base_url=base_url, headers=auth_headers, timeout=2.0) as client:
            for _ in range(60):
                try:
                    health = client.get("/healthz")
                    ready = client.get("/readyz")
                    version = client.get("/v1/version")
                    capabilities = client.get("/v1/capabilities")
                    if (
                        health.status_code == 200
                        and ready.status_code == 200
                        and version.status_code == 200
                        and capabilities.status_code == 200
                        and ready.json().get("ready") is True
                    ):
                        break
                except Exception:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError("installed daemon did not become ready")

            lease_response = client.post(
                "/v1/leases/acquire",
                json={"run_id": "distribution-probe-run"},
            )
            expect(lease_response, 200)
            lease = lease_response.json()
            lease_headers = {
                **auth_headers,
                "x-computer-use-lease-id": lease["lease_id"],
                "x-computer-use-lease-epoch": lease["daemon_epoch"],
                "x-computer-use-lease-fence": str(lease["fence"]),
                "x-computer-use-lease-token": lease_response.headers[
                    "x-computer-use-lease-token"
                ],
            }
            try:
                screenshot_response = client.post(
                    "/v1/screenshots/full/raw",
                    json={"format": "png", "show_cursor": False, "storage": "inline"},
                    headers=lease_headers,
                )
                expect(screenshot_response, 200)
                if screenshot_response.headers.get("content-type") != "image/png":
                    raise RuntimeError("installed daemon returned an unexpected screenshot type")
                screenshot_bytes = screenshot_response.content
                screenshot_headers = screenshot_response.headers
                if not screenshot_bytes:
                    raise RuntimeError("installed daemon returned an empty screenshot")
                if int(screenshot_headers["x-computer-use-width"]) < 1:
                    raise RuntimeError("installed daemon returned invalid screenshot width")
                if int(screenshot_headers["x-computer-use-height"]) < 1:
                    raise RuntimeError("installed daemon returned invalid screenshot height")
                if int(screenshot_headers["x-computer-use-size-bytes"]) != len(screenshot_bytes):
                    raise RuntimeError("installed daemon returned invalid screenshot size")
                if screenshot_headers["x-computer-use-sha256"] != hashlib.sha256(
                    screenshot_bytes
                ).hexdigest():
                    raise RuntimeError("installed daemon returned invalid screenshot digest")
                datetime.fromisoformat(
                    screenshot_headers["x-computer-use-captured-at"].replace("Z", "+00:00")
                )
                if not screenshot_headers["x-computer-use-capture-backend"]:
                    raise RuntimeError("installed daemon omitted screenshot backend metadata")
                if screenshot_headers["x-computer-use-cursor-visible"] != "false":
                    raise RuntimeError("installed daemon returned cursor-visible screenshot")
                if not isinstance(
                    json.loads(screenshot_headers["x-computer-use-coordinate-space"]), dict
                ):
                    raise RuntimeError("installed daemon returned invalid coordinate metadata")
                if not isinstance(
                    json.loads(screenshot_headers["x-computer-use-timing-ms"]), dict
                ):
                    raise RuntimeError("installed daemon returned invalid timing metadata")
                json.loads(screenshot_headers["x-computer-use-cursor-position"])

                action_response = client.post(
                    "/v1/actions/run",
                    json={
                        "actions": [
                            {"type": "move", "x": 10, "y": 20},
                            {"type": "click", "x": 10, "y": 20},
                        ]
                    },
                    headers={
                        **lease_headers,
                        "x-computer-use-operation-sequence": "0",
                    },
                )
                expect(action_response, 200)
                action_result = action_response.json()
                results = action_result.get("results")
                if action_result.get("ok") is not True or not isinstance(results, list):
                    raise RuntimeError("installed daemon returned an invalid action batch")
                if [item.get("index") for item in results] != [0, 1]:
                    raise RuntimeError("installed daemon reordered the action batch")
                if [item.get("type") for item in results] != ["move", "click"]:
                    raise RuntimeError("installed daemon changed the action batch")
                if not all(item.get("ok") is True for item in results):
                    raise RuntimeError("installed daemon failed an action batch item")

                receipt_response = client.post(
                    "/v1/receipts/status",
                    json={"run_id": "distribution-probe-run", "sequence": 0},
                    headers=lease_headers,
                )
                expect(receipt_response, 200)
                if receipt_response.json().get("state") != "COMPLETED":
                    raise RuntimeError("installed daemon did not complete the action receipt")
            finally:
                release_response = client.post("/v1/leases/release", headers=lease_headers)
                expect(release_response, 200)
                if release_response.json().get("state") != "released":
                    raise RuntimeError("installed daemon did not release the lease")
        """
    ).replace("__DAEMON_PORT__", str(port))


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _validate_installed_daemon(
    installed_python: Path,
    *,
    daemon_script: Path,
    probe: Path,
    probe_env: dict[str, str],
) -> None:
    port = _unused_loopback_port()
    daemon_env = dict(probe_env)
    daemon_env.update(
        {
            "COMPUTER_USE_BACKEND": "mock",
            "COMPUTER_USE_LOCAL_TOKEN": "dev",
            "COMPUTER_USE_REQUIRE_CONNECT_USER": "false",
            "COMPUTER_USE_ARTIFACTS_DIR": str(probe / "daemon-artifacts"),
            "COMPUTER_USE_RECORDINGS_DIR": str(probe / "daemon-recordings"),
            "COMPUTER_USE_RUNTIME_DIR": str(probe / "daemon-runtime"),
            "COMPUTER_USE_TRACE_DIR": str(probe / "daemon-traces"),
            "COMPUTER_USE_DAEMON_PORT": str(port),
        }
    )
    daemon = subprocess.Popen(  # noqa: S603
        [str(daemon_script)],
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
            installed_daemon_protocol_probe(port=port),
            cwd=probe,
            env=probe_env,
        )
    finally:
        if daemon.poll() is None:
            os.killpg(daemon.pid, signal.SIGTERM)
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(daemon.pid, signal.SIGKILL)
                daemon.wait(timeout=5)


def _validate_wheel(wheel: Path, *, root: Path, python: str) -> None:
    venv = root / "wheel-venv"
    installed_python = _create_venv(venv, python=python)
    _run("uv", "pip", "install", "--python", str(installed_python), str(wheel))

    probe = root / "probe"
    probe.mkdir()
    probe_env = clean_probe_environment()
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
        env=probe_env,
    )

    computer_use = _installed_script(venv, "computer-use")
    _run(str(computer_use), "--help", cwd=probe, env=probe_env)
    _run(str(computer_use), "trace", "--help", cwd=probe, env=probe_env)
    _run(
        str(computer_use),
        "benchmark",
        "action-batch",
        "--mock-local",
        "--iterations",
        "1",
        cwd=probe,
        env=probe_env,
    )

    _validate_installed_daemon(
        installed_python,
        daemon_script=_installed_script(venv, "computer-use-daemon"),
        probe=probe,
        probe_env=probe_env,
    )


def _validate_sdist(sdist: Path, *, root: Path, python: str) -> None:
    venv = root / "sdist-venv"
    installed_python = _create_venv(venv, python=python)
    _run("uv", "pip", "install", "--python", str(installed_python), str(sdist))
    probe = root / "sdist-probe"
    probe.mkdir()
    probe_env = clean_probe_environment()
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
        env=probe_env,
    )
    _validate_installed_daemon(
        installed_python,
        daemon_script=_installed_script(venv, "computer-use-daemon"),
        probe=probe,
        probe_env=probe_env,
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
