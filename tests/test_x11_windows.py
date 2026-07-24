from __future__ import annotations

import ctypes
import subprocess
from collections.abc import Awaitable, Callable

import anyio
import pytest

from modal_computer_use.daemon.desktop import windows as windows_module
from modal_computer_use.daemon.desktop.windows import (
    NativeEwmhWindowAdapter,
    NativeWindowRequest,
    X11WindowController,
    X11WindowNativeOperationError,
    X11WindowNativeUnavailableError,
    normalize_window_id,
    parse_window_id,
)
from modal_computer_use.models import X11Window


class FakeNativeWindowAdapter:
    def __init__(
        self,
        *,
        windows: list[X11Window] | None = None,
        available: bool = True,
        failure: str | None = None,
        operation_error: Exception | None = None,
        verified: bool = True,
    ) -> None:
        self.windows = windows or []
        self._available = available
        self.failure = failure
        self.operation_error = operation_error
        self.verified = verified
        self.activations: list[int] = []
        self.closures: list[int] = []
        self.closed = False

    def available(self) -> bool:
        return self._available

    def close_display(self) -> None:
        self.closed = True

    def list(self) -> list[X11Window]:
        if self.operation_error is not None:
            raise self.operation_error
        return self.windows

    def activate(self, window_id: int) -> NativeWindowRequest:
        if self.operation_error is not None:
            raise self.operation_error
        self.activations.append(window_id)
        return NativeWindowRequest(verified=self.verified)

    def close(self, window_id: int) -> NativeWindowRequest:
        if self.operation_error is not None:
            raise self.operation_error
        self.closures.append(window_id)
        return NativeWindowRequest(verified=self.verified)


class CommandRecorder:
    def __init__(
        self,
        responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, ...]] = []

    async def __call__(
        self,
        *args: str,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return self.responses.get(args, subprocess.CompletedProcess(args, 0, "", ""))


async def _fallback_windows() -> list[X11Window]:
    return [
        X11Window(
            id="mock-root",
            title="Mock Desktop",
            x=0,
            y=0,
            width=100,
            height=100,
            is_active=True,
        )
    ]


def _controller(
    native: FakeNativeWindowAdapter,
    run: Callable[..., Awaitable[subprocess.CompletedProcess[str]]] | None = None,
) -> X11WindowController:
    controller = X11WindowController(
        run=run or CommandRecorder(),
        fallback_windows=_fallback_windows,
        native=native,
    )
    controller._commands.available = lambda: True  # type: ignore[method-assign]
    return controller


def test_native_window_list_is_the_canonical_path() -> None:
    expected = [
        X11Window(
            id="0x00100001",
            title="Terminal",
            class_name="terminal.Terminal",
            pid=123,
            x=10,
            y=20,
            width=800,
            height=600,
            workspace=2,
            is_active=True,
        )
    ]
    native = FakeNativeWindowAdapter(windows=expected)
    commands = CommandRecorder()
    controller = _controller(native, commands)

    windows = anyio.run(controller.list)

    assert windows == expected
    assert commands.calls == []
    assert controller.backend_name == "xlib-ewmh"
    assert anyio.run(controller.active) == expected[0]


def test_window_list_falls_back_to_wmctrl_with_field_parity() -> None:
    native = FakeNativeWindowAdapter(
        operation_error=X11WindowNativeUnavailableError("EWMH unavailable")
    )
    commands = CommandRecorder(
        {
            ("wmctrl", "-lpGx"): subprocess.CompletedProcess(
                (),
                0,
                "0x01000001 2 123 10 20 800 600 terminal.Terminal Terminal title\ninvalid line\n",
                "",
            ),
            ("xdotool", "getactivewindow"): subprocess.CompletedProcess((), 0, "16777217\n", ""),
        }
    )
    controller = _controller(native, commands)

    result = anyio.run(controller.list)

    assert result == [
        X11Window(
            id="0x01000001",
            title="Terminal title",
            class_name="terminal.Terminal",
            pid=123,
            x=10,
            y=20,
            width=800,
            height=600,
            workspace=2,
            is_active=True,
        )
    ]
    assert controller.backend_name == "wmctrl"
    assert commands.calls == [
        ("wmctrl", "-lpGx"),
        ("xdotool", "getactivewindow"),
    ]


def test_window_list_preserves_mock_fallback_when_both_real_paths_fail() -> None:
    native = FakeNativeWindowAdapter(
        operation_error=X11WindowNativeUnavailableError("XOpenDisplay failed")
    )
    commands = CommandRecorder(
        {
            ("wmctrl", "-lpGx"): subprocess.CompletedProcess((), 1, "", "not found"),
        }
    )
    controller = _controller(native, commands)

    assert anyio.run(controller.list) == anyio.run(_fallback_windows)


def test_window_list_uses_local_fallback_when_command_tools_are_absent() -> None:
    native = FakeNativeWindowAdapter(
        operation_error=X11WindowNativeUnavailableError("XOpenDisplay failed")
    )
    commands = CommandRecorder()
    controller = _controller(native, commands)
    controller._commands.available = lambda: False  # type: ignore[method-assign]

    assert anyio.run(controller.list) == anyio.run(_fallback_windows)
    assert commands.calls == []
    assert controller.backend_name == "fallback"


@pytest.mark.parametrize("operation", ["activate", "close_window"])
def test_native_window_requests_use_validated_numeric_ids(operation: str) -> None:
    native = FakeNativeWindowAdapter(verified=True)
    controller = _controller(native)

    result = anyio.run(getattr(controller, operation), "0x01000001")

    assert result.ok is True
    assert result.message is None
    assert result.output == {
        "window_id": "0x01000001",
        "window_backend": "xlib-ewmh",
        "verified": True,
    }
    if operation == "activate":
        assert native.activations == [0x01000001]
    else:
        assert native.closures == [0x01000001]


def test_unconfirmed_native_request_is_successfully_accepted_without_polling() -> None:
    native = FakeNativeWindowAdapter(verified=False)
    controller = _controller(native)

    result = anyio.run(controller.activate, "42")

    assert result.ok is True
    assert result.message == "activation requested; window manager has not confirmed it yet"
    assert result.output["verified"] is False


@pytest.mark.parametrize("operation, flag", [("activate", "-ia"), ("close_window", "-ic")])
def test_native_request_failure_uses_wmctrl_rollout_fallback(
    operation: str,
    flag: str,
) -> None:
    native = FakeNativeWindowAdapter(
        operation_error=X11WindowNativeOperationError("window manager rejected request")
    )
    commands = CommandRecorder()
    controller = _controller(native, commands)

    result = anyio.run(getattr(controller, operation), "42")

    assert result.ok is True
    assert result.output == {
        "window_id": "0x0000002a",
        "window_backend": "wmctrl",
    }
    assert commands.calls == [("wmctrl", flag, "0x0000002a")]
    assert controller.backend_name == "wmctrl"


@pytest.mark.parametrize("operation", ["activate", "close_window"])
def test_native_request_failure_is_structured_when_wmctrl_is_absent(
    operation: str,
) -> None:
    native = FakeNativeWindowAdapter(
        operation_error=X11WindowNativeOperationError("window manager rejected request")
    )
    commands = CommandRecorder()
    controller = _controller(native, commands)
    controller._commands.available = lambda: False  # type: ignore[method-assign]

    result = anyio.run(getattr(controller, operation), "42")

    assert result.ok is False
    assert "wmctrl fallback is unavailable" in (result.message or "")
    assert result.output == {
        "window_id": "0x0000002a",
        "window_backend": "unavailable",
    }
    assert commands.calls == []


@pytest.mark.parametrize(
    "window_id",
    ["", "not-a-window", "0", "-1", "0x100000000", "  "],
)
def test_invalid_window_ids_fail_before_native_or_command_side_effects(window_id: str) -> None:
    native = FakeNativeWindowAdapter()
    commands = CommandRecorder()
    controller = _controller(native, commands)

    activate = anyio.run(controller.activate, window_id)
    close = anyio.run(controller.close_window, window_id)

    assert activate.ok is False
    assert activate.message == "invalid window id"
    assert close.ok is False
    assert native.activations == []
    assert native.closures == []
    assert commands.calls == []


def test_window_backend_probe_prefers_native_and_lifecycle_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = FakeNativeWindowAdapter(available=True)
    controller = _controller(native)
    controller._commands.available = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(windows_module.shutil, "which", lambda _name: None)

    assert controller.probe_backend() == (True, None)
    assert controller.backend_name == "xlib-ewmh"

    controller.close()

    assert native.closed is True


def test_window_backend_probe_accepts_command_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = FakeNativeWindowAdapter(
        available=False,
        failure="XOpenDisplay failed",
    )
    controller = _controller(native)
    controller._commands.available = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(windows_module.shutil, "which", lambda _name: "/usr/bin/tool")

    assert controller.probe_backend() == (True, None)
    assert controller.backend_name == "wmctrl"


def test_window_backend_probe_reports_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = FakeNativeWindowAdapter(
        available=False,
        failure="XOpenDisplay failed",
    )
    controller = _controller(native)
    controller._commands.available = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(windows_module.shutil, "which", lambda _name: None)

    assert controller.probe_backend() == (False, "XOpenDisplay failed")


def test_window_id_parsing_and_canonical_formatting() -> None:
    assert parse_window_id("42") == 42
    assert parse_window_id("00042") == 42
    assert parse_window_id("0x2A") == 42
    assert parse_window_id("0xffffffff") == 0xFFFFFFFF
    assert normalize_window_id("42") == "0x0000002a"
    assert normalize_window_id("0x00100001") == "0x00100001"


class InspectableNativeAdapter(NativeEwmhWindowAdapter):
    def __init__(self) -> None:
        super().__init__(display=":test")
        self._display = 1
        self._root = 99
        self.sent: list[tuple[str, int, tuple[int, int, int, int, int]]] = []
        self.client_lists: list[list[int]] = [[10, 20]]
        self.active_ids: list[int | None] = [20]

    def _ensure_open(self) -> tuple[int, int]:
        return 1, 99

    def _client_window_ids(self) -> list[int]:
        return self.client_lists.pop(0)

    def _active_window_id(self) -> int | None:
        return self.active_ids.pop(0)

    def _read_window(self, window_id: int, *, active_id: int | None) -> X11Window | None:
        return X11Window(
            id=normalize_window_id(str(window_id)),
            title=f"Window {window_id}",
            x=window_id,
            y=0,
            width=100,
            height=100,
            is_active=window_id == active_id,
        )

    def _send_client_message(
        self,
        *,
        display: int,
        root: int,
        window_id: int,
        message_name: str,
        data: tuple[int, int, int, int, int],
    ) -> None:
        assert display == 1
        assert root == 99
        self.sent.append((message_name, window_id, data))

    def _sync(self, display: int) -> None:
        assert display == 1


def test_native_adapter_lists_stacking_order_and_marks_active_window() -> None:
    adapter = InspectableNativeAdapter()

    result = adapter.list()

    assert [window.id for window in result] == ["0x0000000a", "0x00000014"]
    assert [window.is_active for window in result] == [False, True]


def test_native_adapter_sends_ewmh_activation_and_verifies_once() -> None:
    adapter = InspectableNativeAdapter()
    adapter.client_lists = [[10, 20]]
    adapter.active_ids = [10, 20]

    result = adapter.activate(20)

    assert result == NativeWindowRequest(verified=True)
    assert adapter.sent == [
        ("_NET_ACTIVE_WINDOW", 20, (1, 0, 10, 0, 0)),
    ]


def test_native_adapter_sends_ewmh_close_and_verifies_once() -> None:
    adapter = InspectableNativeAdapter()
    adapter.client_lists = [[10, 20], [10]]

    result = adapter.close(20)

    assert result == NativeWindowRequest(verified=True)
    assert adapter.sent == [
        ("_NET_CLOSE_WINDOW", 20, (0, 1, 0, 0, 0)),
    ]


def test_native_adapter_rejects_unmanaged_window_before_sending() -> None:
    adapter = InspectableNativeAdapter()
    adapter.client_lists = [[10]]

    with pytest.raises(X11WindowNativeOperationError, match="not managed"):
        adapter.activate(20)

    assert adapter.sent == []


def test_native_adapter_converts_unexpected_xlib_failures() -> None:
    adapter = InspectableNativeAdapter()

    def fail() -> list[int]:
        raise ValueError("low-level detail")

    adapter._client_window_ids = fail  # type: ignore[method-assign]

    with pytest.raises(
        X11WindowNativeOperationError,
        match="native X11 window listing failed: ValueError",
    ):
        adapter.list()


def test_native_window_adapter_retries_when_display_becomes_ready() -> None:
    adapter = NativeEwmhWindowAdapter(display=":99")

    class StartingX11:
        attempts = 0

        def XOpenDisplay(self, _display_name: object) -> int:
            self.attempts += 1
            return 0 if self.attempts == 1 else 1

        def XDefaultRootWindow(self, _display: object) -> int:
            return 99

    adapter._x11 = StartingX11()
    adapter._client_window_ids = lambda: []  # type: ignore[method-assign]
    adapter._require_live_window_manager = lambda: None  # type: ignore[method-assign]
    adapter._require_supported_operations = lambda: None  # type: ignore[method-assign]

    assert adapter.available() is False
    assert adapter.failure == "XOpenDisplay failed"
    assert adapter.available() is True
    assert adapter.failure is None


def test_native_adapter_does_not_cache_atoms_missing_during_startup() -> None:
    adapter = NativeEwmhWindowAdapter(display=":99")

    class StartingAtomsX11:
        calls = 0

        def XInternAtom(self, *_args: object) -> int:
            self.calls += 1
            return 0 if self.calls == 1 else 42

    x11 = StartingAtomsX11()
    adapter._x11 = x11
    adapter._display = 1

    assert adapter._atom("_NET_CLIENT_LIST", only_if_exists=True) == 0
    assert adapter._atom("_NET_CLIENT_LIST", only_if_exists=True) == 42
    assert x11.calls == 2


def test_native_adapter_requires_activation_and_close_support() -> None:
    adapter = InspectableNativeAdapter()
    adapter._root = 99
    atoms = {"_NET_ACTIVE_WINDOW": 10, "_NET_CLOSE_WINDOW": 20}
    adapter._atom = lambda name, **_kwargs: atoms.get(name, 1)  # type: ignore[method-assign]
    adapter._property_values = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: [1, 10]
    )

    with pytest.raises(
        X11WindowNativeUnavailableError,
        match="_NET_CLOSE_WINDOW",
    ):
        NativeEwmhWindowAdapter._require_supported_operations(adapter)


def test_native_adapter_requires_live_self_referencing_window_manager() -> None:
    adapter = InspectableNativeAdapter()
    adapter._root = 99

    def values(window_id: int, name: str, **_kwargs: object) -> list[int] | None:
        assert name == "_NET_SUPPORTING_WM_CHECK"
        assert window_id in {99, 42}
        return [42]

    adapter._property_values = values  # type: ignore[method-assign]

    NativeEwmhWindowAdapter._require_live_window_manager(adapter)


def test_native_adapter_rejects_stale_window_manager_ownership() -> None:
    adapter = InspectableNativeAdapter()
    adapter._root = 99

    def values(window_id: int, name: str, **_kwargs: object) -> list[int] | None:
        assert name == "_NET_SUPPORTING_WM_CHECK"
        return [42] if window_id == 99 else [7]

    adapter._property_values = values  # type: ignore[method-assign]

    with pytest.raises(X11WindowNativeUnavailableError, match="ownership check is stale"):
        NativeEwmhWindowAdapter._require_live_window_manager(adapter)


def test_native_adapter_decodes_32_bit_properties_from_xlib_long_storage() -> None:
    adapter = InspectableNativeAdapter()
    values = (ctypes.c_ulong * 3)(0x10, 0x20, 0xFFFFFFFF)
    payload = ctypes.string_at(values, ctypes.sizeof(values))
    adapter._property = lambda *_args, **_kwargs: (32, payload)  # type: ignore[method-assign]

    assert adapter._property_values(99, "_NET_CLIENT_LIST") == [
        0x10,
        0x20,
        0xFFFFFFFF,
    ]


def test_native_adapter_prefers_stacking_list_then_uses_client_list() -> None:
    adapter = InspectableNativeAdapter()
    requested: list[str] = []

    def values(_window_id: int, name: str, **_kwargs: object) -> list[int] | None:
        requested.append(name)
        return None if name == "_NET_CLIENT_LIST_STACKING" else [30, 10]

    adapter._property_values = values  # type: ignore[method-assign]

    assert NativeEwmhWindowAdapter._client_window_ids(adapter) == [30, 10]
    assert requested == ["_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"]


def test_native_adapter_reads_window_metadata_with_icccm_fallback() -> None:
    adapter = InspectableNativeAdapter()
    properties: dict[str, tuple[int, bytes] | None] = {
        "_NET_WM_NAME": None,
        "WM_NAME": (8, b"Caf\xe9\0"),
        "WM_CLASS": (8, b"terminal\0Terminal\0"),
    }
    integer_properties = {
        "_NET_WM_PID": 123,
        "_NET_WM_DESKTOP": 0xFFFFFFFF,
    }
    adapter._property = (  # type: ignore[method-assign]
        lambda _window_id, name, **_kwargs: properties.get(name)
    )
    adapter._geometry = lambda _window_id: (10, 20, 800, 600)  # type: ignore[method-assign]
    adapter._first_integer_property = (  # type: ignore[method-assign]
        lambda _window_id, name: integer_properties.get(name)
    )
    adapter._atom = lambda *_args, **_kwargs: 1  # type: ignore[method-assign]

    result = NativeEwmhWindowAdapter._read_window(adapter, 42, active_id=42)

    assert result == X11Window(
        id="0x0000002a",
        title="Café",
        class_name="terminal.Terminal",
        pid=123,
        x=10,
        y=20,
        width=800,
        height=600,
        workspace=-1,
        is_active=True,
    )
