// SPDX-License-Identifier: Apache-2.0
#include "diagnostics.h"
#include "cJSON.h"
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

int main(void)
{
    ss_ack_t ack = {.seq = UINT32_MAX, .ok = true, .armed = false, .connected = false};
    const char *uuid = "11111111-1111-4111-8111-111111111111";
    memcpy(ack.id, uuid, SS_ID_SIZE);
    memcpy(ack.session_id, uuid, SS_ID_SIZE);
    memcpy(ack.boot_id, uuid, SS_ID_SIZE);
    ss_ble_diagnostics_t ble = {.nvs = INT_MIN, .init = INT_MAX, .hid_status = -1,
        .adv_rc = INT_MIN, .adv_event = INT_MAX, .fail = INT_MIN, .reason = INT_MAX,
        .bonds = -1, .hid = UINT_MAX, .ready = UINT_MAX, .advertising = UINT_MAX,
        .connections = UINT_MAX, .disconnections = UINT_MAX, .authentications = UINT_MAX};
    char output[SS_MAX_LINE];
    assert(ss_status_json(&ack, &ble, output, sizeof(output)));
    size_t length = strlen(output);
    assert(length < SS_MAX_LINE && output[length-1] == '\n');
    cJSON *root = cJSON_Parse(output);
    assert(root && cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(root, "ok")));
    assert(cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(root, "armed")));
    assert(!strcmp(cJSON_GetObjectItemCaseSensitive(root, "boot_id")->valuestring, uuid));
    cJSON *status = cJSON_GetObjectItemCaseSensitive(root, "ble");
    assert(status && cJSON_GetArraySize(status) == 15);
    assert(cJSON_GetObjectItemCaseSensitive(status, "bonds")->valuedouble == -1);
    assert(cJSON_GetObjectItemCaseSensitive(status, "con")->valuedouble == UINT_MAX);
    cJSON *item;
    cJSON_ArrayForEach(item, status) assert(cJSON_IsNumber(item));
    cJSON_Delete(root);
    assert(ss_status_json(&ack, &ble, output, length+1));
    assert(!ss_status_json(&ack, &ble, output, length));
    assert(output[0] == '\0');
    puts("BLE numeric diagnostics preserve ACK and fit maximum serial frame; truncation fails closed.");
    return 0;
}
