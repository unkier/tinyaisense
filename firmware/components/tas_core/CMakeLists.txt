idf_component_register(SRCS "core.c" "config.c" "console.c" "net.c" "ctrl.c" "commands.c"
                            "status.c" "manifest.c" "udp.c"
                       INCLUDE_DIRS "include"
                       REQUIRES tas_board
                       PRIV_REQUIRES tas_kinds tas_led tas_wire tas_conn tas_status log heap
                                     nvs_flash esp_wifi esp_netif esp_event vfs console
                                     esp_driver_uart esp_app_format esp_system esp_timer efuse
                                     lwip)
target_compile_options(${COMPONENT_LIB} PRIVATE -std=gnu11 -Wall -Wextra -Werror)
