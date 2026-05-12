"""Example-only Anthropic server shape.

This file deliberately avoids importing Anthropic at module import time. User
applications own provider credentials, prompts, model calls, domain policy, and
confirmation UI. The server only shows how provider-returned tool inputs pass
through the adapter contract.
"""

from modal_computer_use.adapters.anthropic import AnthropicAdapter, anthropic_tool_result


class AnthropicMessageServer:
    def __init__(self, computer: object) -> None:
        self.adapter = AnthropicAdapter(computer, tool_version="computer_20250124")

    def apply_tool_use(self, tool_input: dict) -> object:
        return self.adapter.apply(tool_input)

    def apply_tool_use_block(self, tool_use: dict) -> dict:
        result = self.adapter.apply(tool_use["input"])
        return anthropic_tool_result(tool_use_id=tool_use["id"], result=result)
