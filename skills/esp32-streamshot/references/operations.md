# Operations

Use native MCP tools when available. A task normally needs one status check, one observed image, then short `streamshot_act` calls separated by real UI decisions. Do not rediscover the environment for each key.

`streamshot_act` takes `session_id`, `frame_id`, `actions`, optional `capture` (default true), and `settle_ms` (default 250, max 1000). Expanded actions are limited to 16 primitives:

```json
{"op":"move","dx":16,"dy":-32}
{"op":"click","button":"left"}
{"op":"scroll","dy":1}
{"op":"key","usage":44,"modifiers":8}
{"op":"type","text":"WeChat"}
```

Key usages follow USB HID. Left Ctrl=1, Shift=2, Alt=4, GUI/Command=8. [Apple documents Command-Space for Search on Magic Keyboard](https://support.apple.com/guide/iphone/iph3da414515/ios); this does not establish its behavior on an arbitrary BLE keyboard. Verify Home/Search once during setup, using the visible AssistiveTouch menu or a configured device button when appropriate. Do not repeatedly guess shortcuts during a timed task.

Each primitive checks the same session, geometry and fresh observation. Unknown input stops the remainder; never automatically obtain a new frame and continue a partially executed macro. Capture failure after ACK is distinct from input failure. Replaying an RPC returns only its small input receipt, without image caching or further I/O.

With a compatible `settle-ticket-v1` app, capture starts immediately after input ACK. The phone retains only the latest real callback within this request's short settle window, then encodes it, avoiding a desktop sleep that misses the animation's final frame. This is not idle caching. Source callback age is deducted from observation lifetime; a missing post-request sample is explicit. Old phones do not silently claim support for nonzero settle. The result's `input_ms`, `capture_ms`, and `total_ms` are measured host timings, not model reasoning time or proof of visual completion.

## Persistent-session fallback

If native MCP tools are unavailable and a persistent Node REPL is available, use `scripts/session.mjs`. It starts one MCP child, frames JSON lines and preserves observation state without PTY echoes or polling sleeps. Do not start a one-shot MCP process per operation.

```javascript
var { StreamShotSession } = await import('/absolute/skill/path/scripts/session.mjs');
var phone = await StreamShotSession.start({serialPort:'/dev/cu.usbmodemYOUR_DEVICE'});
var result = await phone.call('streamshot_status', {});
```

Create the session only once; reuse an existing `phone` object. Keep frame state inside the session instead of closing over a repeatedly reassigned REPL variable. Receiving an image does not make it an inspected observation. `inspect(result, display)` awaits an explicit image-display callback, then records only that frame's metadata. The next `act(actions, options)` consumes that observation and never fetches or retries by itself:

```javascript
var displayPhone = async ({ summary, images }) => {
  nodeRepl.write(summary);
  for (var block of images) {
    await nodeRepl.emitImage({bytes:Buffer.from(block.data,'base64'),mimeType:block.mimeType});
  }
};
await phone.inspect(await phone.screen({settle_ms:250}), displayPhone);
```

Inspect that image before the next call. For an already identified, focused field:

```javascript
await phone.inspect(await phone.act([{op:'type',text:'WeChat'}]), displayPhone);
```

`screen()` forwards optional `settle_ms` (0–1000, default 0); `act()` defaults to capture=true and settle_ms=250. A failed capture, reset, stop, changed session or superseding request clears the usable observation. Queued acts cannot reuse a frame already consumed by another act. The existing server still enforces source age, session and geometry; the helper never extends freshness. Low-level `call()` remains available for status/reset/stop and explicit tool schemas.

The display callback emits decoded image bytes and a compact receipt. Successful per-key UUID lists stay in the returned result; partial failures remain in the displayed summary. Never print the raw response/base64. Keep the session alive between calls; `await phone.close()` closes only its own child. If another runtime owns the state, use the existing tools rather than killing it or rotating trust.

For a complete task benchmark, call `phone.beginBenchmark()` immediately before its initial observation/navigation, then `phone.endBenchmark({completed:true})` only after visually confirming the requested final result. End an incomplete attempt with the default completed=false. The result includes wall_clock_ms and per-call roundtrip_ms, queue_ms, rpc_ms, display_ms, plus the gap from the preceding display to the next act invocation as decision_ms. Host input/capture timings remain separate. Metrics contain no images, typed text or screen contents. Finish one-time system setup outside this measurement; a failed or interrupted task is not a speed success.

If a mutating fallback call times out or loses transport, the helper immediately interrupts its own MCP child so the remaining batch cannot continue unseen. The last in-flight action remains unknown. Do not retry it or silently restart; reconnect, explicitly reset control, and inspect a new image before deciding what remains. This does not kill another client's bridge.

After a capture timeout, check status once and distinguish sharing, reconnection and input state. Repeated broadcast restarts do not fix a missing frame. Record bounded diagnostics rather than looping on prompts or replaying input.

An explicit desktop reset checks the selected USB device identity. If USB
re-enumeration replaced it behind the same path, reset may reopen only that
selected port, confirm the current boot and require an acknowledged neutral
stop. A lost ACK, still-armed reply or reopen failure keeps input blocked. It
preserves retired sessions, sequence numbers and replay protection; it does not
re-pair Bluetooth or resume input without a fresh image and new action.
