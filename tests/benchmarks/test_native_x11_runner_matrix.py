from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPT_PATH = Path("scripts/benchmarks/native_x11_runner_matrix.py")
SPEC = importlib.util.spec_from_file_location("native_x11_runner_matrix", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MATRIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MATRIX)

SOURCE = {
    "git_revision": "a" * 40,
    "git_worktree_clean": True,
    "git_branch": "docs/article-evidence-provenance",
}


def test_plan_is_deterministic_complete_and_assembly_ready(tmp_path: Path) -> None:
    first = MATRIX.build_plan(output_root=tmp_path, order_seed=20260802, source=SOURCE)
    second = MATRIX.build_plan(output_root=tmp_path, order_seed=20260802, source=SOURCE)

    assert first == second
    assert first["source"]["runner"] == {
        "name": SCRIPT_PATH.name,
        "path": SCRIPT_PATH.as_posix(),
        "sha256": hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest(),
    }
    assert first["controls"] == {
        "blocks": 3,
        "daemon_http_version": "1.1",
        "fresh_sandbox_per_cell": True,
        "input_rate_limit_per_sec": 0,
        "iterations_per_cell": 30,
        "modal_cpu": 4.0,
        "modal_ingress": "connect",
        "modal_memory_mib": 8192,
        "modal_region": "us-west-2",
        "resource_profile": "standard",
        "warmup_iterations_per_cell": 1,
    }
    assert first["assembly"]["expected_blocks"] == 3
    assert first["assembly"]["expected_cells"] == 12
    assert len(first["assembly"]["raw_artifacts"]) == 12
    assert [item["sequence"] for item in first["schedule"]] == list(range(1, 13))
    for block in range(1, 4):
        cells = [
            (item["input_backend"], item["subprocess_backend"])
            for item in first["schedule"]
            if item["block"] == block
        ]
        assert set(cells) == set(MATRIX.CELLS)


def test_plan_command_never_creates_resources_or_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_root = tmp_path / "planned-output"
    monkeypatch.setattr(MATRIX, "_source_state", lambda: SOURCE)
    monkeypatch.setattr(
        MATRIX,
        "execute_matrix",
        lambda *_args, **_kwargs: pytest.fail("plan mode executed the matrix"),
    )

    assert MATRIX.main(["plan", "--output-root", str(output_root)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "planned"
    assert payload["order_seed"] == 20260802
    assert not output_root.exists()


def test_run_command_rejects_dirty_worktree_before_creating_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dirty_source = {**SOURCE, "git_worktree_clean": False}
    output_root = tmp_path / "dirty-run-output"
    monkeypatch.setattr(MATRIX, "_source_state", lambda: dirty_source)
    monkeypatch.setattr(
        MATRIX,
        "execute_matrix",
        lambda *_args, **_kwargs: pytest.fail("dirty run executed the matrix"),
    )

    with pytest.raises(RuntimeError, match="requires a clean Git worktree"):
        MATRIX.main(["run", "--output-root", str(output_root)])

    assert not output_root.exists()


def test_execute_matrix_uses_fresh_sandboxes_probes_versions_and_writes_raw_artifacts(
    tmp_path: Path,
) -> None:
    computers: list[_FakeComputer] = []
    benchmark_calls: list[dict[str, Any]] = []

    def create_computer(**kwargs: Any) -> _FakeComputer:
        computer = _FakeComputer(len(computers) + 1)
        computer.create_kwargs = kwargs
        computers.append(computer)
        return computer

    def benchmark_runner(**kwargs: Any) -> dict[str, Any]:
        benchmark_calls.append(kwargs)
        return {
            "ok": True,
            "metadata": {"environment": dict(kwargs["environment_metadata"])},
            "surfaces": {
                "daemon-http": {
                    "status": "ok",
                    "cases": {
                        "move_click": {
                            "samples_ms": [1.0] * 30,
                            "daemon_samples_ms": [0.5] * 30,
                        }
                    },
                }
            },
        }

    run_ids = iter(f"run-{index:02d}" for index in range(1, 13))
    plan = MATRIX.build_plan(output_root=tmp_path, order_seed=17, source=SOURCE)
    manifest_path = MATRIX.execute_matrix(
        plan,
        output_root=tmp_path,
        computer_factory=create_computer,
        benchmark_runner=benchmark_runner,
        run_id_factory=lambda: next(run_ids),
    )

    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "complete"
    assert len(computers) == 12
    assert len({id(computer) for computer in computers}) == 12
    assert len(benchmark_calls) == 12
    assert all(computer.terminated == [True] for computer in computers)
    assert all(computer.client.closed is True for computer in computers)
    assert all(computer.detach_calls == 0 for computer in computers)
    for computer, scheduled in zip(computers, plan["schedule"], strict=True):
        create = computer.create_kwargs
        config = create["config"]
        assert create["app_name"] == MATRIX.APP_NAME
        assert create["wait"] is True
        assert config.ingress == "connect"
        assert config.network.daemon_http_version == "1.1"
        assert config.runtime.modal_region == "us-west-2"
        assert config.resources.cpu == 4.0
        assert config.resources.memory_mib == 8192
        assert config.actions.input_rate_limit_per_sec == 0
        assert config.actions.input_backend == scheduled["input_backend"]
        assert config.actions.subprocess_backend == scheduled["subprocess_backend"]
    assert all(call["iterations"] == 30 for call in benchmark_calls)
    assert all(call["warmup_iterations"] == 1 for call in benchmark_calls)
    assert all(call["surfaces"] == ["daemon-http"] for call in benchmark_calls)
    assert all(cell["status"] == "complete" for cell in manifest["cells"])

    first_path = tmp_path / manifest["cells"][0]["raw_artifact"]
    first = json.loads(first_path.read_text())
    environment = first["metadata"]["environment"]
    assert environment["target_runtime_versions"] == {
        "debian_packages": {
            "libx11-6:amd64": "2:1.8.4-2+deb12u2",
            "libxtst6:amd64": "2:1.2.3-1.1",
            "xdotool": "1:3.20160805.1-5",
            "xvfb": "2:21.1.7-3+deb12u10",
        },
        "os_release": {
            "ID": "debian",
            "PRETTY_NAME": "Debian GNU/Linux 12 (bookworm)",
            "VERSION_ID": "12",
        },
        "python_packages": {
            "modal-computer-use": None,
            "python": "3.12.11",
            "uvicorn": "0.46.0",
            "uvloop": "0.22.1",
        },
        "xdotool": "xdotool version 3.20160805.1",
    }
    assert environment["cleanup"] == {"attempted": True, "errors": [], "succeeded": True}
    assert environment["matrix_runner"] == manifest["source"]["runner"]
    assert environment["matrix_cell"]["cell_id"] == manifest["cells"][0]["cell_id"]
    assert "token" not in json.dumps(first).lower()
    assert "environment_variables" not in json.dumps(first).lower()


def test_cell_terminates_and_closes_client_when_benchmark_raises() -> None:
    computer = _FakeComputer(1)
    plan = MATRIX.build_plan(output_root=Path("unused"), order_seed=7, source=SOURCE)
    cell = plan["schedule"][0]

    with pytest.raises(ZeroDivisionError):
        MATRIX._execute_cell(
            cell,
            source=plan["source"],
            run_id="run-failure",
            computer_factory=lambda **_kwargs: computer,
            benchmark_runner=lambda **_kwargs: 1 / 0,
        )

    assert computer.terminated == [True]
    assert computer.client.closed is True


def test_termination_failure_closes_client_and_fails_matrix(tmp_path: Path) -> None:
    computer = _FakeComputer(1, terminate_error=RuntimeError("termination failed"))
    plan = MATRIX.build_plan(output_root=tmp_path, order_seed=7, source=SOURCE)

    with pytest.raises(RuntimeError, match="matrix cell cleanup failed"):
        MATRIX.execute_matrix(
            plan,
            output_root=tmp_path,
            computer_factory=lambda **_kwargs: computer,
            benchmark_runner=lambda **_kwargs: {
                "ok": True,
                "metadata": {"environment": {}},
            },
            run_id_factory=lambda: "run-cleanup-failure",
        )

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert computer.cleanup_events == ["terminate", "client_close"]
    assert computer.client.closed is True
    assert computer.detach_calls == 0
    assert manifest["status"] == "failed"
    assert manifest["cells"][0]["status"] == "failed"
    assert manifest["cells"][0]["error_type"] == "RuntimeError"
    assert all(cell["status"] == "pending" for cell in manifest["cells"][1:])


def test_runtime_probe_rejects_empty_command_output() -> None:
    computer = SimpleNamespace(
        commands=SimpleNamespace(
            run=lambda *_command, **_kwargs: SimpleNamespace(
                ok=True,
                output={"stdout": "  \n"},
            )
        )
    )

    with pytest.raises(RuntimeError, match="returned no stdout"):
        MATRIX._command_stdout(computer, "python3", "--version")


class _FakeComputer:
    def __init__(self, index: int, *, terminate_error: Exception | None = None) -> None:
        self.index = index
        self.cleanup_events: list[str] = []
        self.client = SimpleNamespace(
            base_url="https://connect.modal.run/safe",
            closed=False,
            close=self._close_client,
        )
        self.commands = _FakeCommands()
        self.detach_calls = 0
        self.terminated: list[bool] = []
        self.terminate_error = terminate_error
        self.create_kwargs: dict[str, Any] = {}

    def first_valid_frame(self, _config: Any) -> bytes:
        return b"safe-frame"

    def metadata(self) -> SimpleNamespace:
        return SimpleNamespace(sandbox_id=f"sb-{self.index:02d}")

    def runtime_placement(self) -> dict[str, str]:
        return {"cloud": "aws", "region": "us-west-2"}

    def terminate(self, *, wait: bool = False) -> None:
        self.cleanup_events.append("terminate")
        self.terminated.append(wait)
        if self.terminate_error is not None:
            raise self.terminate_error

    def detach(self) -> None:
        self.detach_calls += 1
        self.cleanup_events.append("detach")

    def _close_client(self) -> None:
        self.cleanup_events.append("client_close")
        self.client.closed = True


class _FakeCommands:
    def run(self, *command: str, timeout: float) -> SimpleNamespace:
        assert timeout == 30
        if command[:2] == ("python3", "-c") and "importlib.metadata" in command[2]:
            stdout = json.dumps(
                {
                    "modal-computer-use": None,
                    "python": "3.12.11",
                    "uvicorn": "0.46.0",
                    "uvloop": "0.22.1",
                }
            )
        elif command[:2] == ("python3", "-c"):
            stdout = json.dumps(
                {
                    "ID": "debian",
                    "VERSION_ID": "12",
                    "PRETTY_NAME": "Debian GNU/Linux 12 (bookworm)",
                }
            )
        elif command[0] == "dpkg-query":
            stdout = (
                "xdotool\t1:3.20160805.1-5\n"
                "xvfb\t2:21.1.7-3+deb12u10\n"
                "libxtst6:amd64\t2:1.2.3-1.1\n"
                "libx11-6:amd64\t2:1.8.4-2+deb12u2\n"
            )
        elif command == ("xdotool", "version"):
            stdout = "xdotool version 3.20160805.1\n"
        else:
            raise AssertionError(f"unexpected probe: {command}")
        return SimpleNamespace(ok=True, output={"stdout": stdout})
