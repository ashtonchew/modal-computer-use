from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(base_url="http://127.0.0.1:8080", token="dev")
print(computer.status())
