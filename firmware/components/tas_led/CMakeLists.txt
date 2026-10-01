# Pure C with no IDF calls: the native test env builds the same sources (see library.json).
idf_component_register(SRCS "src/tas_led.c"
                       INCLUDE_DIRS "include"
                       REQUIRES tas_wire tas_status)
target_compile_options(${COMPONENT_LIB} PRIVATE -std=c11 -Wall -Wextra -Werror)
