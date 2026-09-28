idf_component_register(SRCS "main.c"
                       PRIV_REQUIRES tas_core tas_board)
target_compile_options(${COMPONENT_LIB} PRIVATE -std=c11 -Wall -Wextra -Werror)
