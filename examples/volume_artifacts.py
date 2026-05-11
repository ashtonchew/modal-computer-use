from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(token="dev")
info = computer.artifacts.write_bytes("downloads/example.txt", b"hello\n", "text/plain")
print(info.uri)
print(computer.artifacts.sync())
