# The agent API is one WebSocket, unlike the device side

Agents talk to the hub over one WebSocket (`/agent`). JSON-RPC 2.0 goes in text frames and audio in binary frames, one 10 ms frame per message. Devices use raw TCP plus UDP (ADR 0002), and we still chose this for agents: here it is just as fast, and much easier to use.
- **Loss recovery:** ADR 0002 moved device streams to UDP because lwIP's coarse retransmit timer stalls TCP for hundreds of ms. Agent hosts run desktop TCP stacks (RACK/TLP) that recover a loss in about 2 RTTs, so UDP buys almost nothing even for an agent on WiFi.
- **Localhost/Ethernet:** WebSocket, raw TCP and UDP measure the same.
- **The next hop is TCP anyway:** agents reach their cloud LLM, or OpenAI Realtime, over TCP/WebSocket.
- **One ordered connection:** Events, stream ends and replies arrive in the order the hub sent them, with no cross-channel reordering.
- **Clients:** every language has a WebSocket client, and a browser debug UI can connect.

## Considered Options

- **The device protocol (TCP control + UDP audio).** It wins only under loss on a lwIP-class stack. It costs a custom client in every language and rules out browsers.
- **gRPC bidi streaming.** Codegen and HTTP/2 flow control, with no latency gain.
- **HTTP + SSE.** Audio upload is awkward, and events and replies arrive on separate connections, so their order is lost.

## Consequences

- **Tuning is part of the spec, not an afterthought:** `compression=None`, a low `write_limit` and TCP_NODELAY.
- **The hub never applies TCP backpressure to agents:** it always reads agent sockets eagerly and buffers speaker audio itself (capped at 5 min per stream), so a `stop` never queues behind unread audio.
- **Auth goes in the first message** (`hello`), because browsers can't set WebSocket headers.
