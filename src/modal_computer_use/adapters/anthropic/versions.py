from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnthropicToolVersion:
    name: str
    beta_header: str
    supports_enhanced_actions: bool
    supports_zoom: bool


ANTHROPIC_TOOL_VERSIONS: dict[str, AnthropicToolVersion] = {
    "computer_20241022": AnthropicToolVersion(
        name="computer_20241022",
        beta_header="computer-use-2024-10-22",
        supports_enhanced_actions=False,
        supports_zoom=False,
    ),
    "computer_20250124": AnthropicToolVersion(
        name="computer_20250124",
        beta_header="computer-use-2025-01-24",
        supports_enhanced_actions=True,
        supports_zoom=False,
    ),
    "computer_20251124": AnthropicToolVersion(
        name="computer_20251124",
        beta_header="computer-use-2025-11-24",
        supports_enhanced_actions=True,
        supports_zoom=True,
    ),
}


def get_tool_version(name: str) -> AnthropicToolVersion:
    try:
        return ANTHROPIC_TOOL_VERSIONS[name]
    except KeyError as exc:
        raise ValueError(f"unsupported Anthropic computer tool version: {name}") from exc
