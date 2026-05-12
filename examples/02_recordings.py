from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(token="dev")
rec = computer.recordings.start(name="demo", fps=12)
stopped = computer.recordings.stop(rec.id)
print(stopped)
