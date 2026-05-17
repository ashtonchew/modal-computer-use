from modal_computer_use import ComputerConfig, ComputerSandbox
from modal_computer_use.adapters.openai import OpenAIAdapter

# OpenAI's computer-use guidance observes strong performance around 1440x900/1600x900.
computer = ComputerSandbox.create(config=ComputerConfig(desktop={"resolution": (1440, 900)}))
adapter = OpenAIAdapter(computer)

# Model calls are intentionally owned by user code. This is only the action side.
adapter.apply_many(
    [
        {"type": "move", "x": 100, "y": 120},
        {"type": "click", "button": "left"},
        {"type": "wait", "duration_ms": 250},
    ]
)
