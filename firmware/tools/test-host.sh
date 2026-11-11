#!/usr/bin/env bash
set -euo pipefail
umask 077

# Host-only checks: no SDK activation, serial access, flashing or HID input.
# Point IDF_PATH at the separately installed, pinned official SDK checkout.
sdk_dir="${IDF_PATH:?Set IDF_PATH to the official ESP-IDF v5.5.1 checkout}"
expected_sha=fcae32885b0296b32044cb99ecbdc50d98dddb83
if [[ "$(git -C "$sdk_dir" rev-parse HEAD)" != "$expected_sha" ]]; then
  echo 'ESP-IDF v5.5.1 pinned source is required.' >&2
  exit 1
fi

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/esp32-streamshot-host.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT
flags=(-std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer)

# Suppress Apple's sprintf deprecation only in unchanged upstream cJSON.
"${CC:-cc}" "${flags[@]}" -Wno-deprecated-declarations \
  -I "$sdk_dir/components/json/cJSON" \
  -c "$sdk_dir/components/json/cJSON/cJSON.c" -o "$work_dir/cJSON.o"
"${CC:-cc}" "${flags[@]}" \
  -I "$project_dir/core" -I "$sdk_dir/components/json/cJSON" \
  "$project_dir/core/control.c" "$project_dir/tests/test_control.c" \
  "$work_dir/cJSON.o" -lm -o "$work_dir/test_control"
"$work_dir/test_control"
"${CC:-cc}" "${flags[@]}" \
  -I "$project_dir/core" -I "$sdk_dir/components/json/cJSON" \
  "$project_dir/core/control.c" "$project_dir/core/diagnostics.c" \
  "$project_dir/tests/test_diagnostics.c" "$work_dir/cJSON.o" \
  -lm -o "$work_dir/test_diagnostics"
"$work_dir/test_diagnostics"
