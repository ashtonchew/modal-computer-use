# OpenAI Adapter

`OpenAIAdapter` translates OpenAI-style computer-use actions into the core action schema. It does not call the OpenAI API.

Supported normalized actions include click, double click, scroll, type, keypress, drag, move, wait, and screenshot.

Pass a `CoordinateSpace` if the screenshot sent to the model was downscaled. The adapter never silently rescales provider coordinates.
