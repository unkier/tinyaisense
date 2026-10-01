idf_component_register(SRCS "mic.c" "speaker.c" "button.c" "led.c" "kinds.c"
                       INCLUDE_DIRS "include"
                       REQUIRES tas_board tas_wire tas_led
                       PRIV_REQUIRES log esp_timer freertos)
target_compile_options(${COMPONENT_LIB} PRIVATE -std=gnu11 -Wall -Wextra -Werror)
