idf_component_register(SRCS "main.c" "ble_hid.c" "../core/control.c"
                       INCLUDE_DIRS "." "../core"
                       REQUIRES esp_hid bt nvs_flash esp_timer esp_driver_usb_serial_jtag esp_driver_uart json)
