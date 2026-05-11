from modal_computer_use import ComputerSandbox
from modal_computer_use.adapters.openai import OpenAIAdapter

computer = ComputerSandbox.local(token="dev")
adapter = OpenAIAdapter(computer)

# Model calls are intentionally owned by user code. This is only the action side.
adapter.apply_many(
    [
        {"type": "move", "x": 100, "y": 120},
        {"type": "click", "button": "left"},
        {"type": "wait", "duration_ms": 250},
    ]
)
