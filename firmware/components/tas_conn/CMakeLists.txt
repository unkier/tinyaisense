# Pure C with no IDF calls: the native test env builds the same sources (see library.json).
idf_component_register(SRCS "src/tas_conn.c" "src/tas_backoff.c"
                       INCLUDE_DIRS "include"
                       REQUIRES tas_wire)
target_compile_options(${COMPONENT_LIB} PRIVATE -std=c11 -Wall -Wextra -Werror)
