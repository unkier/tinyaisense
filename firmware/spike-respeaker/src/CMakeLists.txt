idf_component_register(SRCS "main.c" INCLUDE_DIRS "." REQUIRES tas_audio esp_driver_i2s esp_driver_i2c
                       esp_driver_pcnt esp_driver_gpio esp_driver_usb_serial_jtag esp_wifi esp_netif
                       esp_event nvs_flash lwip esp_timer)
