# Computer-use providers and competitors

This comparison was refreshed against official documentation on 2026-07-23. Categories matter:
model providers define model/tool protocols, browser-agent frameworks orchestrate browser tasks,
and infrastructure products supply the remote environment.

| Product | Category | Canonical integration | Relationship to this project |
| --- | --- | --- | --- |
| [OpenAI Computer use](https://developers.openai.com/api/docs/guides/tools-computer-use) | Model/provider tool | Responses API, `gpt-5.6`, GA `computer` tool, ordered batched actions, original-detail screenshot feedback | Direct provider adapter and cookbook |
| [Anthropic Computer use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool) | Model/provider tool | Messages API beta, `computer_20251124`, exact display dimensions, matched `tool_result` blocks | Direct provider adapter and cookbook |
| [Google Gemini Computer Use](https://ai.google.dev/gemini-api/docs/computer-use) | Model/provider tool | Interactions API, normalized coordinates, explicit safety decisions and acknowledgements | Potential future adapter; intentionally outside this PR |
| [Stagehand](https://docs.stagehand.dev/v3/basics/agent) | Browser-agent framework | DOM/vision observe-act-extract workflows with bounded agents and caching | Browser-only orchestration that can complement or overlap |
| [Browser Use](https://docs.browser-use.com/open-source/customize/agent/all-parameters) | Browser-agent framework/cloud | DOM plus selective vision, bounded retries, domain-scoped secrets, hooks and recordings | Browser-only orchestration that can complement or overlap |
| [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Browser harness/protocol | Accessibility snapshots and deterministic element references; vision is opt-in | Adjacent deterministic browser control, not a security boundary |
| [E2B Desktop](https://e2b.dev/docs/use-cases/computer-use) | Desktop infrastructure | Provider-neutral Linux desktop with screenshot/input/command/VNC primitives | Closest infrastructure competitor |
| [Browserbase](https://docs.browserbase.com/platform/browser/observability/session-replay) | Managed browser infrastructure | CDP browsers, profiles, proxies, live view, recording and replay | Browser infrastructure rather than a full Linux desktop |

## Canonical common denominator

Both provider cookbooks now:

- keep provider-owned model loops outside core;
- run in an isolated desktop and fail closed on unknown actions;
- execute ordered action batches and return a verification screenshot;
- preserve exact screenshot/coordinate-space dimensions;
- bound turns, actions, and time;
- support an application-owned `before_action` policy and human confirmation;
- treat screen and page content as untrusted;
- preserve redacted traces, recordings, and artifacts for debugging.

`modal-computer-use` is positioned closest to E2B Desktop, with a daemon-first typed HTTP/local SDK,
Modal-native orchestration, provider-neutral versioned adapters, action batching, artifacts,
recordings, traces/replay, warm pools, and hot sessions. Browser frameworks can sit above these
primitives when a browser-specific DOM/accessibility path is preferable to full visual desktop
control.
