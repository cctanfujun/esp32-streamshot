// SPDX-License-Identifier: Apache-2.0
#include <stdio.h>
#include <string.h>
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/usb_serial_jtag.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_cpu.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "ble_hid.h"
#include "control.h"
#include "runtime_gate.h"

static ss_control_t control;
static SemaphoreHandle_t mutex;
static esp_err_t nvs_error, ble_error = ESP_ERR_INVALID_STATE;
static ss_link_mailbox_t link_mailbox;
static unsigned applied_link;
static ss_progress_guard_t progress;

static uint64_t now_ms(void *context) { (void)context; return (uint64_t)esp_timer_get_time() / 1000; }
static void sleep_ms(void *context, uint32_t ms) { (void)context; vTaskDelay(pdMS_TO_TICKS(ms)); }

static void connection(bool connected)
{
    ss_link_publish(&link_mailbox, connected);
}

// Called only by non-Bluetooth tasks while holding the control mutex. A new
// epoch always latches the prior lease, including a coalesced false -> true.
static void consume_connection(void)
{
    bool connected;
    if (ss_link_take(&link_mailbox, &applied_link, &connected)) ss_connection(&control, connected);
}

static bool mouse(void *context, uint8_t buttons, int8_t dx, int8_t dy, int8_t wheel)
{
    if (!ss_link_current(&link_mailbox, applied_link)) return false;
    bool sent = ss_ble_mouse(context, buttons, dx, dy, wheel);
    return sent && ss_link_current(&link_mailbox, applied_link);
}

static bool keyboard(void *context, uint8_t modifiers, uint8_t usage)
{
    if (!ss_link_current(&link_mailbox, applied_link)) return false;
    bool sent = ss_ble_keyboard(context, modifiers, usage);
    return sent && ss_link_current(&link_mailbox, applied_link);
}

static void recovery_watchdog(void *context)
{
    (void)context;
    for (;;) {
        if (ss_progress_stalled(&progress, (unsigned)now_ms(NULL), esp_cpu_dbgr_is_attached())) {
            // A blocked SDK callback must not leave USB and advertising hung
            // forever. Restart preserves NVS bonds and boots unarmed; no input
            // or prior lease is replayed. The host detects the new boot ID.
            esp_restart();
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

static void watchdog(void *context)
{
    (void)context;
    // Initialization/BT startup is outside the monitored control loop.
    ss_progress_complete(&progress, (unsigned)now_ms(NULL));
    for (;;) {
        xSemaphoreTake(mutex, portMAX_DELAY);
        consume_connection();
        ss_tick(&control);
        xSemaphoreGive(mutex);
        // Only successful completion of this control loop feeds recovery.
        // Serial polling cannot mask a blocked tick/neutral/connection path.
        ss_progress_complete(&progress, (unsigned)now_ms(NULL));
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

static bool serial_init(void)
{
#if CONFIG_STREAMSHOT_USB_SERIAL
    usb_serial_jtag_driver_config_t config = {.rx_buffer_size = 2048, .tx_buffer_size = 2048};
    return usb_serial_jtag_driver_install(&config) == ESP_OK;
#else
    uart_config_t config = {.baud_rate = 115200, .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE, .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE, .source_clk = UART_SCLK_DEFAULT};
    return uart_param_config(UART_NUM_0, &config) == ESP_OK
        && uart_driver_install(UART_NUM_0, 2048, 2048, 0, NULL, 0) == ESP_OK;
#endif
}

static int serial_read(uint8_t *data, size_t capacity)
{
#if CONFIG_STREAMSHOT_USB_SERIAL
    return usb_serial_jtag_read_bytes(data, capacity, pdMS_TO_TICKS(20));
#else
    return uart_read_bytes(UART_NUM_0, data, capacity, pdMS_TO_TICKS(20));
#endif
}

static void serial_write(const char *data)
{
    size_t length = strlen(data);
#if CONFIG_STREAMSHOT_USB_SERIAL
    (void)usb_serial_jtag_write_bytes(data, length, pdMS_TO_TICKS(100));
#else
    (void)uart_write_bytes(UART_NUM_0, data, length);
#endif
}

void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_NONE);
    if (!serial_init()) return;
    uint8_t random[16];
    esp_fill_random(random, sizeof(random));
    random[6] = (random[6] & 0x0F) | 0x40;
    random[8] = (random[8] & 0x3F) | 0x80;
    char boot_id[SS_ID_SIZE];
    snprintf(boot_id, sizeof(boot_id),
        "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
        random[0],random[1],random[2],random[3],random[4],random[5],random[6],random[7],
        random[8],random[9],random[10],random[11],random[12],random[13],random[14],random[15]);
    ss_link_init(&link_mailbox);
    ss_progress_init(&progress);
    ss_io_t io = {.mouse = mouse, .keyboard = keyboard, .sleep_ms = sleep_ms, .now_ms = now_ms};
    ss_init(&control, io, boot_id);
    mutex = xSemaphoreCreateMutex();
    if (!mutex) return;
    // Never erase existing NVS or pairing data automatically on an init error.
    nvs_error = nvs_flash_init();
    if (nvs_error == ESP_OK) ble_error = ss_ble_init(connection);
    if (xTaskCreate(recovery_watchdog, "hid_recovery", 2048, NULL, 6, NULL) != pdPASS) return;
    if (xTaskCreate(watchdog, "hid_watchdog", 3072, NULL, 5, NULL) != pdPASS) return;
    uint8_t input[128];
    ss_framer_t framer = {0};
    for (;;) {
        int count = serial_read(input, sizeof(input));
        for (int i = 0; i < count; i++) {
            ss_request_t request;
            if (ss_feed(&framer, input[i], &request)) {
                xSemaphoreTake(mutex, portMAX_DELAY);
                consume_connection();
                ss_ack_t ack = ss_execute(&control, &request);
                xSemaphoreGive(mutex);
                char output[SS_MAX_LINE];
                bool encoded;
                if (request.op == SS_STATUS) {
                    ss_ble_diagnostics_t diagnostic = ss_ble_diagnostics();
                    diagnostic.nvs = nvs_error;
                    diagnostic.init = ble_error;
                    encoded = ss_status_json(&ack, &diagnostic, output, sizeof(output));
                } else encoded = ss_ack_json(&ack, output, sizeof(output));
                if (encoded) serial_write(output);
            }
        }
    }
}
