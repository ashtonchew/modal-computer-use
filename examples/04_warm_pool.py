"""Sketch of a warm-pool strategy.

Production apps can keep Modal sandbox IDs in a queue and attach with
ComputerSandbox.attach(sandbox_id=...). The core package intentionally does not
run a queue service.
"""

from modal_computer_use import ComputerSandbox


def attach_ready_sandbox(sandbox_id: str) -> ComputerSandbox:
    return ComputerSandbox.attach(sandbox_id=sandbox_id)
