from __future__ import annotations

import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = runpy.run_path(str(ROOT / "scripts" / "smoke_distribution_install.py"))
select_distributions = SCRIPT["select_distributions"]
clean_probe_environment = SCRIPT["clean_probe_environment"]
validate_sdist = SCRIPT["_validate_sdist"]
installed_daemon_protocol_probe = SCRIPT["installed_daemon_protocol_probe"]


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
