"""Recording lifecycle example with safe output.

The recording artifact URI and local paths can reveal run internals, so this
example reports only bounded metadata.
"""

from pathlib import Path

from modal_computer_use import ComputerSandbox


def main() -> None:
    computer = ComputerSandbox.local(token="dev")
    computer.wait_until_ready()

    recording = computer.recordings.start(name="demo", fps=12)
    computer.mouse.move(50, 50)
    stopped = computer.recordings.stop(recording.id)
    recordings = computer.recordings.list()
    downloaded = computer.recordings.download(stopped.id, Path("artifacts/demo-recording.mp4"))

    print(
        {
            "recording_id": stopped.id,
            "status": stopped.status,
            "format": stopped.format,
            "size_bytes": stopped.size_bytes,
            "downloaded": downloaded.exists(),
            "recording_count": len(recordings),
        }
    )


if __name__ == "__main__":
    main()
