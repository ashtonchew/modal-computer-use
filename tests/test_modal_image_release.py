from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from modal_computer_use.errors import (
    ImageReleaseCanaryError,
    ImageReleaseConflictError,
    ImageReleaseIdentityMismatchError,
    ImageReleaseLockError,
    ImageReleaseManifestError,
    ImageReleaseNotFoundError,
)
from modal_computer_use.image import (
    ImageCanaryRecord,
    ImageReleaseRecord,
    ImageReleaseSpec,
    publish_image_release,
    resolve_release_image,
)

REVISION = "603cad0e4f2a3b4c5d6e7f8091a2b3c4d5e6f708"


def test_image_release_spec_uses_canonical_reference(tmp_path: Path) -> None:
    spec = ImageReleaseSpec(
        source_revision=REVISION,
        logical_release="2.0.0",
        image_variant="chromium",
        environment_name="prod",
        manifest_path=tmp_path / "modal-image-release.v1.json",
        expected_image_builder_version="2025.06",
    )

    assert spec.image_name == "modal-computer-use-chromium"
    assert spec.image_tag == REVISION
    assert spec.image_reference == f"modal-computer-use-chromium:{REVISION}"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"source_revision": "abc123"}, "full 40-character Git revision"),
        ({"logical_release": ""}, "logical_release must be non-empty"),
        ({"environment_name": "  "}, "environment_name must be non-empty"),
        (
            {"expected_image_builder_version": ""},
            "expected_image_builder_version must be non-empty",
        ),
    ],
)
def test_image_release_spec_rejects_ambiguous_inputs(
    tmp_path: Path,
    changes: dict[str, str],
    message: str,
) -> None:
    values = {
        "source_revision": REVISION,
        "logical_release": "2.0.0",
        "image_variant": "standard",
        "environment_name": "prod",
        "manifest_path": tmp_path / "modal-image-release.v1.json",
        "expected_image_builder_version": "2025.06",
        **changes,
    }

    with pytest.raises(ValueError, match=message):
        ImageReleaseSpec(**values)


def _release_record() -> ImageReleaseRecord:
    return ImageReleaseRecord(
        schema_version=1,
        logical_release="2.0.0",
        source_revision=REVISION,
        image_variant="standard",
        image_name="modal-computer-use-standard",
        image_tag=REVISION,
        image_reference=f"modal-computer-use-standard:{REVISION}",
        workspace_name="acme",
        environment_name="prod",
        modal_image_object_id="im-release-object",
        pyproject_sha256="1" * 64,
        uv_lock_sha256="2" * 64,
        image_builder_version="2025.06",
        uv_version="0.12.3",
        modal_sdk_version="1.5.3",
        build_app_name="modal-computer-use-image-builds",
        canary=ImageCanaryRecord(
            status="passed",
            checks=(
                "healthz",
                "readyz",
                "version",
                "capabilities",
                "image_object_id",
                "browser",
                "screenshot",
                "cleanup",
            ),
            checked_at="2026-08-08T19:00:00Z",
        ),
        published_at="2026-08-08T19:01:00Z",
    )


def test_image_release_record_round_trips_without_losing_identity() -> None:
    record = _release_record()

    restored = ImageReleaseRecord.from_dict(record.to_dict())

    assert restored == record
    assert restored.modal_image_object_id == "im-release-object"
    assert "digest" not in restored.to_dict()


def test_image_release_record_rejects_unknown_or_inconsistent_fields() -> None:
    payload = _release_record().to_dict()
    payload["unexpected"] = "value"
    with pytest.raises(ValueError, match="unexpected fields"):
        ImageReleaseRecord.from_dict(payload)

    payload = _release_record().to_dict()
    payload["image_reference"] = f"modal-computer-use-firefox:{REVISION}"
    with pytest.raises(ValueError, match="image_reference does not match"):
        ImageReleaseRecord.from_dict(payload)


def _spec(tmp_path: Path) -> ImageReleaseSpec:
    return ImageReleaseSpec(
        source_revision=REVISION,
        logical_release="2.0.0",
        image_variant="standard",
        environment_name="prod",
        manifest_path=tmp_path / "modal-image-release.v1.json",
        expected_image_builder_version="2025.06",
    )


class _BuiltImage:
    object_id = "im-release-object"

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def publish(self, reference: str, *, environment_name: str) -> None:
        self.events.append(f"publish:{reference}:{environment_name}")


class _Recipe:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def build(self, app: object) -> _BuiltImage:
        del app
        self.events.append("build")
        return _BuiltImage(self.events)


def _install_release_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str],
    assignments: list[dict[str, str]] | None = None,
) -> None:
    import modal_computer_use.image as image_module

    state = assignments or [{}, {f"modal-computer-use-standard:{REVISION}": "im-release-object"}]

    def listed(*, environment_name: str) -> dict[str, str]:
        events.append(f"list:{environment_name}")
        return state.pop(0) if len(state) > 1 else state[0]

    class FakeImage:
        @staticmethod
        def from_id(object_id: str) -> object:
            events.append(f"from_id:{object_id}")
            return SimpleNamespace(object_id=object_id)

    fake_modal = SimpleNamespace(
        App=SimpleNamespace(
            lookup=lambda *args, **kwargs: events.append("app_lookup") or object()
        ),
        Image=FakeImage,
        enable_output=lambda: __import__("contextlib").nullcontext(),
    )
    monkeypatch.setattr(image_module, "_modal", lambda: fake_modal)
    monkeypatch.setattr(image_module, "_verify_image_runtime_lock", lambda context: None)
    monkeypatch.setattr(
        image_module,
        "_named_image_recipe",
        lambda **kwargs: events.append(f"recipe:{kwargs['variant']}") or _Recipe(events),
    )
    monkeypatch.setattr(image_module, "_published_named_image_assignments", listed)
    monkeypatch.setattr(
        image_module,
        "_modal_release_context",
        lambda **kwargs: ("acme", "2025.06", "1.5.3"),
    )
    monkeypatch.setattr(
        image_module,
        "_run_image_release_canary",
        lambda image, spec: events.append(f"canary:{image.object_id}")
        or ImageCanaryRecord(
            status="passed",
            checks=(
                "healthz",
                "readyz",
                "version",
                "capabilities",
                "image_object_id",
                "browser",
                "screenshot",
                "cleanup",
            ),
            checked_at="2026-08-08T19:00:00Z",
        ),
    )
    monkeypatch.setattr(
        image_module,
        "_utc_now",
        lambda: "2026-08-08T19:01:00Z",
    )


def test_publish_image_release_builds_canaries_then_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modal_computer_use.image as image_module

    events: list[str] = []
    _install_release_fakes(monkeypatch, events=events)
    times = iter(("2026-08-08T19:01:00Z", "2026-08-08T19:02:00Z"))
    monkeypatch.setattr(image_module, "_utc_now", lambda: next(times))

    record = publish_image_release(_spec(tmp_path))

    assert record == ImageReleaseRecord.from_dict(
        __import__("json").loads(_spec(tmp_path).manifest_path.read_text())
    )
    assert record.published_at == "2026-08-08T19:02:00Z"
    assert events == [
        "list:prod",
        "app_lookup",
        "recipe:standard",
        "build",
        "from_id:im-release-object",
        "canary:im-release-object",
        f"publish:modal-computer-use-standard:{REVISION}:prod",
        "list:prod",
    ]


def test_publish_image_release_rejects_an_existing_reference_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    reference = f"modal-computer-use-standard:{REVISION}"
    _install_release_fakes(
        monkeypatch,
        events=events,
        assignments=[{reference: "im-other-object"}],
    )

    with pytest.raises(ImageReleaseConflictError, match="already exists"):
        publish_image_release(_spec(tmp_path))

    assert events == ["list:prod"]


def test_publish_image_release_is_idempotent_with_matching_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    spec = _spec(tmp_path)
    record = _release_record()
    spec.manifest_path.write_text(__import__("json").dumps(record.to_dict()))
    _install_release_fakes(
        monkeypatch,
        events=events,
        assignments=[{record.image_reference: record.modal_image_object_id}],
    )

    assert publish_image_release(spec) == record
    assert events == ["list:prod"]


def test_publish_image_release_keeps_recoverable_record_when_final_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modal_computer_use.image as image_module

    events: list[str] = []
    spec = _spec(tmp_path)
    _install_release_fakes(monkeypatch, events=events)
    real_replace = image_module.os.replace
    calls = 0

    def fail_final_replace(source: Any, target: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("no")
        real_replace(source, target)

    monkeypatch.setattr(image_module.os, "replace", fail_final_replace)

    with pytest.raises(ImageReleaseManifestError, match="could not write"):
        publish_image_release(spec)

    assert not spec.manifest_path.exists()
    assert spec.manifest_path.with_name(f".{spec.manifest_path.name}.pending").is_file()


def test_publish_image_release_removes_temporary_manifest_after_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modal_computer_use.image as image_module

    events: list[str] = []
    spec = _spec(tmp_path)
    _install_release_fakes(monkeypatch, events=events)
    monkeypatch.setattr(
        image_module.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("no")),
    )

    with pytest.raises(ImageReleaseManifestError, match="could not write"):
        publish_image_release(spec)

    assert not spec.manifest_path.exists()
    assert not spec.manifest_path.with_name(f".{spec.manifest_path.name}.pending").exists()
    assert list(tmp_path.glob(".*.tmp")) == []
    assert not any(item.startswith("publish:") for item in events)


def test_publish_image_release_fails_before_publish_when_canary_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modal_computer_use.image as image_module

    events: list[str] = []
    _install_release_fakes(monkeypatch, events=events)
    monkeypatch.setattr(
        image_module,
        "_run_image_release_canary",
        lambda image, spec: (_ for _ in ()).throw(ImageReleaseCanaryError("failed")),
    )

    with pytest.raises(ImageReleaseCanaryError, match="failed"):
        publish_image_release(_spec(tmp_path))

    assert not any(item.startswith("publish:") for item in events)


def test_publish_image_release_rejects_builder_drift_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modal_computer_use.image as image_module

    events: list[str] = []
    _install_release_fakes(monkeypatch, events=events)
    monkeypatch.setattr(
        image_module,
        "_modal_release_context",
        lambda **kwargs: ("acme", "2026.01", "1.5.3"),
    )

    with pytest.raises(ImageReleaseConflictError, match="Builder Version"):
        publish_image_release(_spec(tmp_path))

    assert "build" not in events


def test_publish_image_release_resumes_a_verified_pending_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modal_computer_use.image as image_module

    events: list[str] = []
    spec = _spec(tmp_path)
    record = _release_record()
    pending = spec.manifest_path.with_name(f".{spec.manifest_path.name}.pending")
    pending.write_text(__import__("json").dumps(record.to_dict()))
    reference = record.image_reference
    exact_image = SimpleNamespace(
        object_id=record.modal_image_object_id,
        publish=lambda name, environment_name: events.append(
            f"resume_publish:{name}:{environment_name}"
        ),
    )
    assignments = [{}, {reference: record.modal_image_object_id}]
    monkeypatch.setattr(
        image_module,
        "_published_named_image_assignments",
        lambda **kwargs: assignments.pop(0),
    )
    monkeypatch.setattr(
        image_module,
        "_resolve_release_image_object_id",
        lambda object_id: exact_image,
    )
    monkeypatch.setattr(image_module, "_utc_now", lambda: "2026-08-08T19:02:00Z")

    resumed = publish_image_release(spec)
    assert resumed.published_at == "2026-08-08T19:02:00Z"
    assert resumed.modal_image_object_id == record.modal_image_object_id
    assert events == [f"resume_publish:{reference}:prod"]
    assert not pending.exists()
    assert ImageReleaseRecord.from_dict(
        __import__("json").loads(spec.manifest_path.read_text())
    ) == resumed


def test_publish_image_release_canary_checks_daemon_frame_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modal_computer_use.image as image_module
    import modal_computer_use.sandbox as sandbox_module

    events: list[str] = []
    canary_runner = image_module._run_image_release_canary
    _install_release_fakes(monkeypatch, events=events)
    monkeypatch.setattr(image_module, "_run_image_release_canary", canary_runner)

    class FakeClient:
        def get_json(self, path: str) -> dict[str, bool]:
            events.append(path)
            return {"ok": True}

    class FakeComputer:
        client = FakeClient()

        def __enter__(self) -> FakeComputer:
            events.append("canary_enter")
            return self

        def __exit__(self, *args: object) -> None:
            events.append("canary_cleanup")

        def first_valid_frame(self, config: object) -> bytes:
            events.append("first_valid_frame")
            return b"png"

        def modal_image_object_id(self) -> str:
            events.append("modal_image_object_id")
            return "im-release-object"

        def ensure_browser_ready(self, config: object) -> None:
            events.append("ensure_browser_ready")

    def create(**kwargs: Any) -> FakeComputer:
        config = kwargs["config"]
        assert config.runtime.modal_environment == "prod"
        assert kwargs["image"].object_id == "im-release-object"
        events.append("canary_create")
        return FakeComputer()

    monkeypatch.setattr(sandbox_module.ComputerSandbox, "create", create)

    record = publish_image_release(_spec(tmp_path))

    assert record.canary.status == "passed"
    assert events.index("canary_cleanup") < events.index(
        f"publish:{record.image_reference}:prod"
    )
    assert events[events.index("canary_enter") + 1 : events.index("first_valid_frame")] == [
        "/healthz",
        "/readyz",
        "/v1/version",
        "/v1/capabilities",
        "modal_image_object_id",
        "ensure_browser_ready",
    ]


def test_resolve_release_image_verifies_name_then_uses_exact_object_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modal_computer_use.image as image_module

    calls: list[str] = []
    expected = object()
    named = SimpleNamespace(
        object_id="im-release-object",
        hydrate=lambda: named,
    )
    fake_modal = SimpleNamespace(
        Image=SimpleNamespace(
            from_id=lambda object_id: calls.append(f"id:{object_id}") or expected,
            from_name=lambda name, environment_name: calls.append(
                f"name:{name}:{environment_name}"
            )
            or named,
        )
    )
    monkeypatch.setattr(image_module, "_modal", lambda: fake_modal)

    assert resolve_release_image(_release_record()) is expected
    assert calls == [
        f"name:modal-computer-use-standard:{REVISION}:prod",
        "id:im-release-object",
    ]


def test_resolve_release_image_rejects_a_mismatched_hydrated_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modal_computer_use.image as image_module

    named = SimpleNamespace(object_id="im-other-object")
    named.hydrate = lambda: named
    fake_modal = SimpleNamespace(
        Image=SimpleNamespace(
            from_name=lambda name, environment_name: named,
            from_id=lambda object_id: pytest.fail("must reject before exact lookup"),
        )
    )
    monkeypatch.setattr(image_module, "_modal", lambda: fake_modal)

    with pytest.raises(ImageReleaseIdentityMismatchError, match="object ID"):
        resolve_release_image(_release_record())


def test_resolve_release_image_maps_modal_not_found_to_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modal_computer_use.image as image_module

    class FakeNotFoundError(Exception):
        pass

    fake_modal = SimpleNamespace(
        exception=SimpleNamespace(NotFoundError=FakeNotFoundError),
        Image=SimpleNamespace(
            from_name=lambda name, environment_name: (_ for _ in ()).throw(
                FakeNotFoundError("provider detail")
            )
        ),
    )
    monkeypatch.setattr(image_module, "_modal", lambda: fake_modal)

    with pytest.raises(
        ImageReleaseNotFoundError,
        match=f"modal-computer-use-standard:{REVISION}.*Environment prod",
    ):
        resolve_release_image(_release_record())


def test_publish_image_release_checks_lock_freshness_before_modal_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modal_computer_use.image as image_module

    events: list[str] = []
    _install_release_fakes(monkeypatch, events=events)
    monkeypatch.setattr(
        image_module,
        "_verify_image_runtime_lock",
        lambda context: (_ for _ in ()).throw(ImageReleaseLockError("stale lock")),
    )

    with pytest.raises(ImageReleaseLockError, match="stale lock"):
        publish_image_release(_spec(tmp_path))

    assert events == ["list:prod"]


def test_publish_image_release_uses_the_pinned_uv_for_lock_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modal_computer_use.image as image_module

    events: list[str] = []
    lock_commands: list[list[str]] = []
    lock_check = image_module._verify_image_runtime_lock
    _install_release_fakes(monkeypatch, events=events)
    monkeypatch.setattr(image_module, "_verify_image_runtime_lock", lock_check)
    monkeypatch.setenv("UV_EXECUTABLE", "/tools/uv")

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        lock_commands.append(command)
        return SimpleNamespace(stdout="uv 0.12.3\n", returncode=0)

    monkeypatch.setattr(image_module.subprocess, "run", run)

    publish_image_release(_spec(tmp_path))

    assert lock_commands == [
        ["/tools/uv", "--version"],
        [
            "/tools/uv",
            "lock",
            "--check",
            "--project",
            str(image_module._image_runtime_context()),
        ],
    ]
