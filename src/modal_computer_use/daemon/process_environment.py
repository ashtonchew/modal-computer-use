from __future__ import annotations

import os
import pwd
import shutil
from collections.abc import Mapping

_SECRET_MARKERS = ("_CREDENTIAL", "_PASSWORD", "_SECRET", "_TOKEN")
DAEMON_USER_ENV = "COMPUTER_USE_DAEMON_USER"
DESKTOP_USER_ENV = "COMPUTER_USE_DESKTOP_USER"
VNC_SECRET_DIR_ENV = "COMPUTER_USE_VNC_SECRET_DIR"  # noqa: S105 - environment key, not a secret.
DAEMON_SERVICE_USER = "computer-daemon"
DESKTOP_USER = "computer-desktop"
SHARED_PROCESS_GROUP = "computer-use"


def desktop_process_environment(
    *,
    display: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a desktop child environment without daemon-owned credentials."""
    source = os.environ if environ is None else environ
    env = {key: value for key, value in source.items() if not _is_daemon_secret_name(key)}
    env["DISPLAY"] = display
    user = env.get(DESKTOP_USER_ENV)
    if user:
        # sudo -H also sets these values for managed images. Supplying them in
        # the child environment keeps direct test/local launchers deterministic
        # and prevents a desktop app from inheriting the daemon's HOME.
        env["HOME"] = (
            "/home/desktop" if user == DESKTOP_USER else env.get("HOME") or "/home/desktop"
        )
        env["USER"] = user
        env["LOGNAME"] = user
    return env


def desktop_process_command(
    *args: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Run a desktop child under the image's non-daemon UID.

    Managed Images set ``COMPUTER_USE_DESKTOP_USER`` to a dedicated account.
    The daemon runs as the service account and uses the root-owned ``sudo``
    policy baked into that image to cross into the desktop account. Local and
    custom images leave the marker unset, preserving their existing command
    shape. Never silently fall back to the daemon UID when the marker is set.
    """

    if not args:
        raise ValueError("desktop command must contain at least one argument")
    source = os.environ if environ is None else environ
    user = source.get(DESKTOP_USER_ENV)
    if not user or os.name != "posix":
        return tuple(args)
    target_uid = _require_user_uid(user, role="desktop")
    if os.geteuid() == target_uid:
        return tuple(args)
    # Keep the wrapper explicit and non-interactive. A missing sudo policy or
    # account causes the child to fail instead of running with daemon rights.
    sudo = shutil.which("sudo") or "sudo"
    return (sudo, "-n", "-H", "-u", user, "--", *args)


def daemon_process_command(
    *args: str,
    environ: Mapping[str, str] | None = None,
    managed_image: bool = False,
) -> tuple[str, ...]:
    """Build a daemon launcher whose UID decision runs inside the Image.

    ``managed_image`` is selected by the SDK from the image source, not from
    caller environment values. The shell trampoline evaluates the baked image
    marker in-container and fails closed when a managed release predates the
    credential boundary. Account resolution never occurs on the caller machine.
    """

    if not args:
        raise ValueError("daemon command must contain at least one argument")
    del environ
    if not managed_image:
        return tuple(args)
    # ``$@`` is expanded only in the image. No caller-controlled values or
    # bearer secrets are interpolated into the script or launcher argv.
    trampoline = (
        'if [ -n "$COMPUTER_USE_DAEMON_USER" ]; then '
        'exec setpriv --reuid="$COMPUTER_USE_DAEMON_USER" '
        '--regid="$COMPUTER_USE_DAEMON_USER" --init-groups -- "$@"; '
        "else echo 'managed image credential boundary is unavailable' >&2; exit 78; fi"
    )
    return (
        "sh",
        "-c",
        trampoline,
        "modal-computer-use-daemon",
        *args,
    )


def _desktop_user_uid(user: str) -> int | None:
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def _require_user_uid(user: str, *, role: str) -> int:
    target_uid = _desktop_user_uid(user)
    if target_uid is None:
        raise RuntimeError(f"configured {role} user does not exist: {user}")
    return target_uid


def _is_daemon_secret_name(name: str) -> bool:
    return name.startswith("COMPUTER_USE_") and any(marker in name for marker in _SECRET_MARKERS)
