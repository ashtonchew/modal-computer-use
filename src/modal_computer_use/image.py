from __future__ import annotations

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
