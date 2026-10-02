set(srcs "mic.c" "speaker.c" "button.c" "led.c" "kinds.c" "exclusive.c")
if(CONFIG_TAS_TEST_HOOKS)
    list(APPEND srcs "test_hooks.c")
endif()

idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "include"
                       REQUIRES tas_board tas_wire tas_led
                       PRIV_REQUIRES tas_reflex log esp_timer freertos console)
target_compile_options(${COMPONENT_LIB} PRIVATE -std=gnu11 -Wall -Wextra -Werror)
