# Audio streams

Mic audio flows up and speaker audio flows down as UDP stream frames (datagram kind 1, see [`device.md`](device.md)). Start, stop and drain are Commands on TCP, so a lost datagram never loses a stream boundary.

## Format

A format is an object:

```json
{"codec":"pcm_s16le","rate":16000,"channels":1,"frame_ms":10}
```

- `pcm_s16le` is signed 16-bit little-endian PCM, channels interleaved. It's the only codec in the MVP.
- A capability advertises its formats in `params.formats`, with `frame_ms` as a list of the frame durations it supports. The Atom Echo advertises the same format for its mic and speaker: `{"codec":"pcm_s16le","rate":16000,"channels":1,"frame_ms":[10,20]}`.
- `start{stream, format}` picks one advertised format with a single `frame_ms`. The reply echoes the chosen format. A format the capability doesn't advertise gets `invalid_args`.
- The hub uses 10 ms frames in both directions: 320 bytes of payload, 100 datagrams per second.

## Frames

- `frame_samples = rate × frame_ms / 1000`, and `frame_bytes = frame_samples × channels × 2`.
- Each stream datagram carries one frame. `seq` starts at 0 for each stream and counts frames, so the frame's first sample sits at position `seq × frame_samples`.
- Every frame is exactly `frame_bytes` long, except **the final frame, which may be short**. It must still hold at least one whole sample.
- A receiver drops and counts a frame that is:
  - not a whole number of samples: `partial_sample` (for mono s16le, an odd byte count);
  - longer than `frame_bytes`: `oversized_frame`;
  - empty, or short without being the final frame: `short_frame`.
- The final `seq` is known from TCP: the mic `stop` reply or `stopped` event carries `last_seq`, and the hub sends the speaker `drain{last_seq}`. A receiver that gets a short frame before it knows the final `seq` holds it until it does.

## Stream ids

The `stream` byte names the stream a frame belongs to.

| Range | Owner | Use |
|---|---|---|
| 0 | — | invalid |
| 1–127 | hub | assigned in `start{stream}` |
| 128–255 | device | streams the Reflex opens |

- Each side assigns its ids round-robin: the next id after the last one it used, wrapping within its range and skipping open ids. A just-closed id isn't reused soon.
- A frame on an unknown **device-range** id opens a Reflex mic stream at once, in the format the Reflex was armed with, if the Reflex is armed. The hub doesn't wait for the `mic.started` event, which only confirms. Otherwise the frame is dropped and counted.
- A frame on an unknown **hub-range** id is dropped.
- Each side remembers the last 16 closed ids of the Connection and silently drops their late frames.

## Timestamps

- **Mic:** `ts` is the device clock at the frame's first sample: the DMA completion time minus the frame duration. The hub maps it to hub time with the clock offset from the pongs.
- **Speaker:** `ts` is 0, meaning "play as soon as possible". Non-zero values are reserved for scheduled playout; MVP devices ignore them.

## Conditioning

Conditioning runs on the device, so the wire carries clean, speech-level PCM and the hub does none.
- **Mic:** a 100 Hz 2nd-order high-pass, then +15 dB of saturating gain. The first 20 ms after the mic is enabled are zeroed.
- **Speaker:** a 300 Hz 2nd-order low-cut.

## Mic stream

- The device never blocks capture on the network. Each stream has a 200 ms queue; on overflow the oldest frames are dropped and counted in `dropped`.
- `stop` replies `{last_seq, dropped}` after the final frame has gone out.
- **Loss, on the hub:**
  - There's no reorder wait. A gap counts as loss as soon as a later frame arrives, and it's zero-filled so consumers see a continuous timeline.
  - Late frames and duplicates are dropped.
  - After the `last_seq` reply, the hub waits at most 30 ms for missing tail frames, then zero-fills and finalizes.

## Speaker stream

**Pacing (hub).** The hub sends the first 60 ms as a burst, then paces in real time to keep a 60 ms lead on its monotonic clock. There's no drift compensation. If the agent's audio runs dry and the stream falls behind its schedule, the audio that follows starts a fresh 60 ms burst, which refills the jitter buffer after its underrun.

**Jitter buffer (device), elastic.**
- Playback starts once `prefill_ms` (default 40) is buffered.
- A missing frame while later frames are buffered is **lost**: it's concealed as silence and playback moves on.
- An empty buffer is an **underrun**: playback pauses and resumes at the oldest frame held once frames arrive. Frames still missing then are skipped, since the pause stood in for them, and they count as lost.
- After `drain`, playback never pauses; missing frames are concealed.
- The buffer spans 200 ms of the stream from the playhead. A frame beyond that drops the frames the buffer no longer spans, and playback jumps to the oldest frame still held. The frames it skips count as lost.
- Every edge gets a 2 ms linear ramp: start, cut, concealment, underrun pause and resume. Concealment is silence, never a repeated frame.

**Stats**, reported by `stop`, `drained` and `stopped`:

| Stat | Meaning |
|---|---|
| `played_ms` | the stream position reached. Lost frames count toward it; underrun pauses don't |
| `lost` | frames never played: concealed as silence, or skipped on overflow or resume |
| `underruns` | how many times playback paused on an empty buffer |
| `underrun_ms` | the total time spent paused |

**Ending**
- `drain{last_seq}` is acknowledged at once. The device plays through `last_seq`, then emits `drained` once the last sample has left the DMA, so a `then_listen` never clips speech.
- `stop` cuts at once. The stream id is dead and its later frames are dropped.
- There is no `flush`. A restart is `stop` plus `start` on a new id, pipelined.

## Exclusive groups

At most one capability of an `exclusive` group is active at a time. This is how the half-duplex Atom Echo declares `[["mic","speaker"]]`.
- A `start` on a capability whose group peer is active gets `busy`.
- The hub normally stops the peer first, pipelined, so `busy` only shows up in races, e.g. when the Reflex holds the mic.

## Stream-end reasons

| Reason | Emitted by | Meaning |
|---|---|---|
| `drained` | device | the speaker played through `last_seq` |
| `reflex` | device | the Reflex cut the speaker, or closed its mic on release |
| `stopped` | hub | the hub or the agent stopped the stream |
| `preempted` | hub | an exclusive peer or a newer stream replaced it |
| `disconnected` | hub | the Connection dropped |
| `overflow` | hub | the hub's source buffer went over its cap |
