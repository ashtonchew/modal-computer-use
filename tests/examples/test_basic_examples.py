from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_example(filename: str) -> ModuleType:
    path = ROOT / "examples" / filename
    spec = importlib.util.spec_from_file_location(f"basic_example_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_daemon_helper_execs_the_supported_entry_point() -> None:
    source = (ROOT / "scripts" / "run_local_daemon.sh").read_text(encoding="utf-8")

    assert "exec uv run computer-use-daemon" in source
    assert "python -m modal_computer_use.daemon" not in source


def test_local_input_example_is_import_safe_and_detaches(monkeypatch, capsys) -> None:
    example = _load_example("01_mouse_keyboard_screenshots.py")
    events: list[object] = []

    class FakeComputer:
        mouse = SimpleNamespace(
            move=lambda *args: events.append(("move", args)),
            click=lambda: events.append("click"),
        )
        keyboard = SimpleNamespace(type=lambda text: events.append(("type", text)))
        screenshots = SimpleNamespace(
            full=lambda **kwargs: (
                events.append(("screenshot", kwargs))
                or SimpleNamespace(width=1024, height=768, sha256="digest")
            )
        )

        def wait_until_ready(self) -> None:
            events.append("ready")

        def detach(self) -> None:
            events.append("detach")

    monkeypatch.setattr(
        example,
        "ComputerSandbox",
        SimpleNamespace(local=lambda **kwargs: events.append(("local", kwargs)) or FakeComputer()),
    )

    assert events == []
    example.main()

    assert events[0] == ("local", {"token": "dev"})
    assert events[-1] == "detach"
    assert "1024 768 digest" in capsys.readouterr().out


def test_local_input_example_detaches_after_action_failure(monkeypatch) -> None:
    example = _load_example("01_mouse_keyboard_screenshots.py")
    events: list[str] = []

    class FakeComputer:
        mouse = SimpleNamespace(move=lambda *_args: None, click=lambda: None)
        keyboard = SimpleNamespace(type=lambda _text: (_ for _ in ()).throw(RuntimeError("failed")))
        screenshots = SimpleNamespace(full=lambda **_kwargs: None)

        def wait_until_ready(self) -> None:
            events.append("ready")

        def detach(self) -> None:
            events.append("detach")

    monkeypatch.setattr(
        example,
        "ComputerSandbox",
        SimpleNamespace(local=lambda **_kwargs: FakeComputer()),
    )

    with pytest.raises(RuntimeError, match="failed"):
        example.main()

    assert events == ["ready", "detach"]


def test_attach_example_waits_and_only_detaches(monkeypatch, capsys) -> None:
    example = _load_example("attach_existing_sandbox.py")
    calls: list[dict[str, Any]] = []
    events: list[str] = []

    class AttachedComputer:
        def status(self) -> object:
            events.append("status")
            return SimpleNamespace(ready=True)

        def detach(self) -> None:
            events.append("detach")

    monkeypatch.setattr(
        example,
        "ComputerSandbox",
        SimpleNamespace(attach=lambda **kwargs: calls.append(kwargs) or AttachedComputer()),
    )

    example.main(["--sandbox-id", "sb-placeholder"])

    assert calls == [
        {
            "sandbox_id": "sb-placeholder",
            "app_name": "modal-computer-use",
            "modal_environment": None,
            "wait": True,
            "readiness_timeout": 120.0,
        }
    ]
    assert events == ["status", "detach"]
    assert capsys.readouterr().out.strip() == "{'ready': True}"


@pytest.mark.parametrize(
    "argv",
    [[], ["--sandbox-id", "sb-placeholder", "--run-id", "run-placeholder"]],
)
def test_attach_example_requires_exactly_one_selector(argv: list[str], monkeypatch) -> None:
    example = _load_example("attach_existing_sandbox.py")
    for name in (
        "MODAL_COMPUTER_USE_SANDBOX_ID",
        "MODAL_COMPUTER_USE_SANDBOX_NAME",
        "MODAL_COMPUTER_USE_RUN_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit):
        example.main(argv)


def test_volume_example_requires_verified_persistence_and_cleans_up(monkeypatch, capsys) -> None:
    example = _load_example("volume_artifacts.py")
    calls: dict[str, Any] = {}
    events: list[object] = []
    volume = object()

    class FakeComputer:
        artifacts = SimpleNamespace(
            write_bytes=lambda path, data, content_type: (
                events.append(("write", path, data, content_type))
                or SimpleNamespace(kind="file", content_type=content_type, size_bytes=len(data))
            ),
            sync=lambda: events.append("sync") or SimpleNamespace(ok=True, persistent=True),
        )

        def wait_until_ready(self) -> None:
            events.append("ready")

        def terminate(self, *, wait: bool = False) -> None:
            events.append(("terminate", wait))

        def detach(self) -> None:
            events.append("detach")

    def create(**kwargs: Any) -> FakeComputer:
        calls.update(kwargs)
        return FakeComputer()

    monkeypatch.setattr(
        example,
        "_modal_volume",
        lambda name, **kwargs: events.append(("volume", name, kwargs)) or volume,
    )
    monkeypatch.setattr(example, "ComputerSandbox", SimpleNamespace(create=create))

    example.main(["--volume-name", "artifact-volume"])

    config = calls["config"]
    assert config.storage.persist_artifacts is True
    assert config.run_id.startswith("volume-artifacts-")
    assert calls["volumes"] == {"/home/desktop/artifacts": volume}
    assert calls["wait"] is False
    write = next(event for event in events if isinstance(event, tuple) and event[0] == "write")
    assert write[1].startswith("runs/")
    assert events[-2:] == [("terminate", True), "detach"]
    output = capsys.readouterr().out
    assert "'sync_ok': True" in output
    assert "'persistent': True" in output
    assert "artifact-volume" not in output


def test_volume_example_fails_closed_and_cleans_up_on_unverified_sync(monkeypatch) -> None:
    example = _load_example("volume_artifacts.py")
    events: list[object] = []

    class FakeComputer:
        artifacts = SimpleNamespace(
            write_bytes=lambda *_args: SimpleNamespace(
                kind="file", content_type="text/plain", size_bytes=6
            ),
            sync=lambda: SimpleNamespace(ok=True, persistent=False),
        )

        def wait_until_ready(self) -> None:
            pass

        def terminate(self, *, wait: bool = False) -> None:
            events.append(("terminate", wait))

        def detach(self) -> None:
            events.append("detach")

    monkeypatch.setattr(example, "_modal_volume", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        example,
        "ComputerSandbox",
        SimpleNamespace(create=lambda **_kwargs: FakeComputer()),
    )

    with pytest.raises(RuntimeError, match="verified Modal Volume artifact sync"):
        example.main(["--volume-name", "artifact-volume"])

    assert events == [("terminate", True), "detach"]


def test_volume_example_cleans_up_when_readiness_fails(monkeypatch) -> None:
    example = _load_example("volume_artifacts.py")
    events: list[object] = []

    class FakeComputer:
        def wait_until_ready(self) -> None:
            raise RuntimeError("not ready")

        def terminate(self, *, wait: bool = False) -> None:
            events.append(("terminate", wait))

        def detach(self) -> None:
            events.append("detach")

    monkeypatch.setattr(example, "_modal_volume", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        example,
        "ComputerSandbox",
        SimpleNamespace(create=lambda **_kwargs: FakeComputer()),
    )

    with pytest.raises(RuntimeError, match="not ready"):
        example.main(["--volume-name", "artifact-volume"])

    assert events == [("terminate", True), "detach"]
