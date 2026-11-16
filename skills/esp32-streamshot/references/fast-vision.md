# StreamShot fast vision

This optional desktop helper uses an existing ChatGPT login in Codex CLI for one
specific decision: whether the current iPhone frame actually shows the desired
Full Keyboard Access focus. Tested with Codex CLI 0.169.0 and Node 24.22.1.
The main agent can continue to inspect images normally.

Set the named focus support explicitly on the desktop client only after the
owner has enabled iPhone Full Keyboard Access with default bindings. Open the
optional advisor before asking for a screen. `reviewFocus` takes the result you
already requested; this can also be the image returned from a previous action.
Use an explicit display callback so the main agent can review that exact image:

```js
import { FastVisionAdvisor, reviewFocus } from './fast-vision.mjs';

const advisor = await FastVisionAdvisor.start().catch(() => undefined);
try {
  const review = await reviewFocus(session, await session.screen(), {
    target: 'Touch', display, advisor,
  });
  // advisor: a label tied to this image; confirm the actual highlight yourself.
  // main: inspect the same displayed image directly; never guess a focus.
  // blocked: there is no observation eligible for input.
} finally {
  await advisor?.close();
}
```

For a longer task, reuse the warm advisor and StreamShot session across calls.
The review emits one image and waits up to six seconds for the optional model.
On timeout, no visible focus, uncertain output or mismatched evidence, the
main agent has that same image and can make its own judgment without a second
capture. A cached or diagnostic image only provides context and is not sent
to the focus advisor. Never send `focus_activate` unless a new image from the
current session visibly confirms the highlighted target and the requested
action is authorized.

The advisor uses a warm local process, one ephemeral thread per image,
`gpt-6-luna` at low effort and standard service, no model API key, and a
12-second default direct-call ceiling, including thread startup and inference.
The shorter review wait and the frame's remaining lifetime can limit it further.
It asks for no tools or execution
environment, reports only focus evidence tied to the input frame, and has
no connection to StreamShot HID. The bridge still enforces the original
30-second observation limit. No button is pressed by model output alone.

The selected image is sent in memory to the signed-in Codex service; it
can count toward regular ChatGPT usage. No local screenshot is written.
Only use this option if the current screenshot is appropriate for that client.
No continuous capture, extra subscription, or priority-tier request is required.
