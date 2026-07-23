# Computer-use landscape

This research note compares the main computer-use products. The information is current as of
2026-07-23.

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
| [Google Gemini Computer Use](https://ai.google.dev/gemini-api/docs/computer-use) | Model provider | Interactions API, normalized coordinates, safety decisions, safety acknowledgements | A future adapter can support this protocol. This change does not add one. |
| [Daytona Computer Use](https://www.daytona.io/docs/en/computer-use/) | Desktop infrastructure | Sandbox lifecycle, mouse, keyboard, screenshots, accessibility, display and window data, recordings, VNC, and process control | This is the closest competitor for the primitive surface. The repository also uses Daytona in provider benchmarks. |
| [E2B Desktop](https://e2b.dev/docs/use-cases/computer-use) | Desktop infrastructure | Provider-neutral Linux desktop, input, screenshots, commands, and desktop stream | This is a close infrastructure competitor with a smaller desktop SDK surface. |
| [Stagehand](https://docs.stagehand.dev/v3/basics/agent) | Browser-agent framework | DOM and vision workflows for observe, act, and extract operations | It can run above browser infrastructure. It does not supply a full desktop primitive layer. |
| [Browser Use](https://docs.browser-use.com/open-source/customize/agent/all-parameters) | Browser-agent framework and cloud | DOM data, optional vision, retries, domain controls, hooks, and recordings | It can complement or replace browser-only parts of a desktop workflow. |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Browser control protocol | Accessibility snapshots and stable element references. Vision is optional. | It supplies deterministic browser control. It is not a desktop isolation boundary. |
| [Browserbase](https://docs.browserbase.com/platform/browser/observability/session-replay) | Browser infrastructure | Managed CDP browsers, profiles, proxies, live view, recording, and replay | It supplies browser infrastructure. It does not supply a general Linux desktop. |

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

Daytona is the closest reference for the complete primitive surface. It supplies desktop lifecycle,
input, screenshots, accessibility data, display data, recordings, VNC, and process controls.
`modal-computer-use` supplies a similar primitive layer on Modal Sandboxes.

The project keeps Modal orchestration in the SDK. It keeps primitive execution in the daemon. It
also adds typed action batches, artifacts, traces, replay, warm pools, hot sessions, and versioned
provider adapters.

E2B Desktop is the next closest desktop infrastructure competitor. Browser frameworks can run above
these desktop primitives when a task needs DOM or accessibility data. They do not replace the full
desktop surface for native applications or visual-only interfaces.

The repository contains measured Daytona and E2B comparisons in
[the current provider benchmark](../docs/benchmark-results-2026-07-18-current.md). Read that report
before you make performance claims.
