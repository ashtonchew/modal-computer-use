from __future__ import annotations

from modal_computer_use.daemon.desktop.xdamage import (
    XDamageRect,
    XDamageWaitResult,
    XDamageWatcher,
    prepare_change_signal,
)


class _FakeWatcher:
    def __init__(self, *, arm_error: str | None = None) -> None:
        self.arm_error = arm_error
        self.failure = arm_error
        self.armed = 0
        self.closed = False

    def arm(self) -> None:
        self.armed += 1
        if self.arm_error is not None:
            raise RuntimeError(self.arm_error)

    def close(self) -> None:
        self.closed = True


def test_prepare_change_signal_poll_does_not_create_watcher() -> None:
    created: list[str] = []

    prepared = prepare_change_signal(
        "poll",
        display=":99",
        watcher_factory=lambda *, display: created.append(display) or _FakeWatcher(),
    )

    assert created == []
    assert prepared.wait_watcher is None
    assert prepared.metadata(None) == {
        "change_signal_requested": "poll",
        "change_signal_active": "poll",
        "change_signal_available": None,
        "change_signal_detected": None,
        "change_signal_wait_ms": None,
        "change_signal_reason": None,
        "change_signal_version": None,
    }


def test_prepare_change_signal_auto_falls_back_and_retains_watcher_for_owner() -> None:
    watcher = _FakeWatcher(arm_error="XDamage extension unavailable")

    prepared = prepare_change_signal("auto", display=":99", watcher=watcher)

    assert prepared.active == "poll"
    assert prepared.wait_watcher is None
    assert prepared.reusable_watcher is watcher
    assert prepared.metadata(None)["change_signal_available"] is False
    assert prepared.metadata(None)["change_signal_reason"] == "XDamage extension unavailable"
    prepared.close()
    assert watcher.closed is True


def test_prepare_change_signal_explicit_xdamage_preserves_unavailable_wait_result() -> None:
    watcher = _FakeWatcher(arm_error="XDamage extension unavailable")
    wait_result = XDamageWaitResult(
        available=False,
        detected=False,
        wait_ms=1.25,
        reason="XDamage extension unavailable",
    )

    prepared = prepare_change_signal("xdamage", display=":99", watcher=watcher)

    assert prepared.active == "xdamage"
    assert prepared.wait_watcher is watcher
    assert prepared.metadata(wait_result) == {
        "change_signal_requested": "xdamage",
        "change_signal_active": "xdamage",
        "change_signal_available": False,
        "change_signal_detected": False,
        "change_signal_wait_ms": 1.25,
        "change_signal_reason": "XDamage extension unavailable",
        "change_signal_version": None,
    }


def test_prepare_change_signal_without_display_preserves_auto_fallback_metadata() -> None:
    prepared = prepare_change_signal("auto", display=None)

    assert prepared.active == "poll"
    assert prepared.reusable_watcher is None
    assert prepared.metadata(None)["change_signal_available"] is False
    assert prepared.metadata(None)["change_signal_reason"] == "backend has no X11 display"


def test_xdamage_arm_syncs_before_draining_stale_events(monkeypatch) -> None:
    watcher = XDamageWatcher(display=":99")
    events: list[str] = []

    monkeypatch.setattr(watcher, "start", lambda: events.append("start"))
    monkeypatch.setattr(watcher, "_subtract_damage", lambda: events.append("subtract"))
    monkeypatch.setattr(watcher, "_sync", lambda: events.append("sync"))
    monkeypatch.setattr(watcher, "_drain_events", lambda: events.append("drain"))

    watcher.arm()

    assert events == ["start", "subtract", "sync", "drain"]


def test_xdamage_wait_resets_damage_after_detected_event(monkeypatch) -> None:
    watcher = XDamageWatcher(display=":99", rect_hints=True)
    events: list[str] = []

    monkeypatch.setattr(watcher, "start", lambda: events.append("start"))
    monkeypatch.setattr(watcher, "_next_damage_rects", lambda: (XDamageRect(1, 2, 3, 4),))
    monkeypatch.setattr(
        watcher,
        "_subtract_damage",
        lambda *, fetch_rects=False: events.append(f"subtract:{fetch_rects}") or (),
    )
    monkeypatch.setattr(watcher, "_sync", lambda: events.append("sync"))
    watcher._x11 = object()  # type: ignore[assignment]
    watcher._display = 1
    watcher._event_base = 1

    result = watcher.wait(timeout_ms=100)

    assert result.available is True
    assert result.detected is True
    assert result.dirty_rect == XDamageRect(x=1, y=2, width=3, height=4)
    assert result.dirty_rects == (XDamageRect(x=1, y=2, width=3, height=4),)
    assert events == ["start", "subtract:True", "sync"]


def test_xdamage_wait_prefers_fetched_region_rects(monkeypatch) -> None:
    watcher = XDamageWatcher(display=":99", rect_hints=True)

    monkeypatch.setattr(watcher, "start", lambda: None)
    monkeypatch.setattr(watcher, "_next_damage_rects", lambda: (XDamageRect(1, 2, 3, 4),))
    monkeypatch.setattr(
        watcher,
        "_subtract_damage",
        lambda *, fetch_rects=False: (
            XDamageRect(10, 20, 5, 6),
            XDamageRect(12, 21, 8, 9),
        ),
    )
    monkeypatch.setattr(watcher, "_sync", lambda: None)
    watcher._x11 = object()  # type: ignore[assignment]
    watcher._display = 1
    watcher._event_base = 1

    result = watcher.wait(timeout_ms=100)

    assert result.detected is True
    assert result.dirty_rects == (
        XDamageRect(x=10, y=20, width=5, height=6),
        XDamageRect(x=12, y=21, width=8, height=9),
    )
    assert result.dirty_rect == XDamageRect(x=10, y=20, width=10, height=10)


def test_xdamage_wait_default_mode_skips_rect_fetch(monkeypatch) -> None:
    watcher = XDamageWatcher(display=":99")
    events: list[str] = []

    monkeypatch.setattr(watcher, "start", lambda: None)
    monkeypatch.setattr(watcher, "_next_damage_rects", lambda: ())
    monkeypatch.setattr(
        watcher,
        "_subtract_damage",
        lambda *, fetch_rects=False: events.append(f"subtract:{fetch_rects}") or (),
    )
    monkeypatch.setattr(watcher, "_sync", lambda: events.append("sync"))
    watcher._x11 = object()  # type: ignore[assignment]
    watcher._display = 1
    watcher._event_base = 1

    result = watcher.wait(timeout_ms=100)

    assert result.detected is True
    assert result.dirty_rect is None
    assert result.dirty_rects == ()
    assert events == ["subtract:False", "sync"]
