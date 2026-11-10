# Setup

**macOS developer preview.** A StreamShot build containing AI Link is required. The existing public App Store 1.0 release does not include this integration; buying it does not enable AI phone control. Check the repository's compatibility/release status before recommending an app download.

## Install the skill

In Codex, ask:

> Use $skill-installer to install https://github.com/cctanfujun/esp32-streamshot/tree/main/skills/esp32-streamshot

Or clone and copy `skills/esp32-streamshot` into the current Codex personal skills folder `~/.agents/skills/esp32-streamshot`. Existing older `~/.codex/skills` installations also remain in use. Compare before overwriting. Start a new Codex session if the installed skill is not discovered automatically.

## Computer runtime

Python 3.10+, macOS `openssl`, `dns-sd` and `scutil` are used; no model SDK or Python package dependency.

From the installed skill folder:

```sh
python3 scripts/setup.py doctor
python3 scripts/setup.py mcp-config --port /dev/cu.usbmodemYOUR_DEVICE
```

Doctor lists serial candidates without opening them. Select the actual ESP32-S3 USB port; never guess or automatically flash. Omit `--port` for screen-only use. `mcp-config` prints a configuration without changing client settings, starting a server or revealing credentials. Add it in the AI client's MCP settings. For Codex CLI, an equivalent command is:

```sh
codex mcp add streamshot --env STREAMSHOT_SERIAL_PORT=/dev/cu.usbmodemYOUR_DEVICE -- python3 /absolute/skill/path/scripts/streamshot/streamshot_bridge.py mcp
```

Inspect and reuse an existing `streamshot` entry. The MCP-managed runtime advertises the computer with Bonjour and keeps private TLS/token state outside the skill/repo. Do not post that directory to an issue.

## iPhone and board

1. Prepare the compatible firmware using the [firmware source and instructions](https://github.com/cctanfujun/esp32-streamshot/tree/main/firmware). A skill-only installation does not include firmware; clone the full repository if a build is needed. Flashing is separate and explicitly port-selected; preserve existing firmware/data before replacement.
2. Pair **StreamShot HID** in iPhone Bluetooth settings. Enable AssistiveTouch for the pointer. Verify keyboard input in an empty note before a messaging task.
3. Use the same LAN. In StreamShot → AI Link, choose the computer and confirm trust on the phone. There is no computer pairing code, QR scan or helper app.
4. Tap the broadcast icon and personally confirm **Start Broadcast**. Pairing or installing the skill never starts sharing or input.

Initial trust assumes a trusted local network. The phone pins the actual computer certificate; never silently accept a changed pin. Do not bypass occupied runtimes or unknown hardware outcomes by generating new state.

Input is relative movement and key taps, not absolute touch, dragging or arbitrary Unicode injection. iOS controls protected content and system broadcast lifetime. Images are only uploaded on request; the AI client/model provider handles its conversation under its own policies.

Official background: [OpenAI skill installation](https://learn.chatgpt.com/docs/build-skills), [Apple pointer setup](https://support.apple.com/111775).
