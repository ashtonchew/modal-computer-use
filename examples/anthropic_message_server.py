"""Example-only Anthropic server shape.

This file deliberately avoids importing Anthropic at module import time. User
applications own provider credentials and model calls.
"""

from modal_computer_use.adapters.anthropic import AnthropicAdapter


class AnthropicMessageServer:
    def __init__(self, computer: object) -> None:
        self.adapter = AnthropicAdapter(computer, tool_version="computer_20250124")

    def apply_tool_use(self, tool_input: dict) -> object:
        return self.adapter.apply(tool_input)
