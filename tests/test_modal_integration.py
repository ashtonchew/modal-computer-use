from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _has_modal_auth() -> bool:
    if os.getenv("MODAL_TOKEN_ID") and os.getenv("MODAL_TOKEN_SECRET"):
        return True
    config_path = Path(os.getenv("MODAL_CONFIG_PATH", "~/.modal.toml")).expanduser()
    if not config_path.is_file():
        return False
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    profiles = [config]
    if isinstance(config.get("profile"), dict):
        profiles.extend(item for item in config["profile"].values() if isinstance(item, dict))
    if any(profile.get("token_id") and profile.get("token_secret") for profile in profiles):
        return True
    try:
        from modal.config import config as modal_config

        return bool(modal_config.get("token_id") and modal_config.get("token_secret"))
    except Exception:
        return False


def _skip_without_modal_auth() -> None:
    if importlib.util.find_spec("modal") is None or not _has_modal_auth():
        pytest.skip("Modal SDK or credentials are not configured")


@pytest.mark.modal
def test_modal_smoke_skipped_without_credentials() -> None:
    _skip_without_modal_auth()
    from modal_computer_use import ComputerConfig, ComputerSandbox
    from modal_computer_use.models import Point

    computer = ComputerSandbox.create(config=ComputerConfig())
    try:
        assert computer.status().ready is True
        actions = computer.actions.run(
            [
                {"type": "move", "x": 24, "y": 25},
                {"type": "cursor_position"},
                {
                    "type": "drag",
                    "path": [{"x": 24, "y": 25}, {"x": 30, "y": 35}],
                    "button": "left",
                    "duration_ms": 0,
                },
                {"type": "scroll", "direction": "down", "amount": 1, "x": 30, "y": 35},
                {
                    "type": "hold_key",
                    "key": "shift",
                    "actions": [{"type": "move", "x": 40, "y": 45}],
                },
                {"type": "keypress", "key": "Escape"},
                {"type": "release_all"},
            ],
            continue_on_error=False,
        )
        assert actions.ok is True
        assert computer.mouse.position() == Point(x=40, y=45)
    finally:
        computer.terminate()
        computer.detach()


@pytest.mark.modal
def test_modal_novnc_view_only_smoke() -> None:
    _skip_without_modal_auth()
    if os.getenv("MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE") != "1":
        pytest.skip("Set MODAL_COMPUTER_USE_RUN_NOVNC_SMOKE=1 to run noVNC smoke")

    from modal_computer_use import ComputerConfig, ComputerSandbox
    from modal_computer_use.config import RuntimeConfig

    computer = ComputerSandbox.create(
        config=ComputerConfig(
            expose_vnc="view_only",
            runtime=RuntimeConfig(timeout_seconds=300, idle_timeout_seconds=120),
        ),
        tags={"computer-use.smoke": "novnc-view-only"},
    )
    try:
        computer.wait_until_ready(timeout=120)
        caps = computer.client.get_json("/v1/capabilities")
        x11vnc = computer.processes.status("x11vnc")
        novnc = computer.processes.status("novnc")

        assert caps["vnc_enabled"] is True
        assert x11vnc.status == "running"
        assert novnc.status == "running"
        command = computer.commands.run("pgrep", "-af", "x11vnc")
        assert command.ok is True
        argv = command.output["stdout"]
        assert "-passwdfile" in argv
        assert "-nopw" not in argv
        assert "-viewonly" in argv
    finally:
        computer.terminate()
        computer.detach()


def _skip_without_v1_smoke() -> None:
    if os.getenv("MODAL_COMPUTER_USE_RUN_V1_SMOKE") != "1":
        pytest.skip("Set MODAL_COMPUTER_USE_RUN_V1_SMOKE=1 to run protected v1 Modal smoke")


def _skip_without_handoff_smoke() -> None:
    if os.getenv("MODAL_COMPUTER_USE_RUN_HANDOFF_SMOKE") != "1":
        pytest.skip(
            "Set MODAL_COMPUTER_USE_RUN_HANDOFF_SMOKE=1 to run protected handoff smoke"
        )


def _skip_without_clipboard_smoke() -> None:
    if os.getenv("MODAL_COMPUTER_USE_RUN_X11_CLIPBOARD_SMOKE") != "1":
        pytest.skip(
            "Set MODAL_COMPUTER_USE_RUN_X11_CLIPBOARD_SMOKE=1 to run the X11 clipboard smoke"
        )


def _required_handoff_setting(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise RuntimeError(f"protected handoff smoke requires {name}")
    return value


@pytest.mark.modal
def test_modal_deployed_function_session_handoff_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_handoff_smoke()

    import modal

    from modal_computer_use import (
        ComputerConfig,
        ComputerSandbox,
        ComputerSessionHandle,
    )
    from modal_computer_use.config import RuntimeConfig

    app_name = _required_handoff_setting("MODAL_COMPUTER_USE_HANDOFF_APP_NAME")
    modal_environment = _required_handoff_setting(
        "MODAL_COMPUTER_USE_HANDOFF_ENVIRONMENT"
    )
    function_region = _required_handoff_setting(
        "MODAL_COMPUTER_USE_HANDOFF_REGION"
    )
    owner = _required_handoff_setting("MODAL_COMPUTER_USE_HANDOFF_OWNER")
    trajectory_run_id = f"handoff-trajectory-{uuid.uuid4().hex}"
    safe_result_fields = {
        "borrow_succeeded",
        "screenshot_succeeded",
        "action_succeeded",
        "width",
        "height",
        "function_cloud",
        "function_region",
    }
    computer = None
    owner_terminated = False

    try:
        computer = ComputerSandbox.create(
            config=ComputerConfig(
                run_id=f"handoff-target-{uuid.uuid4().hex}",
                ingress="attested-tunnel",
                expose_vnc="off",
                runtime=RuntimeConfig(
                    modal_environment=modal_environment,
                    modal_region=function_region,
                    timeout_seconds=600,
                    idle_timeout_seconds=180,
                    readiness_timeout_seconds=180,
                ),
            ),
            app_name=app_name,
            owner=owner,
            tags={"computer-use.smoke": "function-session-handoff"},
        )
        target_placement = computer.runtime_placement()
        assert isinstance(target_placement["cloud"], str)
        assert target_placement["cloud"]
        assert isinstance(target_placement["region"], str)
        assert target_placement["region"]
        assert target_placement["region"] == function_region

        handle = ComputerSessionHandle.model_validate_json(
            computer.session_handle().model_dump_json()
        )
        deployed = modal.Function.from_name(
            app_name,
            "run_handoff_smoke",
            environment_name=modal_environment,
        )
        result = deployed.remote(handle, trajectory_run_id)

        assert isinstance(result, dict)
        assert set(result) == safe_result_fields
        assert result["borrow_succeeded"] is True
        assert result["screenshot_succeeded"] is True
        assert result["action_succeeded"] is True
        assert isinstance(result["width"], int) and result["width"] > 0
        assert isinstance(result["height"], int) and result["height"] > 0
        assert isinstance(result["function_cloud"], str)
        assert result["function_cloud"]
        assert result["function_cloud"] == target_placement["cloud"]
        assert isinstance(result["function_region"], str)
        assert result["function_region"]
        assert result["function_region"] == target_placement["region"]

        lease_status = computer.client.get_json("/v1/leases/status")
        assert lease_status.get("state") == "released"
        assert computer.poll() is None
        assert computer.status().ready is True

        computer.terminate(wait=True)
        owner_terminated = True
        assert computer.poll() is not None
    finally:
        if computer is not None:
            if not owner_terminated:
                computer.terminate(wait=True)
            computer.detach()


@pytest.mark.modal
def test_modal_release_image_x11_clipboard_ownership_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_clipboard_smoke()

    from modal_computer_use import ComputerConfig, ComputerSandbox
    from modal_computer_use.config import RuntimeConfig

    exact_region = _required_handoff_setting("MODAL_COMPUTER_USE_HANDOFF_REGION")
    modal_environment = _required_handoff_setting(
        "MODAL_COMPUTER_USE_HANDOFF_ENVIRONMENT"
    )
    suffix = uuid.uuid4().hex[:10]
    first = "first-clipboard-owner-" + ("a" * 4096)
    replacement = "replacement-clipboard-owner-" + ("b" * 4096)
    computer = ComputerSandbox.create(
        config=ComputerConfig(
            ingress="attested-tunnel",
            expose_vnc="off",
            runtime=RuntimeConfig(
                modal_environment=modal_environment,
                modal_region=exact_region,
                timeout_seconds=300,
                idle_timeout_seconds=120,
                readiness_timeout_seconds=180,
            ),
        ),
        name=f"mcu-clipboard-smoke-{suffix}",
        owner=f"mcu-clipboard-smoke-owner-{suffix}",
        tags={"computer-use.smoke": "x11-clipboard-ownership"},
    )
    try:
        previous = computer.clipboard.get_text()
        assert computer.clipboard.set_text(first).ok is True
        assert computer.clipboard.get_text() == first
        assert computer.clipboard.set_text(replacement).ok is True
        assert computer.clipboard.get_text() == replacement
        assert computer.clipboard.set_text(previous).ok is True
        assert computer.clipboard.get_text() == previous
    finally:
        computer.terminate(wait=True)
        computer.detach()


@pytest.mark.modal
def test_modal_manager_attach_reuse_cleanup_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_v1_smoke()

    from modal_computer_use import ComputerConfig, ComputerSandbox, ComputerSandboxManager
    from modal_computer_use.errors import ConfigConflictError

    suffix = uuid.uuid4().hex[:10]
    run_id = f"mcu-v1-manager-{suffix}"
    name = f"mcu-v1-manager-{suffix}"
    owner = f"mcu-v1-owner-{suffix}"
    manager = ComputerSandboxManager()
    computer = None
    cleaned_up = False

    try:
        config = ComputerConfig(run_id=run_id)
        computer = manager.create(
            config=config,
            name=name,
            owner=owner,
            tags={"computer-use.smoke": "v1-manager"},
        )
        metadata = computer.metadata()
        assert metadata is not None
        assert metadata.sandbox_id

        listed = manager.list(owner=owner)
        assert any(ref.sandbox_id == metadata.sandbox_id for ref in listed)
        found = manager.find_by_run_id(run_id)
        assert found is not None
        assert found.sandbox_id == metadata.sandbox_id

        attached_by_id = ComputerSandbox.attach(sandbox_id=metadata.sandbox_id, wait=True)
        try:
            assert attached_by_id.status().ready is True
        finally:
            attached_by_id.detach()

        attached_by_run_id = ComputerSandbox.attach(run_id=run_id, wait=True)
        try:
            assert attached_by_run_id.status().ready is True
        finally:
            attached_by_run_id.detach()

        reused = manager.attach_or_create(name=name, config=config, wait=True)
        try:
            assert reused.metadata() is not None
            assert reused.metadata().sandbox_id == metadata.sandbox_id
        finally:
            reused.detach()

        mismatch = ComputerConfig(run_id=run_id)
        mismatch.desktop.resolution = (1280, 720)
        with pytest.raises(ConfigConflictError):
            manager.attach_or_create(name=name, config=mismatch, wait=False)

        dry_run = manager.cleanup_expired(
            ttl_seconds=1,
            owner=owner,
            dry_run=True,
            now=datetime.now(UTC) + timedelta(minutes=5),
        )
        assert any(item.sandbox_id == metadata.sandbox_id for item in dry_run.candidates)

        cleanup = manager.cleanup_expired(
            ttl_seconds=1,
            owner=owner,
            dry_run=False,
            now=datetime.now(UTC) + timedelta(minutes=5),
        )
        cleaned_up = any(item.sandbox_id == metadata.sandbox_id for item in cleanup.candidates)
        assert cleaned_up is True
    finally:
        if computer is not None:
            if not cleaned_up:
                computer.terminate()
            computer.detach()


@pytest.mark.modal
@pytest.mark.asyncio
async def test_modal_named_attach_or_create_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_v1_smoke()

    import modal

    from modal_computer_use import AsyncComputerSandbox, ComputerConfig
    from modal_computer_use.config import RuntimeConfig

    suffix = uuid.uuid4().hex[:10]
    name = f"mcu-v1-named-{suffix}"
    run_id = f"mcu-v1-named-{suffix}"
    owner = f"mcu-v1-owner-{suffix}"
    config = ComputerConfig(
        run_id=run_id,
        runtime=RuntimeConfig(
            timeout_seconds=300,
            idle_timeout_seconds=120,
            readiness_timeout_seconds=180,
        ),
    )
    create_options = {
        "name": name,
        "config": config,
        "owner": owner,
        "tags": {"computer-use.smoke": "named-attach-or-create"},
    }
    sandbox_id: str | None = None
    terminated = False

    try:
        async with AsyncComputerSandbox.attach_or_create(**create_options) as created:
            metadata = created.metadata()
            sandbox_id = metadata.sandbox_id
            assert metadata.name == name
            assert metadata.run_id == run_id
            assert metadata.owner == owner
            await created.detach()

        async with AsyncComputerSandbox.attach_or_create(**create_options) as reused:
            assert reused.metadata().sandbox_id == sandbox_id

        assert sandbox_id is not None
        async with AsyncComputerSandbox.attach(sandbox_id=sandbox_id) as attached:
            assert attached.metadata().sandbox_id == sandbox_id
            await attached.terminate(wait=True)
            terminated = True
    finally:
        if sandbox_id is not None and not terminated:
            with suppress(Exception):
                leftover = await modal.Sandbox.from_id.aio(sandbox_id)
                with suppress(Exception):
                    await leftover.terminate.aio(wait=True)
                with suppress(Exception):
                    await leftover.detach.aio()


@pytest.mark.modal
def test_modal_manager_cleanup_multiple_owned_sandboxes_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_v1_smoke()

    from modal_computer_use import ComputerConfig, ComputerSandboxManager

    suffix = uuid.uuid4().hex[:10]
    owner = f"mcu-v1-owner-scale-{suffix}"
    manager = ComputerSandboxManager()
    computers = []

    try:
        for index in range(2):
            computers.append(
                manager.create(
                    config=ComputerConfig(run_id=f"mcu-v1-cleanup-{suffix}-{index}"),
                    owner=owner,
                    tags={"computer-use.smoke": "v1-manager-cleanup"},
                    wait=False,
                )
            )

        refs = manager.list(owner=owner)
        sandbox_ids = {
            computer.metadata().sandbox_id for computer in computers if computer.metadata()
        }
        assert sandbox_ids
        assert sandbox_ids.issubset({ref.sandbox_id for ref in refs})

        future = datetime.now(UTC) + timedelta(minutes=5)
        dry_run = manager.cleanup_expired(
            ttl_seconds=1,
            owner=owner,
            dry_run=True,
            now=future,
        )
        assert sandbox_ids.issubset({item.sandbox_id for item in dry_run.candidates})
        assert dry_run.terminated_count == 0

        cleanup = manager.cleanup_expired(
            ttl_seconds=1,
            owner=owner,
            dry_run=False,
            now=future,
        )
        assert sandbox_ids.issubset({item.sandbox_id for item in cleanup.candidates})
        assert cleanup.terminated_count >= len(sandbox_ids)
    finally:
        for computer in computers:
            computer.detach()


@pytest.mark.modal
def test_modal_attach_by_id_from_separate_process_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_v1_smoke()

    from modal_computer_use import ComputerConfig, ComputerSandbox

    suffix = uuid.uuid4().hex[:10]
    computer = ComputerSandbox.create(
        config=ComputerConfig(run_id=f"mcu-v1-cross-process-{suffix}"),
        tags={"computer-use.smoke": "v1-cross-process-attach"},
    )
    try:
        metadata = computer.metadata()
        assert metadata is not None
        sandbox_id = metadata.sandbox_id
        code = """
from modal_computer_use import ComputerSandbox
import os

computer = ComputerSandbox.attach(sandbox_id=os.environ["MCU_ATTACH_SANDBOX_ID"], wait=True)
try:
    ready = computer.status().ready
finally:
    computer.detach()
raise SystemExit(0 if ready else 1)
"""
        env = os.environ.copy()
        env["MCU_ATTACH_SANDBOX_ID"] = sandbox_id
        result = subprocess.run(  # noqa: S603 - fixed interpreter, no shell.
            [sys.executable, "-c", code],
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        assert sandbox_id not in result.stdout
        assert sandbox_id not in result.stderr
    finally:
        computer.terminate()
        computer.detach()


@pytest.mark.modal
def test_modal_volume_artifact_sync_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_v1_smoke()

    import modal

    from modal_computer_use import ComputerConfig, ComputerSandbox
    from modal_computer_use.config import StorageConfig

    suffix = uuid.uuid4().hex[:10]
    volume_name = f"mcu-v1-artifacts-{suffix}"
    proof_path = f"proof/{suffix}.txt"
    proof = f"volume proof {suffix}\n".encode()
    from modal_proto import api_pb2

    volume = modal.Volume.from_name(
        volume_name,
        create_if_missing=True,
        version=api_pb2.VolumeFsVersion.Value("VOLUME_FS_VERSION_V2"),
    ).hydrate()
    computer = None
    reader = None

    try:
        reader = ComputerSandbox.create(
            config=ComputerConfig(run_id=f"mcu-v1-volume-reader-{suffix}"),
            volumes={"/home/desktop/artifacts": volume},
            tags={"computer-use.smoke": "v1-volume-reader"},
        )
        computer = ComputerSandbox.create(
            config=ComputerConfig(
                run_id=f"mcu-v1-volume-{suffix}",
                storage=StorageConfig(persist_artifacts=True),
            ),
            volumes={"/home/desktop/artifacts": volume},
            tags={"computer-use.smoke": "v1-volume"},
        )
        computer.artifacts.write_bytes(proof_path, proof, "text/plain")
        sync = computer.artifacts.sync()
        assert sync.ok is True
        assert sync.persistent is True
        assert sync.synced_paths == ["artifact-root"]
        assert "v2" in (sync.message or "")
        reader.reload_volumes(timeout=55)
        assert reader.artifacts.read_bytes(proof_path) == proof
        computer.terminate()
        computer.detach()
        computer = None
        assert b"".join(volume.read_file(proof_path)) == proof
    finally:
        if computer is not None:
            computer.terminate()
            computer.detach()
        if reader is not None:
            reader.terminate()
            reader.detach()
        modal.Volume.objects.delete(volume_name, allow_missing=True)


@pytest.mark.modal
def test_modal_snapshot_directory_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_v1_smoke()

    from modal_computer_use import ComputerConfig, ComputerSandbox

    suffix = uuid.uuid4().hex[:10]
    marker_path = f"snapshots/{suffix}.txt"
    nested_path = f"snapshots/nested/{suffix}.txt"
    marker = f"snapshot marker {suffix}\n".encode()
    nested = f"nested snapshot marker {suffix}\n".encode()
    computer = None
    restored = None

    try:
        computer = ComputerSandbox.create(
            config=ComputerConfig(run_id=f"mcu-v1-snapshot-source-{suffix}"),
            tags={"computer-use.smoke": "v1-snapshot-source"},
        )
        computer.artifacts.write_bytes(marker_path, marker, "text/plain")
        computer.artifacts.write_bytes(nested_path, nested, "text/plain")
        snapshot_image = computer.snapshot_directory("/home/desktop/artifacts/snapshots")
        computer.terminate()
        computer.detach()
        computer = None

        restored = ComputerSandbox.create(
            config=ComputerConfig(run_id=f"mcu-v1-snapshot-restore-{suffix}"),
            tags={"computer-use.smoke": "v1-snapshot-restore"},
        )
        restored.mount_image("/home/desktop/artifacts/snapshots", snapshot_image)
        assert restored.artifacts.read_bytes(marker_path) == marker
        assert restored.artifacts.read_bytes(nested_path) == nested
    finally:
        if computer is not None:
            computer.terminate()
            computer.detach()
        if restored is not None:
            restored.terminate()
            restored.detach()


@pytest.mark.modal
def test_modal_browser_profile_open_url_screenshot_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_v1_smoke()

    from modal_computer_use import ComputerConfig, ComputerSandbox
    from modal_computer_use.config import BrowserConfig, ResourceConfig

    suffix = uuid.uuid4().hex[:10]
    computer = ComputerSandbox.create(
        config=ComputerConfig(
            run_id=f"mcu-v1-browser-{suffix}",
            browser=BrowserConfig(kind="chromium", prewarm=True),
            resources=ResourceConfig(profile="browser"),
        ),
        tags={"computer-use.smoke": "v1-browser-profile"},
    )
    try:
        opened = computer.browser.open_url("https://example.com")
        assert opened.ok is True
        screenshot = computer.screenshots.full(storage="inline")
        assert screenshot.width > 0
        assert screenshot.height > 0
        assert screenshot.size_bytes > 0
        assert screenshot.sha256
    finally:
        computer.terminate()
        computer.detach()


@pytest.mark.modal
def test_modal_named_image_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_v1_smoke()
    revision = os.getenv("MODAL_COMPUTER_USE_NAMED_IMAGE_REVISION")
    if not revision:
        pytest.skip("Set MODAL_COMPUTER_USE_NAMED_IMAGE_REVISION to test a published Image")

    from modal_computer_use import ComputerConfig, ComputerSandbox, ImageConfig

    computer = ComputerSandbox.create(
        config=ComputerConfig(
            run_id=f"mcu-v1-named-{uuid.uuid4().hex[:10]}",
            image=ImageConfig(source="named", revision=revision),
        ),
        tags={"computer-use.smoke": "v1-named-image"},
    )
    try:
        assert computer.status().ready is True
    finally:
        computer.terminate()
        computer.detach()


@pytest.mark.modal
def test_modal_action_rate_limit_live_smoke() -> None:
    _skip_without_modal_auth()
    _skip_without_v1_smoke()

    from modal_computer_use import ComputerConfig, ComputerSandbox
    from modal_computer_use.config import ActionConfig

    suffix = uuid.uuid4().hex[:10]
    computer = ComputerSandbox.create(
        config=ComputerConfig(
            run_id=f"mcu-v1-rate-limit-{suffix}",
            actions=ActionConfig(input_rate_limit_per_sec=1),
        ),
        tags={"computer-use.smoke": "v1-rate-limit"},
    )
    try:
        result = computer.actions.run(
            [
                {"type": "move", "x": 10, "y": 10},
                {"type": "move", "x": 20, "y": 20},
            ],
            continue_on_error=True,
        )
        assert result.ok is False
        assert [item.ok for item in result.results] == [True, False]
        assert result.results[1].error_code == "rate_limited"
        assert result.results[1].output["code"] == "rate_limited"
        assert "retry_after_seconds" in result.results[1].output
    finally:
        computer.terminate()
        computer.detach()
