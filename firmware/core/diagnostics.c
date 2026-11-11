// SPDX-License-Identifier: Apache-2.0
#include "diagnostics.h"
#include <stdio.h>
#include <string.h>

bool ss_status_json(const ss_ack_t *ack, const ss_ble_diagnostics_t *ble,
                    char *output, size_t capacity)
{
    if (!ss_ack_json(ack, output, capacity)) return false;
    size_t length = strlen(output);
    if (length < 2 || output[length-2] != '}' || output[length-1] != '\n') return false;
    size_t offset = length - 2;
    int written = snprintf(output + offset, capacity - offset,
        ",\"ble\":{\"v\":1,\"nvs\":%d,\"init\":%d,\"hs\":%d,\"hid\":%u,\"ready\":%u,"
        "\"adv\":%u,\"rc\":%d,\"evt\":%d,\"con\":%u,\"dis\":%u,\"auth\":%u,"
        "\"fail\":%d,\"reason\":%d,\"bonds\":%d}}\n",
        ble->nvs, ble->init, ble->hid_status, ble->hid, ble->ready,
        ble->advertising, ble->adv_rc, ble->adv_event, ble->connections,
        ble->disconnections, ble->authentications, ble->fail, ble->reason, ble->bonds);
    if (written <= 0 || (size_t)written >= capacity-offset) {
        if (capacity) output[0] = '\0';
        return false;
    }
    return true;
}
