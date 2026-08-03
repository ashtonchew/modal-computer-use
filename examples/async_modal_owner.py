"""Create and own one Modal desktop from native async Python.

Use ``AsyncComputerSandbox.attach(...)`` when another process owns the target.
An attached context detaches on exit and never terminates that remote Sandbox.
"""

from __future__ import annotations

import asyncio

from modal_computer_use import AsyncComputerSandbox, ComputerConfig


async def run() -> None:
    async with AsyncComputerSandbox.create(config=ComputerConfig()) as computer:
        await computer.mouse.move(100, 120)
        screenshot = await computer.screenshots.full(show_cursor=True)
        print(screenshot.width, screenshot.height, screenshot.sha256)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
