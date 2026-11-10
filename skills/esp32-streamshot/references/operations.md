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

Key usages follow USB HID. Left Ctrl=1, Shift=2, Alt=4, GUI/Command=8. [Apple documents Command-Space for Search](https://support.apple.com/guide/iphone/iph3da414515/ios). Selecting a result may require Down Arrow then Return; observe the result. This is not a universal Home shortcut; modifier remapping and IME settings can change behavior.

Each primitive checks the same session, geometry and fresh observation. Unknown input stops the remainder; never automatically obtain a new frame and continue a partially executed macro. Capture failure after ACK is distinct from input failure. Replaying an RPC returns only its small input receipt, without image caching or further I/O.

With a compatible `settle-ticket-v1` app, capture starts immediately after input ACK. The phone retains only the latest real callback within this request's short settle window, then encodes it, avoiding a desktop sleep that misses the animation's final frame. This is not idle caching. Source callback age is deducted from observation lifetime; a missing post-request sample is explicit. Old phones do not silently claim support for nonzero settle. The result's `input_ms`, `capture_ms`, and `total_ms` are measured host timings, not model reasoning time or proof of visual completion.

## Persistent-session fallback

If native MCP tools are unavailable and a persistent Node REPL is available, use `scripts/session.mjs`. It starts one MCP child, frames JSON lines and preserves observation state without PTY echoes or polling sleeps. Do not start a one-shot MCP process per operation.

```javascript
var { StreamShotSession } = await import('/absolute/skill/path/scripts/session.mjs');
var phone = await StreamShotSession.start({serialPort:'/dev/cu.usbmodemYOUR_DEVICE'});
var result = await phone.call('streamshot_status', {});
```

Display returned image blocks directly in the client UI by emitting decoded bytes; print only text metadata, never the raw response/base64. Keep the session alive between calls; `await phone.close()` closes only its own child. If another runtime owns the state, use the existing tools rather than killing it or rotating trust.

If a mutating fallback call times out or loses transport, the helper immediately interrupts its own MCP child so the remaining batch cannot continue unseen. The last in-flight action remains unknown. Do not retry it or silently restart; reconnect, explicitly reset control, and inspect a new image before deciding what remains. This does not kill another client's bridge.

After a capture timeout, check status once and distinguish sharing, reconnection and input state. Repeated broadcast restarts do not fix a missing frame. Record bounded diagnostics rather than looping on prompts or replaying input.
