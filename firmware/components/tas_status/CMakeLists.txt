# Pure C with no IDF calls: the native test env builds the same sources (see library.json).
idf_component_register(SRCS "src/tas_status.c"
                       INCLUDE_DIRS "include")
target_compile_options(${COMPONENT_LIB} PRIVATE -std=c11 -Wall -Wextra -Werror)
