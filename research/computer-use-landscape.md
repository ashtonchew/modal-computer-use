# Computer-use landscape

This research note compares selected computer-use products that are relevant to this project. The
information is current as of 2026-07-23.

The products have different roles:

- A model provider defines a model and its tool protocol.
- A browser-agent framework controls browser tasks.
- A browser infrastructure service supplies managed browser sessions.
- A desktop infrastructure service supplies a remote graphical computer.

## Product comparison

| Product | Role | Current interface | Relationship to this project |
| --- | --- | --- | --- |
| [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use) | Model provider | Responses API, `gpt-5.6`, `computer` tool, ordered action batches, screenshot results | The project supplies an adapter and a cookbook for this protocol. |
| [Anthropic Computer use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool) | Model provider | Messages API beta, `computer_20251124`, exact display dimensions, matched `tool_result` blocks | The project supplies an adapter and a cookbook for this protocol. |
| [Google Gemini Computer Use](https://ai.google.dev/gemini-api/docs/computer-use) | Model provider | Interactions API, `computer_use` tool, browser, mobile, and desktop environments, normalized 1000×1000 coordinates, safety decisions, and acknowledgements | A future adapter can support this protocol. This change does not add one. |
| [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/) | Desktop infrastructure | Sandbox lifecycle, mouse, keyboard, screenshots, accessibility, display and window data, recordings, VNC, and process control | This is the closest competitor for the primitive surface. The repository also uses Daytona in provider benchmarks. |
| [Scrapybara](https://docs.scrapybara.com/introduction/) | Desktop infrastructure | Ubuntu, Windows, and browser instances, interactive streams, mouse, keyboard, screenshots, Bash, and files | This is a direct primitive-layer competitor with a provider-neutral action surface. |
| [E2B Desktop](https://e2b.dev/docs/use-cases/computer-use) | Desktop infrastructure | Desktop SDK centered on Linux lifecycle, mouse, keyboard, screenshots, commands, and VNC streaming | This is a close desktop-infrastructure competitor. |
| [Stagehand](https://docs.stagehand.dev/v3/basics/agent) | Browser-agent framework | AI-powered act, observe, and extract operations, plus DOM and hybrid-vision agent modes | It can run above browser infrastructure. It does not supply a full desktop primitive layer. |
| [Browser Use](https://docs.browser-use.com/open-source/customize/agent/all-parameters) | Browser-agent framework and cloud | DOM data, optional vision, retries, domain controls, hooks, and recordings | It can complement or replace browser-only parts of a desktop workflow. |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Browser-control MCP server | Accessibility snapshots and stable element references. Vision is optional. | It supplies deterministic browser control. It is not a desktop isolation boundary. |
| [Browserbase](https://docs.browserbase.com/platform/browser/getting-started/remote-browser-versus-local-browser) | Browser infrastructure | Managed CDP browsers, profiles, proxies, live view, recording, and replay | It supplies browser infrastructure. It does not supply a general Linux desktop. |

## Common integration requirements

The OpenAI and Anthropic cookbooks use the same application controls:

- Keep the provider model loop outside the core package.
- Run the desktop in an isolated environment.
- Reject unknown actions.
- Run actions in the order that the provider returns.
- Return a screenshot after graphical actions.
- Keep the screenshot dimensions and coordinate space consistent.
- Set limits for turns, actions, action time, and total time.
- Apply an application policy before each action.
- Ask a person to confirm actions that have important external effects.
- Treat page and screen content as untrusted input.
- Remove secrets from traces, recordings, and artifacts.

## Project position

Daytona is one of the closest references for the complete primitive surface. It supplies desktop
lifecycle, input, screenshots, accessibility data, display data, recordings, VNC, and process
controls. `modal-computer-use` supplies a similar primitive layer on Modal Sandboxes.

The project keeps Modal orchestration in the SDK. It keeps primitive execution in the daemon. It
also adds typed action batches, artifacts, traces, replay, warm pools, hot sessions, and versioned
provider adapters.

Scrapybara and E2B Desktop are also close desktop-infrastructure competitors. Browser frameworks can
run above these desktop primitives when a task needs DOM or accessibility data. Browser frameworks
do not replace the full desktop surface for native applications or visual-only interfaces.

The repository contains measured Modal, Daytona, E2B, and Tzafon comparisons in the
[current provider report](../docs/benchmark-results-2026-07-26-provider-results.md). Read its
measurement boundaries before making performance claims.
