# MCP push and interrupt: spec vs. real clients

Research for [#4](https://github.com/unkier/tinyaisense/issues/4). Checked **2026-09-26**.

**Question:** can a button-press **Event** on a **Device** reach an **Agent** that only talks MCP to the **Hub**, and change what the Agent does mid-loop? Or does an "instant interrupt" need the agent API?

## Bottom line

- **An MCP-only Agent cannot be interrupted by the Hub.** The current spec, **2026-07-28**, does not let a server push content into the model's context. A server can only send these unprompted:
  - change notifications (`*/list_changed`, and `resources/updated`, which carries only a URI), on a `subscriptions/listen` stream the client opts into;
  - progress and log notifications, but only inside a request the client already made.

  No client surveyed puts any of these into the model's context. The only effect that reaches the model at all is `tools/list_changed`: some clients re-list tools, and the model sees the new tool definitions on its next call.
- **Over MCP, the best we get is delivery at the next tool-call boundary.** Three patterns work:
  1. **Blocking wait tool.** The Agent calls something like `wait_for_event(timeout)`, which blocks until an Event arrives and returns it as the tool result. This works in every client.
  2. **Piggyback.** Append any pending Events to every tool result the Hub returns.
  3. **Claude Code channels.** A Claude Code-only extension, in research preview, stdio only. Events are queued until the next turn.

  None of these stop a generation or a tool call that is already running.
- **An instant interrupt needs the agent API.** Agent-side code must own the agent loop, receive Events from the Hub over a persistent connection, and call the SDK's interrupt method, then inject the Event as a new user message. Examples:
  - Claude Agent SDK: `interrupt()` plus streaming input.
  - OpenAI Agents SDK: `RunResultStreaming.cancel()`, or the Realtime agent's `session.interrupt()`.
  - Codex app-server: `turn/interrupt` / `turn/steer`.

## 1. What the spec lets a server push

Spec revisions:

| Revision | Date | Status |
|---|---|---|
| **2026-07-28** | 2026-07-28 | current (RC was 2026-05-29) |
| 2025-11-25 | 2025-11-25 | previous; many clients still use it |
| 2025-06-18 | 2025-06-18 | older |
| 2025-03-26 | 2025-03-26 | older |

Sources: [versioning][versioning], [repo releases][releases].

| Feature | 2025-11-25 | 2026-07-28 (current) | Carries content for the model? |
|---|---|---|---|
| `notifications/{tools,prompts,resources}/list_changed` | Sent any time, on the standalone HTTP GET SSE stream or on stdio | Sent only on a `subscriptions/listen` stream the client opened, filtered to the types it asked for. The GET endpoint is **removed** | No. It is a cue to re-list. |
| Resource subscriptions / `notifications/resources/updated` | `resources/subscribe` | `resources/subscribe` is removed. Clients list resource URIs in `resourceSubscriptions` on `subscriptions/listen` | No. Carries only `uri`. The client must call `resources/read` itself, and resources are "application-driven": the host decides what reaches the model ([resources][resources]). |
| `notifications/message` (logging) | Server-wide, level set with `logging/setLevel` | **Deprecated**. Sent only for a request that set `io.modelcontextprotocol/logLevel` in its `_meta` | No. The spec defines no interaction model for it. |
| `notifications/progress` | Tied to a request's `progressToken` | Unchanged, but flows only on that request's own response stream | No (progress/total/message fields for display). |
| Elicitation (form and URL modes) | A server-initiated `elicitation/create` request | Server-initiated requests are **removed**. A tool returns an `InputRequiredResult` (MRTR) and the client retries the call with the answers | It is a question to the **user**, and only possible inside a request the client made. |
| Sampling | A server-initiated `sampling/createMessage` request | **Deprecated**, and now also sent via MRTR | It is a separate completion that runs nested inside the server's request. It does not touch the Agent's conversation. |
| Audio in tool results | `{"type":"audio","data":…,"mimeType":…}` | Unchanged ([tools][tools]) | Yes, if the client forwards it (see §2). |
| Transports | stdio; Streamable HTTP with a GET stream for server-initiated messages, resumable with `Last-Event-ID` | stdio; Streamable HTTP is POST-only. Each response is JSON or a request-scoped SSE stream. `subscriptions/listen` is the only long-lived stream. No resumability. Server→client *requests* are forbidden on both transports | n/a |
| Cancellation | `notifications/cancelled` | stdio: `notifications/cancelled`. HTTP: close the response stream. Only the **client** can cancel. | n/a |
| Tasks (long-running) | Experimental and part of the core protocol | Moved out of the core into the **`io.modelcontextprotocol/tasks` extension**. The client polls `tasks/get` and sends input with `tasks/update`. Status pushes (`notifications/tasks`) are optional and go through `subscriptions/listen`. | No. The client polls for the result. |

Sources: [changelog][changelog], [deprecated][deprecated], [subscriptions][subscriptions], [Streamable HTTP][shttp], [stdio][stdio], [MRTR][mrtr], [tasks ext][tasks], [GA post][ga].

**Conclusion for §1:** the spec gives the server no primitive that means "tell the model this now". Every server→model path goes through something the client decides to do: calling a tool, reading a resource, or refreshing tools. In the 2026-07-28 revision, a server cannot even start a user interaction outside a request.

## 2. What real clients do with each feature

The MCP client feature matrix at modelcontextprotocol.io/clients was **deleted on 2026-05-27**; the URL now redirects to the intro page. Its last version is [87993a6][matrix]. Everything below comes from vendor docs, changelogs and source code.

Legend:

| Code | Meaning |
|---|---|
| **M** | reaches the model's context |
| **refresh** | re-lists tools; the model sees new definitions on its next call |
| **UI** | shown to the user or written to logs only |
| **app** | handed to the host application's code only |
| **N** | not supported |
| **?** | no primary source found |
| **(UR)** | only a user-reported GitHub issue |

### 2a. Notifications and subscriptions

| Client | list_changed | resources/updated | logging | progress |
|---|---|---|---|---|
| Claude Code (CLI) | refresh (since 2.1.0) | N (no docs or changelog entry) | ? | UI (tool row; resets the idle timeout) |
| Claude Desktop (chat) | N (UR claude-ai-mcp#960) | N | ? | ? |
| claude.ai remote connectors | N (UR #1044) | N (docs: "Resource subscriptions" unsupported) | ? | N (UR #804) |
| Claude Messages API MCP connector | n/a: tools fetched per request | N (tools only) | N | N |
| Claude Agent SDK | external servers: same as the CLI. In-process `create_sdk_mcp_server` (Python) drops all notifications | same as CLI | same as CLI | host code gets only a `tool_progress` elapsed-time heartbeat |
| OpenAI Agents SDK (Py/JS) | app via `message_handler`. Tools are re-listed each run unless `cache_tools_list` is set | N (app-only `list_resources`/`read_resource`) | app | app |
| OpenAI Responses hosted MCP | tool list cached as an `mcp_list_tools` item | N | N | N |
| Codex CLI | UI (trace log only, no re-list) | ? (resources exposed as tools the model can call) | UI | UI |
| Cursor (closed source) | undocumented | Resources yes (1.6); subscribe undocumented | UI ("MCP Logs") | undocumented |
| VS Code Copilot | refresh | subscribes to watched `mcp-resource:` files; refreshes the file, not the running turn | UI | UI |
| Gemini CLI | refresh (tools, resources, prompts) | N | N | UI |
| Goose | refresh (tools only) | N | UI | UI |

### 2b. Interaction and content

| Client | Elicitation | Sampling | Audio in tool result |
|---|---|---|---|
| Claude Code | form (2.1.76) and URL (2.1.281, 2026-07-28 connections only). The user answers; an `Elicitation` hook can answer automatically | N (FR claude-code#1785 open) | saved to disk; the model gets only the **file path** |
| Claude Desktop | N (UR claude-ai-mcp#153) | N | ? |
| claude.ai connectors | N | N (docs) | N (docs: text and image results only) |
| Claude Agent SDK | TS: `onElicitation` callback. Python in-process servers refuse it | N | TS: saved to disk, path given. Py: dropped |
| OpenAI Agents SDK | N | N | sent as a JSON **text** string, not audio |
| Responses hosted MCP | N (only tool approvals, via `mcp_approval_request`) | N | ? |
| Codex CLI | form and URL, answered by the user | N | **M** (converted to `InputAudio`) |
| Cursor | yes (1.5) | N | ? |
| VS Code Copilot | form and URL | yes (`chat.mcp.serverSampling`) | passed on as a data part; whether the model accepts it is ? |
| Gemini CLI | N | N | **M** (Gemini `inlineData`) |
| Goose | form and URL | removed 2026-09-22 | dropped |

### 2c. Transports and long-running tools

| Client | Transports | Timeouts / cancel / tasks |
|---|---|---|
| Claude Code | stdio, Streamable HTTP, SSE (deprecated), WebSocket. Its v2 runtime keeps `subscriptions/listen` open to 2026-07-28 servers | `MCP_TOOL_TIMEOUT` (long default). Idle timeout 5 min (HTTP) / 30 min (stdio). Calls longer than 2 min are auto-backgrounded, and the result reaches the model later as a task notification. Tasks: N |
| claude.ai / Desktop remote | Streamable HTTP, legacy SSE. No stdio on claude.ai | 240 s per call. Tasks N |
| OpenAI Agents SDK | stdio, Streamable HTTP, SSE | `client_session_timeout_seconds` (5 s default). JS: an AbortSignal sends `notifications/cancelled`. Tools that require tasks are rejected |
| Codex CLI | stdio, Streamable HTTP. 2026-07-28 only behind the `mcp_2026_07_28` flag | startup 10 s, tool call **60 s** by default. Tasks N |
| VS Code Copilot | stdio, Streamable HTTP (falls back to SSE) | cancel token sends `notifications/cancelled`. **Tasks: yes** (the only surveyed client) |
| Gemini CLI | stdio, SSE, Streamable HTTP | 600 s default. Tasks N |
| Goose | stdio, Streamable HTTP | 300 s default; sends `notifications/cancelled`. Tasks N |

## 3. Ways to push into or interrupt a running agent loop (outside core MCP)

| Client | Mechanism | What the model sees / when |
|---|---|---|
| **Claude Code** | **Channels** (research preview, 2.1.80): the server declares the capability `experimental['claude/channel']` and sends `notifications/claude/channel {content, meta}`. The user enables it with `--channels` or `--dangerously-load-development-channels server:<name>` | The model receives `<channel source=… k=v>content</channel>`. If the session is idle, a new turn starts. **If Claude is busy, events are queued and "delivered together on the next turn"**, so this is not a preemption. Limits: stdio only; plugins must be allowlisted during the preview; requires claude.ai or Console auth; not registered on 2026-07-28 connections ([channels][channels], [reference][channels-ref]) |
| Claude Code | Hooks: sync `PostToolUse`/`UserPromptSubmit` return `additionalContext`. Command hooks can set `asyncRewake: true`: they run in the background, and exit code 2 wakes Claude with the hook's stderr ([hooks][hooks]) | A system reminder, delivered at a tool boundary or as a wake-up |
| Claude Code | Monitor tool (2.1.98): the model starts a watcher, and each stdout line or WebSocket message reaches it | Mid-conversation, but only after the model chose to start the watcher. 5 min default deadline, 30 min max ([tools][cc-tools]) |
| **Claude Agent SDK** | **Streaming input** (`query({prompt: AsyncIterable})` / `ClaudeSDKClient.query()`) together with **`interrupt()`** (streaming mode only) | The host injects user messages; queued messages are picked up mid-turn. `interrupt()` stops the current turn ([streaming][sdk-streaming]) |
| **OpenAI Agents SDK** | `RunResultStreaming.cancel(mode="immediate"\|"after_turn")`; `call_model_input_filter` (runs before every model call); Realtime `session.interrupt()` / `send_message()` | Cancel the run and start a new one with the Event, or add the Event to the input before the next model call. Realtime is the only true mid-response interrupt, and it is not MCP ([result.py][oai-result]) |
| Codex (app-server) | `turn/steer` (adds input to the in-flight turn), `turn/interrupt`; `PostToolUse` hook returns `additionalContext` | Arrives at a turn or tool boundary |
| Cursor | Queued messages run "at the next ideal time, usually after a tool call". Hooks: `postToolUse.additional_context`, `stop.followup_message` | At a tool boundary |
| VS Code Copilot | `chat.requestQueuing.defaultAction=steer` (waits for the current tool to finish); `PostToolUse` hook `additionalContext`. MCP Apps `ui/message` only pre-fills the chat input | At a tool boundary |
| Gemini CLI | `experimental.modelSteering`; `AfterTool`/`BeforeAgent` hooks return `additionalContext` | Next turn or tool boundary |

The pattern is the same everywhere. Mid-loop injection happens at a **tool-call boundary**, and an actual interrupt is a **host API call** (`interrupt`, `cancel`, `turn/interrupt`). Neither is reachable from a standard MCP server.

## 4. Implications for tinyaisense

1. **The MCP surface should plan for delivery at tool-call boundaries.**
   - Offer a blocking `wait_for_event`-style tool. Keep its timeout below client limits: Codex 60 s, claude.ai 240 s.
   - Attach any pending Events to every tool result, so an Agent that is busy calling other Hub tools still sees the button press on its next call.
   - Also implement resources and `subscriptions/listen` so the Hub is spec-complete. No client passes those updates to the model, so nothing should depend on them.
2. **"Instant interrupt" belongs to the agent API.**
   - The Hub pushes Events over a persistent connection to an agent-side adapter that owns the loop.
   - The adapter calls the SDK's interrupt method (`interrupt()` in the Claude Agent SDK, `cancel()` in the OpenAI Agents SDK) and resumes the loop with the Event.
   - Latency is then the network hop plus the SDK's cancel. It no longer depends on when the model next calls a tool.
3. **A Claude Code channel is an optional extra**, e.g. a small stdio shim that forwards Hub Events. It is Claude Code-only, in preview, and delivers on the next turn, so it is not a substitute for (2).
4. **Mic audio as MCP audio content is not portable.** Only Codex and Gemini CLI pass audio to the model. Claude clients save it to a file, and the OpenAI Agents SDK turns it into text.
5. **The spec is changing.**
   - 2026-07-28 removed server-initiated requests and the GET stream.
   - The Python SDK v2.x ([2.2.0][pysdk], 2026-09-07) implements `subscriptions/listen` on the server side.
   - Clients still negotiate older revisions: Claude Code channels only register on pre-2026-07-28 connections, and Codex has 2026-07-28 behind a flag. Supporting both 2025-11-25 and 2026-07-28 is therefore needed.

## Sources

**Spec (modelcontextprotocol.io / GitHub)**

- [Versioning][versioning]
- [2026-07-28 changelog][changelog]
- [Deprecated registry][deprecated]
- [Subscriptions][subscriptions]
- [Streamable HTTP][shttp]
- [stdio][stdio]
- [MRTR][mrtr]
- [Resources][resources]
- [Tools][tools]
- [Tasks extension][tasks]
- [2026-07-28 GA post][ga]
- [Spec releases][releases]
- [Last client matrix (87993a6)][matrix]
- [Python SDK subscriptions docs][pysdk-subs]

**Anthropic**

- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Channels][channels]
- [Channels reference][channels-ref]
- [Hooks][hooks]
- [Tools reference (Monitor)][cc-tools]
- [Claude Code CHANGELOG](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md): 2.1.0 list_changed, 2.1.76 elicitation, 2.1.80 channels, 2.1.98 Monitor, 2.1.212 auto-background, 2.1.281 URL elicitation; MCP audio/binary content saved to disk
- [Agent SDK streaming input][sdk-streaming]
- [Agent SDK TS reference](https://code.claude.com/docs/en/agent-sdk/typescript)
- [Agent SDK Python in-process MCP bridge](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/sdk_mcp_bridge.py)
- [Connectors (claude.ai/Desktop)](https://claude.com/docs/connectors/building)
- [Messages API MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector)

**OpenAI**

- [Agents SDK Python MCP server.py](https://github.com/openai/openai-agents-python/blob/main/src/agents/mcp/server.py)
- [MCP docs](https://github.com/openai/openai-agents-python/blob/main/docs/mcp.md)
- [result.py cancel][oai-result]
- [Agents SDK JS MCP](https://github.com/openai/openai-agents-js/blob/main/packages/agents-core/src/shims/mcp-server/node.ts)
- [Responses remote MCP](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
- Codex [models.rs (audio)](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/models.rs)
- Codex [rmcp logging handler](https://github.com/openai/codex/blob/main/codex-rs/rmcp-client/src/logging_client_handler.rs)
- Codex [app-server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/common.rs)

**Others**

- Cursor: [MCP docs](https://cursor.com/docs/context/mcp), [hooks](https://cursor.com/docs/hooks), changelog [1.4](https://cursor.com/changelog/1-4), [1.5](https://cursor.com/changelog/1-5), [1.6](https://cursor.com/en/changelog/1-6)
- VS Code: [mcpServerRequestHandler.ts](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/mcp/common/mcpServerRequestHandler.ts), [MCP config reference](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration), [hooks](https://code.visualstudio.com/docs/copilot/customization/hooks)
- Gemini CLI: [mcp-client.ts](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/tools/mcp-client.ts), [mcp-tool.ts](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/tools/mcp-tool.ts), [model steering](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/model-steering.md)
- Goose: [mcp_client.rs](https://github.com/block/goose/blob/main/crates/goose/src/agents/mcp_client.rs), [sampling removal](https://github.com/aaif-goose/goose/commit/7b68b47ffd6cfe2ffc52342e19b6fd03ec32b609)

**Caveats**

- Cursor is closed source and its docs say nothing about notifications or progress.
- Claims marked (UR) rest on user-reported issues in anthropics/claude-ai-mcp and anthropics/claude-code, not vendor docs.
- Claude Desktop logging and progress handling is unverified.

[versioning]: https://modelcontextprotocol.io/specification/versioning
[changelog]: https://modelcontextprotocol.io/specification/2026-07-28/changelog
[deprecated]: https://modelcontextprotocol.io/specification/2026-07-28/deprecated
[subscriptions]: https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions
[shttp]: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
[stdio]: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio
[mrtr]: https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr
[resources]: https://modelcontextprotocol.io/specification/2026-07-28/server/resources
[tools]: https://modelcontextprotocol.io/specification/2026-07-28/server/tools
[tasks]: https://github.com/modelcontextprotocol/ext-tasks
[ga]: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/blog/content/posts/2026-07-28-spec-ga/index.md
[releases]: https://github.com/modelcontextprotocol/modelcontextprotocol/releases
[matrix]: https://github.com/modelcontextprotocol/modelcontextprotocol/blob/87993a68166cfec740ca4d6a99cbe661f7aed32c/docs/clients.mdx
[pysdk]: https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0
[pysdk-subs]: https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/handlers/subscriptions.md
[channels]: https://code.claude.com/docs/en/channels
[channels-ref]: https://code.claude.com/docs/en/channels-reference
[hooks]: https://code.claude.com/docs/en/hooks
[cc-tools]: https://code.claude.com/docs/en/tools-reference#monitor-tool
[sdk-streaming]: https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode
[oai-result]: https://github.com/openai/openai-agents-python/blob/main/src/agents/result.py
