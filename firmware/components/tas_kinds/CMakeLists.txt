idf_component_register(SRCS "mic.c" "speaker.c" "button.c" "led.c" "kinds.c"
                       INCLUDE_DIRS "include"
                       REQUIRES tas_board
                       PRIV_REQUIRES log)
target_compile_options(${COMPONENT_LIB} PRIVATE -std=c11 -Wall -Wextra -Werror)
