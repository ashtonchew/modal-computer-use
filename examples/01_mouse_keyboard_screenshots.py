from modal_computer_use import ComputerSandbox


def main() -> None:
    computer = ComputerSandbox.local(token="dev")
    try:
        computer.wait_until_ready()
        computer.mouse.move(200, 200)
        computer.mouse.click()
        computer.keyboard.type("hello")
        shot = computer.screenshots.full(show_cursor=True)
        print(shot.width, shot.height, shot.sha256)
    finally:
        computer.detach()


if __name__ == "__main__":
    main()
