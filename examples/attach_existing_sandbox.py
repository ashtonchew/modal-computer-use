from modal_computer_use import ComputerSandbox


def main(sandbox_id: str) -> None:
    computer = ComputerSandbox.attach(sandbox_id=sandbox_id)
    computer.wait_until_ready()
    print(computer.status())
