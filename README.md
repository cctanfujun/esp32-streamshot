# ESP32 + StreamShot

**Let your AI client see an iPhone screen and operate a real Bluetooth keyboard and mouse.**

StreamShot uploads a picture when the AI requests one. An ESP32-S3 carries keyboard/mouse input from your Mac over USB to iPhone over Bluetooth. This repo provides the reusable agent skill, local MCP bridge and prototype firmware. No additional model API key, cloud backend or desktop connection app.

[中文](README.zh-CN.md) · [Install](#install-the-skill) · [Setup](skills/esp32-streamshot/references/setup.md) · [Firmware](firmware/README.md)

```mermaid
flowchart LR
    AI[Your AI client + skill] <--> Mac[Local MCP bridge]
    Phone[iPhone + StreamShot] -->|Requested picture via local HTTPS| Mac
    Mac -->|USB| ESP[ESP32-S3]
    ESP -->|Bluetooth HID| Phone
```

## Current status

**Developer preview: macOS and a StreamShot build containing AI Link.** The public App Store 1.0 release does not include AI Link. Do not purchase that version for this integration. Compatible app distribution will be tracked here; no public AI Link app download is promised yet.

A real iPhone has verified pointer movement, typing `test` into Notes, system Search, opening WeChat and switching to Chats. A 25-second local-server interruption recovered within the existing broadcast and returned a new picture. Complete messaging, arbitrary app control, long-running capture and all lock-screen scenarios are not fully validated. The skill reduces orchestration round trips; capture latency and smooth control remain active engineering work, with no unsupported speed claim.

## Install the skill

Paste into Codex:

```text
Use $skill-installer to install https://github.com/cctanfujun/esp32-streamshot/tree/main/skills/esp32-streamshot
```

Then ask:

```text
Use $esp32-streamshot to connect my iPhone and verify keyboard/mouse input in an empty note.
```

The skill bundles its local bridge. Installation does not flash a board or start phone sharing. Follow [setup](skills/esp32-streamshot/references/setup.md) for MCP configuration, Bluetooth pairing and phone-side computer trust.

## Fewer round trips

- Reuse the established computer connection and selected USB port.
- Run a small known sequence and request its result in one `streamshot_act` call.
- Keep acknowledged input separate from capture failures to avoid duplicated typing or sends.
- Observe a new UI before deciding its next action; keep system broadcast confirmation with the owner.

Relative HID is not absolute touch. ASCII typing is supported; Chinese entry depends on the observed iPhone IME. Local images stay in memory, while the selected AI client/model provider applies its own conversation policies.

## Develop

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s skills/esp32-streamshot/scripts/streamshot/tests -v
node --test tests/session.test.mjs
```

Python uses the standard library; firmware uses pinned ESP-IDF v5.5.1. No private iOS app source, pairing files, screens, signing identities or model credentials are included. Report versions and error names, not private screens or runtime state.

Apache-2.0. StreamShot is a separate iPhone app; this repository publishes integration components.
