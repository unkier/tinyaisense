# TCP control plus UDP streams, raw framing

Each device keeps one raw TCP connection to the hub. It carries JSON control messages (hello, commands, replies, events, ping) and bulk command payloads, framed by a 4-byte header. Stream frames (mic and speaker audio, later video) travel as UDP datagrams, and button events are mirrored over UDP as well. We chose this because lowest latency is the top priority.
- **TCP vs UDP on a clean LAN:** they measured the same in the loopback spike.
- **Under WiFi loss:** a lost TCP segment stalls everything queued behind it until retransmit. lwIP's retransmit timer is coarse, so this can take hundreds of ms. A lost UDP frame is only a 20 ms gap.
- **Stream boundaries:** start, stop and drain stay authoritative on TCP, so a lost datagram never loses a boundary.

## Considered Options

- **One WebSocket** (JSON text + binary frames, as in xiaozhi). Pros: browser devices and `wss://` for free. Cons: over TCP it's no faster than raw TCP, it has the same loss stalls, and ESP-IDF's `esp_websocket_client` needs workarounds to be fast (a lock that can stall sends for seconds, Nagle delays from split header/payload writes). Browser devices can come later through a hub-side gateway that carries streams inside the control connection (TCP frame type 3 is reserved for this).
- **One TCP connection for everything** (ESPHome, and our spike). Simplest, and ESPHome dropped its UDP audio path for it. We rejected it anyway: under loss, audio and STOP/button events queue behind a retransmit.
- **MQTT + UDP:** needs a broker. **WebRTC:** needs an ESP32-S3 with PSRAM.

## Consequences

- **Loss and reordering:** receivers must handle lost and reordered stream frames. A `seq` gap means loss. Stale frames are recognised by their `stream` id.
- **Button events** are sent over TCP and also twice over UDP. The hub dedupes them by event `seq`.
- **Encryption later:** TLS protects only the TCP connection. UDP frames will need their own per-packet encryption, keyed over the control connection.
