from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from .daemon.process_environment import (
    DAEMON_CONTROLLER_ENV,
    DESKTOP_USER,
    SHARED_PROCESS_GROUP,
    VNC_SECRET_DIR_ENV,
)
from .errors import (
    ImageReleaseCanaryError,
    ImageReleaseConflictError,
    ImageReleaseIdentityMismatchError,
    ImageReleaseLockError,
    ImageReleaseManifestError,
    ImageReleaseNotFoundError,
    ModalNotInstalledError,
)

DESKTOP_APT_PACKAGES = [
    "xvfb",
    "xfce4",
    "openbox",
    "x11vnc",
    "novnc",
    "websockify",
    "xdotool",
    "libx11-6",
    "libxcb1",
    "libxcb-shm0",
    "libxdamage1",
    "libxtst6",
    "wmctrl",
    "maim",
    "scrot",
    "ffmpeg",
    "xclip",
    "x11-utils",
    "x11-xserver-utils",
    "dbus-x11",
    "curl",
    "util-linux",
]

BROWSER_APT_PACKAGES = ["firefox-esr", "chromium"]
NAMED_IMAGE_PREFIX = "modal-computer-use"
IMAGE_UV_VERSION = "0.12.3"
NamedImageVariant = Literal["standard", "firefox", "chromium"]

_X11_SHARED_MEMORY_BUILD_PACKAGES = (
    "build-essential",
    "libxcb1-dev",
    "libxcb-shm0-dev",
)
_RUST_TOOLCHAIN = "1.91.0"
_X11_SHARED_MEMORY_REMOTE_PATH = "/opt/modal-computer-use/native/x11_shm"
_X11_SHARED_MEMORY_EXTENSION = "_modal_computer_use_x11_shm"
_X11_SHARED_MEMORY_SOURCE_IGNORES = ("target", "target/**", "*.pyc", "__pycache__/**")


@dataclass(frozen=True, slots=True)
class _ImageVariantDefinition:
    profile: Literal["standard", "browser"]
    browser: Literal["firefox", "chromium"] | None
    browser_apt_package: str | None


@dataclass(frozen=True, slots=True)
class _ImageRecipeDefinition:
    """One explicit set of inputs for the shared Modal Image recipe."""

    profile: Literal["standard", "browser", "browser-gpu", "custom"]
    browser: Literal["firefox", "chromium"] | None
    browser_prewarm: bool
    browser_packages: tuple[str, ...]
    window_manager: Literal["xfce", "openbox"]
    copy_source: bool


_IMAGE_VARIANTS: dict[NamedImageVariant, _ImageVariantDefinition] = {
    "standard": _ImageVariantDefinition(
        profile="standard", browser=None, browser_apt_package=None
    ),
    "firefox": _ImageVariantDefinition(
        profile="browser", browser="firefox", browser_apt_package="firefox-esr"
    ),
    "chromium": _ImageVariantDefinition(
        profile="browser", browser="chromium", browser_apt_package="chromium"
    ),
}
_REQUIRED_IMAGE_CANARY_CHECKS = (
    "healthz",
    "readyz",
    "version",
    "capabilities",
    "image_object_id",
    "browser",
    "screenshot",
    "cleanup",
)


@dataclass(frozen=True, slots=True)
class ImageReleaseSpec:
    """Inputs for one revision-addressed managed Image release."""

    source_revision: str
    logical_release: str
    image_variant: NamedImageVariant
    environment_name: str
    manifest_path: Path
    expected_image_builder_version: str
    app_name: str = "modal-computer-use-image-builds"
    canary_timeout_seconds: int = 180

    def __post_init__(self) -> None:
        _require_full_revision(self.source_revision)
        if not self.logical_release.strip():
            raise ValueError("logical_release must be non-empty")
        if self.image_variant not in _IMAGE_VARIANTS:
            raise ValueError("image_variant must be standard, firefox, or chromium")
        if not self.environment_name.strip():
            raise ValueError("environment_name must be non-empty")
        if not self.expected_image_builder_version.strip():
            raise ValueError("expected_image_builder_version must be non-empty")
        if not self.app_name.strip():
            raise ValueError("app_name must be non-empty")
        if self.canary_timeout_seconds < 1 or self.canary_timeout_seconds > 900:
            raise ValueError("canary_timeout_seconds must be between 1 and 900")
        object.__setattr__(self, "manifest_path", Path(self.manifest_path))

    @property
    def image_name(self) -> str:
        return f"{NAMED_IMAGE_PREFIX}-{self.image_variant}"

    @property
    def image_tag(self) -> str:
        return self.source_revision

    @property
    def image_reference(self) -> str:
        return f"{self.image_name}:{self.image_tag}"


@dataclass(frozen=True, slots=True)
class ImageCanaryRecord:
    """Safe evidence from a successful managed Image canary."""

    status: Literal["passed"]
    checks: tuple[str, ...]
    checked_at: str

    def __post_init__(self) -> None:
        if self.status != "passed":
            raise ValueError("canary status must be passed")
        if self.checks != _REQUIRED_IMAGE_CANARY_CHECKS:
            raise ValueError("canary checks do not match the managed Image release contract")
        _require_utc_timestamp(self.checked_at, field_name="canary.checked_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "checks": list(self.checks),
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ImageCanaryRecord:
        _require_exact_fields(
            payload,
            expected={"status", "checks", "checked_at"},
            context="canary",
        )
        status = _require_text_field(payload, "status")
        if status != "passed":
            raise ValueError("canary status must be passed")
        raw_checks = payload["checks"]
        if not isinstance(raw_checks, list) or not all(
            isinstance(item, str) for item in raw_checks
        ):
            raise ValueError("canary checks must be a list of strings")
        return cls(
            status="passed",
            checks=tuple(raw_checks),
            checked_at=_require_text_field(payload, "checked_at"),
        )


@dataclass(frozen=True, slots=True)
class ImageReleaseRecord:
    """Versioned evidence for one published managed Modal Image."""

    schema_version: int
    logical_release: str
    source_revision: str
    image_variant: NamedImageVariant
    image_name: str
    image_tag: str
    image_reference: str
    workspace_name: str
    environment_name: str
    modal_image_object_id: str
    pyproject_sha256: str
    uv_lock_sha256: str
    image_builder_version: str
    uv_version: str
    modal_sdk_version: str
    build_app_name: str
    canary: ImageCanaryRecord
    published_at: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        _require_full_revision(self.source_revision)
        if self.image_variant not in _IMAGE_VARIANTS:
            raise ValueError("image_variant must be standard, firefox, or chromium")
        expected_name = f"{NAMED_IMAGE_PREFIX}-{self.image_variant}"
        if self.image_name != expected_name:
            raise ValueError("image_name does not match image_variant")
        if self.image_tag != self.source_revision:
            raise ValueError("image_tag does not match source_revision")
        if self.image_reference != f"{self.image_name}:{self.image_tag}":
            raise ValueError("image_reference does not match image_name and image_tag")
        for field_name in (
            "logical_release",
            "workspace_name",
            "environment_name",
            "image_builder_version",
            "uv_version",
            "modal_sdk_version",
            "build_app_name",
        ):
            if not cast(str, getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must be non-empty")
        if not self.modal_image_object_id.startswith("im-"):
            raise ValueError("modal_image_object_id must be a Modal Image object ID")
        _require_sha256(self.pyproject_sha256, field_name="pyproject_sha256")
        _require_sha256(self.uv_lock_sha256, field_name="uv_lock_sha256")
        if not isinstance(self.canary, ImageCanaryRecord):
            raise ValueError("canary must be an ImageCanaryRecord")
        _require_utc_timestamp(self.published_at, field_name="published_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "logical_release": self.logical_release,
            "source_revision": self.source_revision,
            "image_variant": self.image_variant,
            "image_name": self.image_name,
            "image_tag": self.image_tag,
            "image_reference": self.image_reference,
            "workspace_name": self.workspace_name,
            "environment_name": self.environment_name,
            "modal_image_object_id": self.modal_image_object_id,
            "pyproject_sha256": self.pyproject_sha256,
            "uv_lock_sha256": self.uv_lock_sha256,
            "image_builder_version": self.image_builder_version,
            "uv_version": self.uv_version,
            "modal_sdk_version": self.modal_sdk_version,
            "build_app_name": self.build_app_name,
            "canary": self.canary.to_dict(),
            "published_at": self.published_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ImageReleaseRecord:
        fields = {
            "schema_version",
            "logical_release",
            "source_revision",
            "image_variant",
            "image_name",
            "image_tag",
            "image_reference",
            "workspace_name",
            "environment_name",
            "modal_image_object_id",
            "pyproject_sha256",
            "uv_lock_sha256",
            "image_builder_version",
            "uv_version",
            "modal_sdk_version",
            "build_app_name",
            "canary",
            "published_at",
        }
        _require_exact_fields(payload, expected=fields, context="release manifest")
        schema_version = payload["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ValueError("schema_version must be an integer")
        raw_variant = _require_text_field(payload, "image_variant")
        if raw_variant not in _IMAGE_VARIANTS:
            raise ValueError("image_variant must be standard, firefox, or chromium")
        raw_canary = payload["canary"]
        if not isinstance(raw_canary, Mapping):
            raise ValueError("canary must be an object")
        return cls(
            schema_version=schema_version,
            logical_release=_require_text_field(payload, "logical_release"),
            source_revision=_require_text_field(payload, "source_revision"),
            image_variant=raw_variant,
            image_name=_require_text_field(payload, "image_name"),
            image_tag=_require_text_field(payload, "image_tag"),
            image_reference=_require_text_field(payload, "image_reference"),
            workspace_name=_require_text_field(payload, "workspace_name"),
            environment_name=_require_text_field(payload, "environment_name"),
            modal_image_object_id=_require_text_field(payload, "modal_image_object_id"),
            pyproject_sha256=_require_text_field(payload, "pyproject_sha256"),
            uv_lock_sha256=_require_text_field(payload, "uv_lock_sha256"),
            image_builder_version=_require_text_field(payload, "image_builder_version"),
            uv_version=_require_text_field(payload, "uv_version"),
            modal_sdk_version=_require_text_field(payload, "modal_sdk_version"),
            build_app_name=_require_text_field(payload, "build_app_name"),
            canary=ImageCanaryRecord.from_dict(raw_canary),
            published_at=_require_text_field(payload, "published_at"),
        )


def _require_exact_fields(
    payload: Mapping[str, object],
    *,
    expected: set[str],
    context: str,
) -> None:
    actual = set(payload)
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected:
        raise ValueError(f"{context} contains unexpected fields: {', '.join(unexpected)}")
    if missing:
        raise ValueError(f"{context} is missing fields: {', '.join(missing)}")


def _require_text_field(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_sha256(value: str, *, field_name: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name} must be a full lower-case SHA-256 value")


def _require_utc_timestamp(value: str, *, field_name: str) -> None:
    if not value.endswith("Z"):
        raise ValueError(f"{field_name} must be an ISO 8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{field_name} must be an ISO 8601 UTC timestamp")


def _image_runtime_context() -> Path:
    """Return the packaged uv project used to install Modal Image dependencies."""
    context = Path(__file__).with_name("_image_runtime").resolve()
    missing = [
        name
        for name in ("pyproject.toml", "uv.lock")
        if not (context / name).is_file() or (context / name).is_symlink()
    ]
    if missing:
        missing_files = ", ".join(missing)
        raise FileNotFoundError(
            f"Modal Image uv context is incomplete at {context}: missing {missing_files}"
        )
    return context


def _managed_source_mount_ignore(path: Path) -> bool:
    """Keep runtime source files and exclude generated or private files."""
    parts = path.parts
    try:
        package_index = len(parts) - 1 - parts[::-1].index("modal_computer_use")
    except ValueError:
        relative_parts = parts
    else:
        relative_parts = parts[package_index + 1 :]
    if "__pycache__" in relative_parts or "target" in relative_parts:
        return True
    if path.name.endswith(".pyc") or any(part.startswith(".") for part in relative_parts):
        return True
    if path.is_dir() or path.suffix == ".py" or path.name == "py.typed":
        return False
    return relative_parts[-2:] not in {
        ("_image_runtime", "pyproject.toml"),
        ("_image_runtime", "uv.lock"),
    }


def _native_screenshot_source() -> Path:
    """Return the packaged Cargo source without depending on the caller's CWD."""
    return Path(__file__).resolve().parent / "_native" / "x11_shm"


def _add_x11_shared_memory_capture(
    image: object,
    *,
    cargo_features: tuple[str, ...] = ("extension-module",),
    prefix_commands: tuple[str, ...] = (),
) -> object:
    """Compile and bake one explicit X11 shared-memory codec artifact.

    The default image keeps the existing Rust miniz_oxide build. Benchmark
    images may pass one alternate, compile-time codec feature; no runtime
    selector is added to the SDK surface.
    """
    source = _native_screenshot_source()
    if not source.is_dir():
        raise RuntimeError(
            "the managed image build is missing the packaged X11 shared-memory Cargo source"
        )

    build_packages = list(_X11_SHARED_MEMORY_BUILD_PACKAGES)
    if "stock-zlib" in cargo_features:
        build_packages.append("zlib1g-dev")
    image = image.apt_install(*build_packages)
    image = image.add_local_dir(
        str(source),
        remote_path=_X11_SHARED_MEMORY_REMOTE_PATH,
        copy=True,
        ignore=_X11_SHARED_MEMORY_SOURCE_IGNORES,
    )
    cargo_manifest = f"{_X11_SHARED_MEMORY_REMOTE_PATH}/Cargo.toml"
    if not cargo_features or any(
        not feature or not all(character.isalnum() or character in "_-" for character in feature)
        for feature in cargo_features
    ):
        raise ValueError("native Cargo features must be non-empty safe identifiers")
    # Cargo accepts a comma-separated feature list as one shell-safe value;
    # passing multiple bare arguments would interpret the second feature as a
    # package name.
    feature_args = ",".join(cargo_features)
    cargo_output = (
        f"{_X11_SHARED_MEMORY_REMOTE_PATH}/target/release/"
        f"lib{_X11_SHARED_MEMORY_EXTENSION}.so"
    )
    image = image.run_commands(
        *prefix_commands,
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init",
        f"chmod 0755 /tmp/rustup-init && /tmp/rustup-init -y --profile minimal "
        f"--default-toolchain {_RUST_TOOLCHAIN}",
        "export PATH=/root/.cargo/bin:$PATH && "
        f"RUSTUP_TOOLCHAIN={_RUST_TOOLCHAIN} PYO3_PYTHON=python "
        "cargo build "
        f"--locked --release --features {feature_args} "
        f"--manifest-path {cargo_manifest}",
        "python -c 'import pathlib, shutil, sysconfig; "
        f"source = pathlib.Path(\"{cargo_output}\"); assert source.is_file(); "
        f"destination = pathlib.Path(sysconfig.get_path(\"platlib\")) / "
        f"\"{_X11_SHARED_MEMORY_EXTENSION}.so\"; "
        "shutil.copy2(source, destination); destination.chmod(0o755)'",
        f"python {_X11_SHARED_MEMORY_REMOTE_PATH}/canary.py",
        f"rm -rf {_X11_SHARED_MEMORY_REMOTE_PATH}/target /root/.cargo/registry "
        "/root/.cargo/git /root/.rustup /root/.cargo/bin /tmp/rustup-init",
        f"python -c 'import {_X11_SHARED_MEMORY_EXTENSION} as m; "
        "assert hasattr(m, \"X11SharedMemoryScreenshotSession\"); "
        "assert issubclass(m.X11ScreenshotTimeoutError, RuntimeError)'",
    )
    return image


def _modal() -> object:
    try:
        import modal
    except ImportError as exc:
        raise ModalNotInstalledError(
            "Modal APIs require the modal extra, for example `uv sync --extra modal` "
            "in this repository or `uv add 'modal-computer-use[modal]'` downstream"
        ) from exc
    return modal


def default_image(
    *,
    profile: Literal["standard", "browser", "browser-gpu", "custom"] = "standard",
    browser: Literal["firefox", "chromium"] | None = None,
    window_manager: Literal["xfce", "openbox"] = "xfce",
    browser_prewarm: bool = False,
) -> object:
    browser_packages = (
        tuple(BROWSER_APT_PACKAGES)
        if profile in ("browser", "browser-gpu") or browser
        else ()
    )
    return _image_recipe(
        _ImageRecipeDefinition(
            profile=profile,
            browser=browser,
            browser_prewarm=browser_prewarm,
            browser_packages=browser_packages,
            window_manager=window_manager,
            copy_source=False,
        )
    )


def named_image_name(
    *,
    revision: str,
    profile: Literal["standard", "browser", "browser-gpu", "custom"],
    browser: Literal["firefox", "chromium"] | None,
) -> str:
    """Return the revision-tagged named Image identity for a runtime selection."""
    _require_full_revision(revision)
    variant = _named_image_variant(profile=profile, browser=browser)
    return f"{NAMED_IMAGE_PREFIX}-{variant}:{revision}"


def named_image(
    *,
    revision: str,
    profile: Literal["standard", "browser", "browser-gpu", "custom"],
    browser: Literal["firefox", "chromium"] | None,
    environment_name: str | None = None,
) -> object:
    """Select a published named Image without triggering an inline build."""
    modal = _modal()
    name = named_image_name(revision=revision, profile=profile, browser=browser)
    return modal.Image.from_name(name, environment_name=environment_name)


def selected_image_identity(
    *,
    source: Literal["inline", "named"],
    revision: str | None,
    profile: Literal["standard", "browser", "browser-gpu", "custom"],
    browser: Literal["firefox", "chromium"] | None,
) -> str:
    """Return a safe, stable identity for tags and benchmark provenance."""
    if source == "named":
        if revision is None:
            raise ValueError("named images require a revision")
        return named_image_name(revision=revision, profile=profile, browser=browser)
    variant = "custom" if profile == "custom" else _inline_image_variant(profile, browser)
    return f"inline:{variant}"


def publish_named_images(
    *,
    revision: str,
    app_name: str = "modal-computer-use-image-builds",
    environment_name: str | None = None,
) -> dict[NamedImageVariant, str]:
    """Build and publish standard, Firefox, and Chromium Images for one Git revision."""
    _require_full_revision(revision)
    variants: tuple[NamedImageVariant, ...] = ("standard", "firefox", "chromium")
    identities = {
        variant: f"{NAMED_IMAGE_PREFIX}-{variant}:{revision}"
        for variant in variants
    }
    existing = _published_named_image_identities(environment_name=environment_name)
    pending = [
        (variant, identity) for variant, identity in identities.items() if identity not in existing
    ]
    if not pending:
        return identities
    _image_runtime_context()
    modal = _modal()
    app = modal.App.lookup(
        app_name,
        create_if_missing=True,
        environment_name=environment_name,
    )
    with modal.enable_output():
        for variant, identity in pending:
            recipe = _named_image_recipe(variant=variant, window_manager="xfce")
            recipe.build(app).publish(identity, environment_name=environment_name)
    return identities


def publish_image_release(spec: ImageReleaseSpec) -> ImageReleaseRecord:
    """Build, verify, publish, and record one immutable managed Image release."""
    assignments = _published_named_image_assignments(
        environment_name=spec.environment_name
    )
    existing_record = _read_image_release_record_if_present(spec.manifest_path)
    pending_path = _pending_image_release_path(spec.manifest_path)
    pending_record = _read_image_release_record_if_present(pending_path)
    assigned_object_id = assignments.get(spec.image_reference)
    if existing_record is not None:
        _require_record_matches_spec(existing_record, spec)
        if assigned_object_id != existing_record.modal_image_object_id:
            raise ImageReleaseConflictError(
                "the release manifest and named Image reference do not identify the same object"
        )
        return existing_record
    if pending_record is not None:
        _require_record_matches_spec(pending_record, spec)
        if assigned_object_id is None:
            pending_image = _resolve_release_image_object_id(
                pending_record.modal_image_object_id
            )
            pending_image.publish(
                pending_record.image_reference,
                environment_name=pending_record.environment_name,
            )
            resumed_assignments = _published_named_image_assignments(
                environment_name=spec.environment_name
            )
            assigned_object_id = resumed_assignments.get(spec.image_reference)
        if assigned_object_id != pending_record.modal_image_object_id:
            raise ImageReleaseConflictError(
                "the pending release manifest and named Image reference do not identify "
                "the same object"
            )
        completed_record = _with_publication_time(pending_record)
        _write_image_release_record(completed_record, pending_path)
        _promote_pending_image_release(pending_path, spec.manifest_path)
        return completed_record
    if assigned_object_id is not None:
        raise ImageReleaseConflictError(
            f"managed Image reference {spec.image_reference} already exists without its "
            "release manifest"
        )

    context = _image_runtime_context()
    _verify_image_runtime_lock(context)
    workspace_name, builder_version, modal_sdk_version = _modal_release_context(
        environment_name=spec.environment_name
    )
    if builder_version != spec.expected_image_builder_version:
        raise ImageReleaseConflictError(
            "the effective Modal Image Builder Version does not match the release specification"
        )

    modal = _modal()
    app = modal.App.lookup(
        spec.app_name,
        create_if_missing=True,
        environment_name=spec.environment_name,
    )
    with modal.enable_output():
        recipe = _named_image_recipe(variant=spec.image_variant, window_manager="xfce")
        built_image = recipe.build(app)
    object_id = getattr(built_image, "object_id", None)
    if not isinstance(object_id, str) or not object_id.startswith("im-"):
        raise ImageReleaseIdentityMismatchError(
            "the built Modal Image did not provide a valid object ID"
        )
    exact_image = _resolve_release_image_object_id(object_id)
    canary = _run_image_release_canary(exact_image, spec)
    record = ImageReleaseRecord(
        schema_version=1,
        logical_release=spec.logical_release,
        source_revision=spec.source_revision,
        image_variant=spec.image_variant,
        image_name=spec.image_name,
        image_tag=spec.image_tag,
        image_reference=spec.image_reference,
        workspace_name=workspace_name,
        environment_name=spec.environment_name,
        modal_image_object_id=object_id,
        pyproject_sha256=_sha256_file(context / "pyproject.toml"),
        uv_lock_sha256=_sha256_file(context / "uv.lock"),
        image_builder_version=builder_version,
        uv_version=IMAGE_UV_VERSION,
        modal_sdk_version=modal_sdk_version,
        build_app_name=spec.app_name,
        canary=canary,
        published_at=_utc_now(),
    )
    _write_image_release_record(record, pending_path)
    built_image.publish(spec.image_reference, environment_name=spec.environment_name)

    post_publish = _published_named_image_assignments(
        environment_name=spec.environment_name
    )
    if post_publish.get(spec.image_reference) != object_id:
        raise ImageReleaseIdentityMismatchError(
            "the published Image reference does not resolve to the built Modal object ID"
        )
    completed_record = _with_publication_time(record)
    _write_image_release_record(completed_record, pending_path)
    _promote_pending_image_release(pending_path, spec.manifest_path)
    return completed_record


def resolve_release_image(record: ImageReleaseRecord) -> object:
    """Verify a release reference in its Environment, then resolve its exact object ID."""
    assignments = _published_named_image_assignments(
        environment_name=record.environment_name
    )
    assigned_object_id = assignments.get(record.image_reference)
    if assigned_object_id is None:
        raise ImageReleaseNotFoundError(
            f"managed Image release {record.image_reference} was not found in "
            f"Environment {record.environment_name}"
        )
    if assigned_object_id != record.modal_image_object_id:
        raise ImageReleaseIdentityMismatchError(
            "the managed Image reference does not identify the recorded Modal object ID"
        )
    return _resolve_release_image_object_id(record.modal_image_object_id)


def _resolve_release_image_object_id(object_id: str) -> object:
    """Resolve one exact Modal Image object ID and verify the hydrated identity."""
    if not isinstance(object_id, str) or not object_id.startswith("im-"):
        raise ImageReleaseIdentityMismatchError("Modal Image object ID is invalid")
    image = _modal().Image.from_id(object_id)
    resolved_id = getattr(image, "object_id", object_id)
    if resolved_id != object_id:
        raise ImageReleaseIdentityMismatchError(
            "the resolved Modal Image object ID does not match the release record"
        )
    return image


def _require_record_matches_spec(
    record: ImageReleaseRecord, spec: ImageReleaseSpec
) -> None:
    if (
        record.logical_release != spec.logical_release
        or record.source_revision != spec.source_revision
        or record.image_variant != spec.image_variant
        or record.image_reference != spec.image_reference
        or record.environment_name != spec.environment_name
        or record.image_builder_version != spec.expected_image_builder_version
        or record.build_app_name != spec.app_name
    ):
        raise ImageReleaseConflictError(
            "the existing release manifest does not match the requested release"
        )


def load_image_release_record(path: str | Path) -> ImageReleaseRecord:
    """Load one strict managed Image release manifest from a regular file."""

    manifest_path = Path(path)
    record = _read_image_release_record_if_present(manifest_path)
    if record is None:
        raise ImageReleaseManifestError("the release manifest does not exist")
    return record


def _read_image_release_record_if_present(path: Path) -> ImageReleaseRecord | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ImageReleaseManifestError("the release manifest path is not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("manifest root must be an object")
        return ImageReleaseRecord.from_dict(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ImageReleaseManifestError("could not read the release manifest") from exc


def _write_image_release_record(record: ImageReleaseRecord, path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ImageReleaseManifestError("the release manifest path is not a regular file")
    serialized = json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
        raise ImageReleaseManifestError("could not write the release manifest") from exc


def _pending_image_release_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.pending")


def _promote_pending_image_release(pending_path: Path, manifest_path: Path) -> None:
    if not pending_path.is_file() or pending_path.is_symlink():
        raise ImageReleaseManifestError("the pending release manifest is not a regular file")
    if manifest_path.is_symlink() or (
        manifest_path.exists() and not manifest_path.is_file()
    ):
        raise ImageReleaseManifestError("the release manifest path is not a regular file")
    try:
        os.replace(pending_path, manifest_path)
    except OSError as exc:
        raise ImageReleaseManifestError("could not write the release manifest") from exc


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _with_publication_time(record: ImageReleaseRecord) -> ImageReleaseRecord:
    payload = record.to_dict()
    payload["published_at"] = _utc_now()
    return ImageReleaseRecord.from_dict(payload)


def _verify_image_runtime_lock(context: Path) -> None:
    uv = os.getenv("UV_EXECUTABLE") or shutil.which("uv")
    if not uv:
        raise ImageReleaseLockError(
            f"uv {IMAGE_UV_VERSION} is required to verify the managed Image lock"
        )
    try:
        version = subprocess.run(  # noqa: S603 - explicit or PATH-resolved uv executable.
            [uv, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        version_parts = version.split()
        if version_parts[:2] != ["uv", IMAGE_UV_VERSION]:
            raise ImageReleaseLockError(
                f"managed Image releases require uv {IMAGE_UV_VERSION}; found {version}"
            )
        subprocess.run(  # noqa: S603 - explicit or PATH-resolved uv executable.
            [uv, "lock", "--check", "--project", str(context)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except ImageReleaseLockError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageReleaseLockError(
            "the managed Image dependency lock is stale or could not be verified"
        ) from exc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _modal_release_context(*, environment_name: str) -> tuple[str, str, str]:
    """Read the active Workspace and effective builder version from Modal."""
    del environment_name
    modal = _modal()
    workspace = modal.Workspace.from_context().hydrate()
    workspace_name = getattr(workspace, "name", None)
    settings = workspace.settings.list()
    builder_version = getattr(settings, "image_builder_version", None)
    if not isinstance(workspace_name, str) or not workspace_name.strip():
        raise ImageReleaseConflictError("Modal did not provide an active Workspace name")
    if not isinstance(builder_version, str) or not builder_version.strip():
        raise ImageReleaseConflictError(
            "Modal did not provide the effective Image Builder Version"
        )
    try:
        modal_sdk_version = importlib.metadata.version("modal")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ModalNotInstalledError("could not determine the installed Modal SDK version") from exc
    return workspace_name, builder_version, modal_sdk_version


def _run_image_release_canary(
    image: object, spec: ImageReleaseSpec
) -> ImageCanaryRecord:
    """Create an exact-image Sandbox and verify the release boundary."""
    try:
        from .config import BrowserConfig, ComputerConfig, ResourceConfig, RuntimeConfig
        from .sandbox import ComputerSandbox

        variant = _IMAGE_VARIANTS[spec.image_variant]
        browser = (
            BrowserConfig(kind=variant.browser, prewarm=True)
            if variant.browser is not None
            else None
        )
        config = ComputerConfig(
            run_id=f"managed-image-canary-{spec.source_revision[:12]}-{spec.image_variant}",
            runtime=RuntimeConfig(
                readiness_timeout_seconds=spec.canary_timeout_seconds,
                modal_environment=spec.environment_name,
            ),
            resources=ResourceConfig(profile=variant.profile),
            browser=browser,
        )
        with ComputerSandbox.create(
            config=config,
            app_name=spec.app_name,
            image=image,
            tags={"computer-use.image-release-canary": "true"},
        ) as computer:
            computer.client.get_json("/healthz")
            computer.client.get_json("/readyz")
            computer.client.get_json("/v1/version")
            computer.client.get_json("/v1/capabilities")
            if computer.modal_image_object_id() != image.object_id:
                raise ImageReleaseIdentityMismatchError(
                    "the canary Sandbox does not use the built Modal Image object ID"
                )
            computer.ensure_browser_ready(config)
            computer.first_valid_frame(config)
    except Exception as exc:
        raise ImageReleaseCanaryError(
            "the managed Image release canary did not complete successfully"
        ) from exc
    return ImageCanaryRecord(
        status="passed",
        checks=_REQUIRED_IMAGE_CANARY_CHECKS,
        checked_at=_utc_now(),
    )


def _published_named_image_identities(*, environment_name: str | None) -> set[str]:
    return set(_published_named_image_assignments(environment_name=environment_name))


def _published_named_image_assignments(
    *, environment_name: str | None
) -> dict[str, str]:
    command = [
        sys.executable,
        "-m",
        "modal",
        "image",
        "names",
        "list",
        "--prefix",
        NAMED_IMAGE_PREFIX,
        "--json",
    ]
    if environment_name is not None:
        command.extend(("--env", environment_name))
    try:
        completed = subprocess.run(  # noqa: S603 - fixed interpreter and Modal CLI arguments.
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not verify existing named Image revision tags") from exc
    if completed.returncode != 0:
        raise RuntimeError("could not verify existing named Image revision tags")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Modal named Image list returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Modal named Image list returned an invalid result")
    assignments: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        tag = item.get("tag")
        image_id = item.get("image_id")
        if not isinstance(tag, str):
            continue
        if not isinstance(image_id, str) or not image_id.startswith("im-"):
            raise RuntimeError("Modal named Image list omitted an Image object ID")
        previous = assignments.get(tag)
        if previous is not None and previous != image_id:
            raise RuntimeError("Modal named Image list returned conflicting object IDs")
        assignments[tag] = image_id
    return assignments


def _named_image_recipe(
    *,
    variant: NamedImageVariant,
    window_manager: Literal["xfce", "openbox"],
) -> object:
    definition = _IMAGE_VARIANTS[variant]
    return _image_recipe(
        _ImageRecipeDefinition(
            profile=definition.profile,
            browser=definition.browser,
            browser_prewarm=definition.browser is not None,
            browser_packages=(
                (definition.browser_apt_package,)
                if definition.browser_apt_package is not None
                else ()
            ),
            window_manager=window_manager,
            copy_source=True,
        )
    )


def _credential_boundary_commands() -> tuple[str, ...]:
    """Create isolated accounts, shared data directories, and a one-way bridge."""

    return (
        (
            f"groupadd --system --gid 1901 {DESKTOP_USER} && "
            f"useradd --system --uid 1901 --gid {DESKTOP_USER} "
            f"--home-dir /home/desktop --create-home --shell /bin/bash {DESKTOP_USER}"
        ),
        (
            f"groupadd --system --gid 1902 {SHARED_PROCESS_GROUP} && "
            f"usermod --append --groups {SHARED_PROCESS_GROUP} {DESKTOP_USER}"
        ),
        f"install -d -m 0755 -o {DESKTOP_USER} -g {DESKTOP_USER} /home/desktop",
        (
            f"install -d -m 3770 -o root -g {SHARED_PROCESS_GROUP} "
            "/home/desktop/artifacts /home/desktop/recordings"
        ),
        (
            "install -d -m 0700 -o root -g root "
            "/var/lib/computer-daemon/runtime"
        ),
        (
            f"install -d -m 2750 -o root -g {SHARED_PROCESS_GROUP} "
            "/var/lib/computer-daemon/vnc"
        ),
    )

def _image_recipe(definition: _ImageRecipeDefinition) -> object:
    """Build the one shared recipe while preserving explicit policy differences."""

    context = _image_runtime_context()
    modal = _modal()
    packages = [*DESKTOP_APT_PACKAGES, *definition.browser_packages]
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install(*packages)
        .uv_sync(
            uv_project_dir=str(context),
            frozen=True,
            uv_version=IMAGE_UV_VERSION,
        )
    )
    image = _add_x11_shared_memory_capture(
        image,
        prefix_commands=_credential_boundary_commands(),
    )
    image = image.env(
        {
            "COMPUTER_USE_WINDOW_MANAGER": definition.window_manager,
            "COMPUTER_USE_IMAGE_PROFILE": definition.profile,
            "COMPUTER_USE_BROWSER_PREWARM": str(
                definition.browser_prewarm
            ).lower(),
            "COMPUTER_USE_BROWSER": definition.browser or "",
            DAEMON_CONTROLLER_ENV: "root",
            "COMPUTER_USE_DESKTOP_USER": DESKTOP_USER,
            VNC_SECRET_DIR_ENV: "/var/lib/computer-daemon/vnc",
            "COMPUTER_USE_RUNTIME_DIR": "/var/lib/computer-daemon/runtime",
        }
    )
    return image.add_local_python_source(
        "modal_computer_use",
        copy=definition.copy_source,
        ignore=_managed_source_mount_ignore,
    )


def _named_image_variant(
    *,
    profile: Literal["standard", "browser", "browser-gpu", "custom"],
    browser: Literal["firefox", "chromium"] | None,
) -> NamedImageVariant:
    if profile == "standard":
        return "standard"
    if profile in ("browser", "browser-gpu") and browser is not None:
        return browser
    if profile == "custom":
        raise ValueError("custom profiles do not have a managed named Image")
    raise ValueError("browser profiles require an explicit browser for named Images")


def _inline_image_variant(
    profile: Literal["standard", "browser", "browser-gpu", "custom"],
    browser: Literal["firefox", "chromium"] | None,
) -> str:
    if profile in ("browser", "browser-gpu") and browser:
        return browser
    return profile


def _require_full_revision(revision: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("named image revision must be a full 40-character Git revision")
