"""Acquire one named Modal desktop from native async Python.

The process owns a Sandbox it creates and only attaches to an existing named
Sandbox. Use ``detach()`` before exit to keep a newly created Sandbox running.
"""

from __future__ import annotations

import asyncio

from modal_computer_use import AsyncComputerSandbox, ComputerConfig


async def run(name: str = "support-desktop") -> None:
    async with AsyncComputerSandbox.attach_or_create(
        name=name,
        config=ComputerConfig(),
    ) as computer:
        screenshot = await computer.screenshots.full(show_cursor=True)
        print(computer.metadata().sandbox_id, screenshot.width, screenshot.height)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
