# One board file per Board; the Kconfig choice picks which one is built.
if(CONFIG_TAS_BOARD_M5STACK_ATOM_ECHO)
    set(board_src "boards/m5stack_atom_echo.c")
endif()

idf_component_register(SRCS ${board_src}
                       INCLUDE_DIRS "include"
                       REQUIRES tas_audio
                       PRIV_REQUIRES esp_driver_gpio)
target_compile_options(${COMPONENT_LIB} PRIVATE -std=c11 -Wall -Wextra -Werror)
