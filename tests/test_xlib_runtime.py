from __future__ import annotations

from modal_computer_use.daemon.desktop import _xlib_runtime


class _FakeFunction:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: list[object] = []
        self.restype: object = None

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


class _FakeXlib:
    def __init__(self) -> None:
        self.XInitThreads = _FakeFunction(1)
        self.XSetErrorHandler = _FakeFunction(0)


def test_xlib_runtime_installs_nonfatal_handler_once(monkeypatch) -> None:
    monkeypatch.setattr(_xlib_runtime, "_configured", False)
    x11 = _FakeXlib()

    _xlib_runtime.configure_xlib_runtime(x11)  # type: ignore[arg-type]
    _xlib_runtime.configure_xlib_runtime(x11)  # type: ignore[arg-type]

    assert x11.XInitThreads.calls == [()]
    assert x11.XSetErrorHandler.calls == [
        (_xlib_runtime._nonfatal_xlib_error_handler,)
    ]
