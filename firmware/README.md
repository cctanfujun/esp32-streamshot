# ESP32-S3 Bluetooth HID prototype

USB Serial/JTAG → ESP32-S3 → Bluetooth keyboard/mouse. Uses official ESP-IDF **v5.5.1**, commit `fcae32885b0296b32044cb99ecbdc50d98dddb83`. Original sources are Apache-2.0; ESP-IDF and its bundled cJSON retain their upstream licenses. SDK dependencies are fetched separately, not included here.

This prototype targets **ESP32-S3**, with a 2 MB application layout and native USB Serial/JTAG by default. It has been tested on one owner's S3 board; the PCB model remains unconfirmed. Other ESP32 variants, USB-UART wiring and board-specific pins are not interchangeable assumptions.

## Build

Install the pinned official ESP-IDF and its ESP32-S3 toolchain according to [Espressif's guide](https://docs.espressif.com/projects/esp-idf/en/v5.5.1/esp32s3/get-started/index.html). Activate that SDK in your shell, then:

```sh
idf.py --version
idf.py -C firmware -B .local/firmware-build build
```

Verify that the active version is v5.5.1 before building. Generated `sdkconfig` and `.local` outputs are ignored. Build and inspect first; a build does not pair a phone or send input.

## Flash and pair

Flashing replaces firmware on the selected board. Preserve a full flash backup and confirm the exact S3/port before an explicitly authorized write. This skill never auto-flashes. After preparing that backup and selecting your board's **native USB** port:

```sh
idf.py -C firmware -B .local/firmware-build -p /dev/cu.usbmodemYOUR_DEVICE flash
```

If native USB remains in download mode, use the official tool's full watchdog reset. A USB core reset may not start the app; do not erase NVS or assume BOOT was held. Pair **StreamShot HID** on the intended iPhone in a trusted physical setting, enable AssistiveTouch and verify in an empty note. Pairing uses LE Secure Connections Just Works, without MITM authentication.

## Runtime limits

- Boot starts unarmed. Only an authenticated connected BLE host can accept a new 1–30 second lease.
- Inputs are relative mouse movement, scroll, click and key taps. Press and release occur within each command; no held drag, absolute touch or arbitrary text endpoint.
- Increasing sequence numbers, a bounded command-ID cache, stop, lease expiry and BLE loss prevent stale replay. Unknown input is not automatically retried.
- At most 64 distinct armed sessions per boot; exhaustion requires an explicit physical reboot and new session.
- Stop and reconnect neutralize when transport permits. A disconnected phone cannot receive a release; a board ACK is not visual confirmation.

The integration requires its screen observation guard for AI input. Owner-observed hardware diagnosis is separate from automatic phone operation. No Wi-Fi credentials, model service, account access or screen capture exists in the firmware.
