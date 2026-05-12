from __future__ import annotations

from modal_computer_use.models import Screenshot


def test_health_version_capabilities(test_client) -> None:
    assert test_client.get("/healthz").json() == {"ok": True}
    assert test_client.get("/readyz").json()["ready"] is True
    assert test_client.get("/v1/version").json()["api_version"] == "v1"
    caps = test_client.get("/v1/capabilities").json()
    assert "mouse" in caps["primitives"]


def test_status_and_screenshot(test_client) -> None:
    status = test_client.get("/v1/computer/status").json()
    assert status["ready"] is True
    response = test_client.post("/v1/screenshots/full", json={"format": "png", "show_cursor": True})
    shot = Screenshot.model_validate(response.json())
    assert shot.width == 1440
    assert shot.coordinate_space.desktop_width == 1440
    assert shot.sha256


def test_clipboard_and_release_all(test_client) -> None:
    assert test_client.put("/v1/clipboard/text", json={"text": "secret"}).json()["ok"] is True
    assert test_client.get("/v1/clipboard/text").json()["text"] == "secret"
    assert test_client.delete("/v1/clipboard/text").json()["ok"] is True
    test_client.post("/v1/mouse/down", json={"button": "left"})
    released = test_client.post("/v1/input/release-all").json()
    assert released["ok"] is True
    assert "left" in released["output"]["buttons"]
