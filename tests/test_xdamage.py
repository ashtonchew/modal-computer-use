from __future__ import annotations

from modal_computer_use.daemon.desktop.xdamage import XDamageWatcher


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
    watcher = XDamageWatcher(display=":99")
    events: list[str] = []

    monkeypatch.setattr(watcher, "start", lambda: events.append("start"))
    monkeypatch.setattr(watcher, "_next_damage_event", lambda: True)
    monkeypatch.setattr(watcher, "_subtract_damage", lambda: events.append("subtract"))
    monkeypatch.setattr(watcher, "_sync", lambda: events.append("sync"))
    watcher._x11 = object()  # type: ignore[assignment]
    watcher._display = 1
    watcher._event_base = 1

    result = watcher.wait(timeout_ms=100)

    assert result.available is True
    assert result.detected is True
    assert events == ["start", "subtract", "sync"]
