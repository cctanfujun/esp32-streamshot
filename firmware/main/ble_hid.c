// SPDX-License-Identifier: Apache-2.0
// Uses ESP-IDF v5.5.1 esp_hidd / Bluedroid APIs, not an Arduino HID library.
#include "ble_hid.h"
#include <stdatomic.h>
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_gap_ble_api.h"
#include "esp_gatts_api.h"
#include "esp_hidd.h"

static esp_hidd_dev_t *device;
static void (*notify_connection)(bool);
static atomic_bool authenticated, hid_started;
static atomic_uint adv_ready;
static atomic_bool advertising;
static atomic_int adv_result = -1, adv_event = -1, hid_status = -1;
static atomic_int auth_failure, disconnect_reason;
static atomic_uint connections, disconnections, authentications;

// Report 1: relative mouse, three buttons, signed X/Y/wheel. Report 2:
// boot-compatible keyboard packet plus LED output. IDs are not in packet data.
static const uint8_t descriptor[] = {
    0x05,0x01, 0x09,0x02, 0xA1,0x01, 0x85,0x01,
    0x09,0x01, 0xA1,0x00, 0x05,0x09, 0x19,0x01, 0x29,0x03,
    0x15,0x00, 0x25,0x01, 0x95,0x03, 0x75,0x01, 0x81,0x02,
    0x95,0x01, 0x75,0x05, 0x81,0x03, 0x05,0x01,
    0x09,0x30, 0x09,0x31, 0x09,0x38, 0x15,0x81, 0x25,0x7F,
    0x75,0x08, 0x95,0x03, 0x81,0x06, 0xC0,0xC0,
    0x05,0x01, 0x09,0x06, 0xA1,0x01, 0x85,0x02,
    0x05,0x07, 0x19,0xE0, 0x29,0xE7, 0x15,0x00, 0x25,0x01,
    0x75,0x01, 0x95,0x08, 0x81,0x02,
    0x95,0x01, 0x75,0x08, 0x81,0x03,
    0x95,0x05, 0x75,0x01, 0x05,0x08, 0x19,0x01, 0x29,0x05, 0x91,0x02,
    0x95,0x01, 0x75,0x03, 0x91,0x03,
    0x95,0x06, 0x75,0x08, 0x15,0x00, 0x26,0xE7,0x00,
    0x05,0x07, 0x19,0x00, 0x2A,0xE7,0x00, 0x81,0x00, 0xC0,
};
static esp_hid_raw_report_map_t maps[] = {{.data = descriptor, .len = sizeof(descriptor)}};
static const esp_hid_device_config_t hid_config = {
    // Prototype PnP values are not a claimed registered product identity.
    .vendor_id = 0, .product_id = 0, .version = 0x0100,
    .device_name = "StreamShot HID", .manufacturer_name = "StreamShot prototype",
    .serial_number = "prototype-v1", .report_maps = maps, .report_maps_len = 1,
};

static void advertise(void)
{
    if (!hid_started || adv_ready != 3) return;
    esp_ble_adv_params_t parameters = {
        .adv_int_min = 0x30, .adv_int_max = 0x50, .adv_type = ADV_TYPE_IND,
        .own_addr_type = BLE_ADDR_TYPE_PUBLIC, .channel_map = ADV_CHNL_ALL,
        .adv_filter_policy = ADV_FILTER_ALLOW_SCAN_ANY_CON_ANY,
    };
    adv_result = esp_ble_gap_start_advertising(&parameters);
}

static void gap_callback(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param)
{
    if (event == ESP_GAP_BLE_ADV_DATA_SET_COMPLETE_EVT) {
        if (param->adv_data_cmpl.status == ESP_BT_STATUS_SUCCESS) adv_ready |= 1;
        advertise();
    } else if (event == ESP_GAP_BLE_SCAN_RSP_DATA_SET_COMPLETE_EVT) {
        if (param->scan_rsp_data_cmpl.status == ESP_BT_STATUS_SUCCESS) adv_ready |= 2;
        advertise();
    } else if (event == ESP_GAP_BLE_ADV_START_COMPLETE_EVT) {
        adv_event = param->adv_start_cmpl.status;
        advertising = adv_event == ESP_BT_STATUS_SUCCESS;
    } else if (event == ESP_GAP_BLE_ADV_STOP_COMPLETE_EVT) {
        advertising = false;
    } else if (event == ESP_GAP_BLE_SEC_REQ_EVT) {
        (void)esp_ble_gap_security_rsp(param->ble_security.ble_req.bd_addr, true);
    } else if (event == ESP_GAP_BLE_AUTH_CMPL_EVT) {
        authentications++;
        auth_failure = param->ble_security.auth_cmpl.success ? 0 : param->ble_security.auth_cmpl.fail_reason;
        authenticated = param->ble_security.auth_cmpl.success;
        notify_connection(authenticated);
        if (!authenticated) (void)esp_ble_gap_disconnect(param->ble_security.auth_cmpl.bd_addr);
    } else if (event == ESP_GAP_BLE_NC_REQ_EVT) {
        // No display or serial passkey endpoint: never auto-confirm a different
        // pairing method or log passkeys, addresses or input reports.
        (void)esp_ble_confirm_reply(param->ble_security.key_notif.bd_addr, false);
    }
}

static void hid_callback(void *context, esp_event_base_t base, int32_t event, void *data)
{
    (void)context; (void)base;
    if (event == ESP_HIDD_START_EVENT) {
        // ESP-IDF BLE posts START with a NULL payload (unlike Classic HID).
        hid_status = ESP_OK;
        hid_started = true;
        advertise();
    } else if (event == ESP_HIDD_CONNECT_EVENT) {
        connections++;
        advertising = false;
    } else if (event == ESP_HIDD_DISCONNECT_EVENT || event == ESP_HIDD_STOP_EVENT) {
        if (event == ESP_HIDD_DISCONNECT_EVENT) {
            const esp_hidd_event_data_t *value = data;
            disconnections++;
            disconnect_reason = value->disconnect.reason;
        }
        authenticated = false;
        notify_connection(false);
        if (event == ESP_HIDD_DISCONNECT_EVENT) advertise();
    } else if (event == ESP_HIDD_CONTROL_EVENT) {
        const esp_hidd_event_data_t *value = data;
        notify_connection(value->control.control ? authenticated : false);
    }
}

ss_ble_diagnostics_t ss_ble_diagnostics(void)
{
    return (ss_ble_diagnostics_t){.hid_status = hid_status, .hid = hid_started,
        .ready = adv_ready, .advertising = advertising, .adv_rc = adv_result,
        .adv_event = adv_event, .connections = connections, .disconnections = disconnections,
        .authentications = authentications, .fail = auth_failure, .reason = disconnect_reason,
        .bonds = device ? esp_ble_get_bond_device_num() : -1};
}

esp_err_t ss_ble_init(void (*connection_changed)(bool))
{
    notify_connection = connection_changed;
    esp_bt_controller_config_t config = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    esp_err_t error = esp_bt_controller_init(&config);
    if (error != ESP_OK) return error;
    if ((error = esp_bt_controller_enable(ESP_BT_MODE_BLE)) != ESP_OK) return error;
    if ((error = esp_bluedroid_init()) != ESP_OK) return error;
    if ((error = esp_bluedroid_enable()) != ESP_OK) return error;
    if ((error = esp_ble_gap_register_callback(gap_callback)) != ESP_OK) return error;
    if ((error = esp_ble_gatts_register_callback(esp_hidd_gatts_event_handler)) != ESP_OK) return error;
    // LE Secure Connections + bonding, no fixed PIN or fake display. First
    // pairing remains an explicit physical iPhone Settings/AssistiveTouch step.
    esp_ble_auth_req_t authentication = ESP_LE_AUTH_REQ_SC_BOND;
    esp_ble_io_cap_t capability = ESP_IO_CAP_NONE;
    uint8_t key_size = 16, keys = ESP_BLE_ENC_KEY_MASK | ESP_BLE_ID_KEY_MASK;
#define SECURITY(parameter, value) do { error = esp_ble_gap_set_security_param(parameter, &(value), sizeof(value)); if (error != ESP_OK) return error; } while (0)
    SECURITY(ESP_BLE_SM_AUTHEN_REQ_MODE, authentication);
    SECURITY(ESP_BLE_SM_IOCAP_MODE, capability);
    SECURITY(ESP_BLE_SM_MAX_KEY_SIZE, key_size);
    SECURITY(ESP_BLE_SM_SET_INIT_KEY, keys);
    SECURITY(ESP_BLE_SM_SET_RSP_KEY, keys);
#undef SECURITY
    if ((error = esp_ble_gap_set_device_name(hid_config.device_name)) != ESP_OK) return error;
    uint8_t service_uuid[] = {0xFB,0x34,0x9B,0x5F,0x80,0x00,0x00,0x80,0x00,0x10,0x00,0x00,0x12,0x18,0x00,0x00};
    esp_ble_adv_data_t advertising = {
        .appearance = ESP_HID_APPEARANCE_GENERIC, .service_uuid_len = sizeof(service_uuid),
        .p_service_uuid = service_uuid, .flag = ESP_BLE_ADV_FLAG_GEN_DISC | ESP_BLE_ADV_FLAG_BREDR_NOT_SPT,
    };
    esp_ble_adv_data_t scan_response = {.set_scan_rsp = true, .include_name = true};
    if ((error = esp_ble_gap_config_adv_data(&advertising)) != ESP_OK) return error;
    if ((error = esp_ble_gap_config_adv_data(&scan_response)) != ESP_OK) return error;
    return esp_hidd_dev_init(&hid_config, ESP_HID_TRANSPORT_BLE, hid_callback, &device);
}

bool ss_ble_mouse(void *context, uint8_t buttons, int8_t dx, int8_t dy, int8_t wheel)
{
    (void)context;
    if (!authenticated || !device || !esp_hidd_dev_connected(device)) return false;
    uint8_t packet[] = {buttons, (uint8_t)dx, (uint8_t)dy, (uint8_t)wheel};
    return esp_hidd_dev_input_set(device, 0, 1, packet, sizeof(packet)) == ESP_OK;
}

bool ss_ble_keyboard(void *context, uint8_t modifiers, uint8_t usage)
{
    (void)context;
    if (!authenticated || !device || !esp_hidd_dev_connected(device)) return false;
    if (usage >= 224 && usage <= 231) { modifiers |= (uint8_t)(1u << (usage - 224)); usage = 0; }
    uint8_t packet[] = {modifiers, 0, usage, 0, 0, 0, 0, 0};
    return esp_hidd_dev_input_set(device, 0, 2, packet, sizeof(packet)) == ESP_OK;
}
