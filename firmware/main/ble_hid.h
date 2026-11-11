// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "diagnostics.h"

esp_err_t ss_ble_init(void (*connection_changed)(bool));
ss_ble_diagnostics_t ss_ble_diagnostics(void);
bool ss_ble_mouse(void *context, uint8_t buttons, int8_t dx, int8_t dy, int8_t wheel);
bool ss_ble_keyboard(void *context, uint8_t modifiers, uint8_t usage);
