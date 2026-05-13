from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(token="dev")
rec = computer.recordings.start(name="demo", fps=12)
stopped = computer.recordings.stop(rec.id)
print(
    {
        "id": stopped.id,
        "status": stopped.status,
        "format": stopped.format,
        "size_bytes": stopped.size_bytes,
        "duration_seconds": stopped.duration_seconds,
        "sha256": stopped.sha256,
    }
)
