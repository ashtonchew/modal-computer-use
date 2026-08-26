from __future__ import annotations

import os
import pwd
import shutil
from collections.abc import Mapping

_SECRET_MARKERS = ("_CREDENTIAL", "_PASSWORD", "_SECRET", "_TOKEN")
DAEMON_CONTROLLER_ENV = "COMPUTER_USE_DAEMON_CONTROLLER"
DESKTOP_USER_ENV = "COMPUTER_USE_DESKTOP_USER"
VNC_SECRET_DIR_ENV = "COMPUTER_USE_VNC_SECRET_DIR"  # noqa: S105 - environment key, not a secret.
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
    Their daemon is the root lifecycle controller so it can drop each desktop
    child to that account even when Modal enforces ``no_new_privs``. Local and
    custom images leave the marker unset, preserving their existing command
    shape. Never silently run a managed desktop child as the controller.
    """

    if not args:
        raise ValueError("desktop command must contain at least one argument")
    source = os.environ if environ is None else environ
    user = source.get(DESKTOP_USER_ENV)
    if not user or os.name != "posix":
        return tuple(args)
    target_uid, target_gid = _require_user_identity(user, role="desktop")
    if os.geteuid() == target_uid:
        return tuple(args)
    if os.geteuid() != 0:
        raise RuntimeError("managed desktop command requires the managed root controller")
    setpriv = shutil.which("setpriv") or "setpriv"
    return (
        setpriv,
        f"--reuid={target_uid}",
        f"--regid={target_gid}",
        "--init-groups",
        "--",
        *args,
    )


def prepare_desktop_output_file(
    file_descriptor: int,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Grant a managed desktop child write access without making it file owner.

    Root keeps ownership so another process with the desktop UID cannot replace
    the path in a sticky temporary directory. The desktop account receives only
    group read/write access to the already-open output file.
    """

    source = os.environ if environ is None else environ
    user = source.get(DESKTOP_USER_ENV)
    if not user or os.name != "posix":
        return
    target_uid, target_gid = _require_user_identity(user, role="desktop")
    current_uid = os.geteuid()
    if current_uid == target_uid:
        return
    if current_uid != 0:
        raise RuntimeError("managed desktop output requires the managed root controller")
    os.fchown(file_descriptor, 0, target_gid)
    os.fchmod(file_descriptor, 0o660)


def daemon_process_command(
    *args: str,
    environ: Mapping[str, str] | None = None,
    managed_image: bool = False,
) -> tuple[str, ...]:
    """Build a daemon launcher whose controller check runs inside the Image.

    ``managed_image`` is selected by the SDK from the image source, not from
    caller environment values. The shell trampoline evaluates the baked marker
    in-container and fails closed unless the managed daemon retains the root
    authority required to drop every GUI child to the desktop account. This is
    compatible with Modal's ``no_new_privs`` runtime boundary; attempting to
    regain privilege from a non-root daemon is not.
    """

    if not args:
        raise ValueError("daemon command must contain at least one argument")
    del environ
    if not managed_image:
        return tuple(args)
    # ``$@`` and the marker are expanded only in the image. No caller-controlled
    # values or bearer secrets are interpolated into the script or launcher argv.
    trampoline = (
        'if [ "$COMPUTER_USE_DAEMON_CONTROLLER" != "root" ]; then '
        "echo 'managed image credential boundary is unavailable' >&2; exit 78; "
        'elif [ "$(id -u)" -ne 0 ]; then '
        "echo 'managed image root controller is unavailable' >&2; exit 77; "
        'else exec "$@"; fi'
    )
    return (
        "sh",
        "-c",
        trampoline,
        "modal-computer-use-daemon",
        *args,
    )


def _desktop_user_identity(user: str) -> tuple[int, int] | None:
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        return None
    return account.pw_uid, account.pw_gid


def _require_user_identity(user: str, *, role: str) -> tuple[int, int]:
    identity = _desktop_user_identity(user)
    if identity is None:
        raise RuntimeError(f"configured {role} user does not exist: {user}")
    return identity


def _is_daemon_secret_name(name: str) -> bool:
    return name.startswith("COMPUTER_USE_") and any(marker in name for marker in _SECRET_MARKERS)
