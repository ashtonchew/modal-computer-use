from __future__ import annotations

from modal_computer_use.models import Recording


def test_recording_stop_updates_file_metadata_and_download(test_client) -> None:
    started = Recording.model_validate(
        test_client.post("/v1/recordings", json={"name": "demo", "fps": 5}).json()
    )

    stopped = Recording.model_validate(
        test_client.post(f"/v1/recordings/{started.id}/stop").json()
    )

    assert stopped.status == "stopped"
    assert stopped.size_bytes > 0
    assert stopped.sha256
    assert stopped.duration_seconds is not None
    download = test_client.get(f"/v1/recordings/{started.id}/download")
    assert download.status_code == 200
    assert download.content.startswith(b"mock recording")


def test_recording_delete_removes_metadata(test_client) -> None:
    started = test_client.post("/v1/recordings", json={}).json()

    assert test_client.delete(f"/v1/recordings/{started['id']}").json() == {"ok": True}
    assert test_client.get(f"/v1/recordings/{started['id']}").status_code == 404
