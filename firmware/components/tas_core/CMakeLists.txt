idf_component_register(SRCS "core.c"
                       INCLUDE_DIRS "include"
                       REQUIRES tas_board
                       PRIV_REQUIRES tas_kinds log heap)
target_compile_options(${COMPONENT_LIB} PRIVATE -std=c11 -Wall -Wextra -Werror)
