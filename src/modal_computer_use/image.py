from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Literal

from .errors import ModalNotInstalledError

DESKTOP_APT_PACKAGES = [
    "xvfb",
    "xfce4",
    "openbox",
    "x11vnc",
    "novnc",
    "websockify",
    "xdotool",
    "libx11-6",
    "libxdamage1",
    "libxtst6",
    "wmctrl",
    "maim",
    "scrot",
    "ffmpeg",
    "xclip",
    "xsel",
    "x11-utils",
    "x11-xserver-utils",
    "dbus-x11",
    "curl",
]

BROWSER_APT_PACKAGES = ["firefox-esr", "chromium"]
NAMED_IMAGE_PREFIX = "modal-computer-use"
NamedImageVariant = Literal["standard", "firefox", "chromium"]


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
    modal = _modal()
    packages = list(DESKTOP_APT_PACKAGES)
    if profile in ("browser", "browser-gpu") or browser:
        packages.extend(BROWSER_APT_PACKAGES)
    image = (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install(*packages)
        .pip_install_from_pyproject("pyproject.toml")
        .env(
            {
                "COMPUTER_USE_WINDOW_MANAGER": window_manager,
                "COMPUTER_USE_IMAGE_PROFILE": profile,
                "COMPUTER_USE_BROWSER_PREWARM": str(browser_prewarm).lower(),
                "COMPUTER_USE_BROWSER": browser or "",
            }
        )
        .add_local_python_source("modal_computer_use")
    )
    return image


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


def _published_named_image_identities(*, environment_name: str | None) -> set[str]:
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
    return {
        str(item["tag"])
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("tag"), str)
    }


def _named_image_recipe(
    *,
    variant: NamedImageVariant,
    window_manager: Literal["xfce", "openbox"],
) -> object:
    modal = _modal()
    packages = list(DESKTOP_APT_PACKAGES)
    browser: Literal["firefox", "chromium"] | None = None
    if variant == "firefox":
        packages.append("firefox-esr")
        browser = "firefox"
    elif variant == "chromium":
        packages.append("chromium")
        browser = "chromium"
    profile = "standard" if variant == "standard" else "browser"
    return (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install(*packages)
        .pip_install_from_pyproject("pyproject.toml")
        .env(
            {
                "COMPUTER_USE_WINDOW_MANAGER": window_manager,
                "COMPUTER_USE_IMAGE_PROFILE": profile,
                "COMPUTER_USE_BROWSER_PREWARM": str(browser is not None).lower(),
                "COMPUTER_USE_BROWSER": browser or "",
            }
        )
        .add_local_python_source("modal_computer_use", copy=True)
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


def browser_image(
    *,
    browser: Literal["firefox", "chromium"] = "firefox",
    window_manager: Literal["xfce", "openbox"] = "xfce",
    gpu: bool = False,
) -> object:
    return default_image(
        profile="browser-gpu" if gpu else "browser",
        browser=browser,
        window_manager=window_manager,
        browser_prewarm=True,
    )
