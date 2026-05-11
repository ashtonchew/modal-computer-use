from __future__ import annotations


def test_sdk_local_client_calls_daemon(computer) -> None:
    computer.wait_until_ready(timeout=1)
    assert computer.status().ready is True
    assert computer.mouse.move(10, 20).x == 10
    assert computer.mouse.position().y == 20
    computer.clipboard.set_text("hello")
    assert computer.clipboard.get_text() == "hello"
    shot = computer.screenshots.region(0, 0, 100, 80)
    assert shot.width == 100
