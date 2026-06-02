from __future__ import annotations

from modal_computer_use.transports.websocket_url import daemon_websocket_url


def test_daemon_websocket_url_converts_plain_http_origin() -> None:
    assert daemon_websocket_url("http://127.0.0.1:8080", "/v1/session/hot") == (
        "ws://127.0.0.1:8080/v1/session/hot"
    )


def test_daemon_websocket_url_preserves_base_path_and_query() -> None:
    assert daemon_websocket_url(
        "https://connect.modal.run/abc123?workspace=ws&_modal_connect_token=secret",
        "/v1/observations/stream",
    ) == (
        "wss://connect.modal.run/abc123/v1/observations/stream"
        "?workspace=ws&_modal_connect_token=secret"
    )


def test_daemon_websocket_url_normalizes_slashes() -> None:
    assert daemon_websocket_url("https://example.com/prefix/", "v1/session/hot") == (
        "wss://example.com/prefix/v1/session/hot"
    )
