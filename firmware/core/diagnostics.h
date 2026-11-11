// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "control.h"

// Numeric status only: never include peer addresses, keys, passkeys or input.
typedef struct {
    int nvs, init, hid_status, adv_rc, adv_event, fail, reason, bonds;
    unsigned hid, ready, advertising, connections, disconnections, authentications;
} ss_ble_diagnostics_t;

bool ss_status_json(const ss_ack_t *ack, const ss_ble_diagnostics_t *ble,
                    char *output, size_t capacity);
