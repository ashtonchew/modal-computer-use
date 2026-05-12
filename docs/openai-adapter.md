# OpenAI Adapter

`OpenAIAdapter` translates OpenAI-style computer-use action JSON into the core [action schema](glossary.md#action-schema). It does not call the OpenAI API.

## Supported actions

`click`, `double_click`, `scroll`, `type`, `keypress`, `drag`, `move`, `wait`, `screenshot`.

## Example

```python
from modal_computer_use import ComputerSandbox
from modal_computer_use.adapters.openai import OpenAIAdapter

computer = ComputerSandbox.local(token="dev")
computer.wait_until_ready()

adapter = OpenAIAdapter(computer)
adapter.apply({"type": "click", "x": 500, "y": 300, "button": "left"})
adapter.apply({"type": "type", "text": "hello"})
```

## Coordinate spaces

If you downscaled a 1440×900 desktop screenshot to 720×450 before sending it to the model, pass a [`CoordinateSpace`](glossary.md#coordinatespace) so the adapter translates model coordinates back to the desktop grid. The adapter never silently rescales.

```python
from modal_computer_use import CoordinateSpace

space = CoordinateSpace(
    desktop_width=1440, desktop_height=900,
    image_width=720, image_height=450,
)
adapter = OpenAIAdapter(computer, coordinate_space=space)
```
