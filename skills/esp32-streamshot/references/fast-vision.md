# StreamShot fast vision

This optional desktop helper uses an existing ChatGPT login in Codex CLI for one
specific decision: whether the current iPhone frame actually shows the desired
Full Keyboard Access focus. Tested with Codex CLI 0.169.0 and Node 24.22.1.
The main agent can continue to inspect images normally.

Set the named focus support explicitly on the desktop client only after the
owner has enabled iPhone Full Keyboard Access with default bindings. Open the
advisor before asking for a screen. Pass it no more than the single frame you
intend to inspect. For example, in the persistent StreamShot session, with `FastVisionAdvisor` imported
from `scripts/fast-vision.mjs`:

```js
const advisor = await FastVisionAdvisor.start();
try {
  let evidence;
  const seen = await session.inspect(await session.screen(), async view => {
    await display(view); // Emit the current image for the operator.
    try { evidence = await advisor.inspect(view, 'Touch'); }
    catch { evidence = undefined; } // Continue with the open main agent.
  });
  // Require seen.observed, matching session/frame, and a visibly focused
  // target before a separately requested and permitted focus_activate.
  // If unavailable, expired, uncertain or nowhere in the current image, stop.
} finally {
  await advisor.close();
}
```

The advisor uses a warm local process, one ephemeral thread per image,
`gpt-6-luna` at low effort and standard service, no model API key, and a
12-second default inference deadline. It asks for no tools or execution
environment, reports only focus evidence tied to the input frame, and has
no connection to StreamShot HID. The bridge still enforces the original
30-second observation limit. No button is pressed by model output alone.

The selected image is sent in memory to the signed-in Codex service; it
can count toward regular ChatGPT usage. No local screenshot is written.
Only use this option if the current screenshot is appropriate for that client.
No continuous capture, extra subscription, or priority-tier request is required.
