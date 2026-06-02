from __future__ import annotations

from modal_computer_use.daemon.desktop.xdamage import XDamageRect, XDamageWatcher


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
