from modal_computer_use import ComputerConfig, ComputerSandbox


def main() -> None:
    computer = ComputerSandbox.create(config=ComputerConfig())
    try:
        computer.wait_until_ready()
        print(computer.status())
    finally:
        computer.terminate()
        computer.detach()


if __name__ == "__main__":
    main()
