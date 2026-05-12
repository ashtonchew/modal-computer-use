from modal_computer_use import ComputerSandbox

computer = ComputerSandbox.local(token="dev")
computer.wait_until_ready()
computer.mouse.move(200, 200)
computer.mouse.click()
computer.keyboard.type("hello")
shot = computer.screenshots.full(show_cursor=True)
print(shot.width, shot.height, shot.sha256)
