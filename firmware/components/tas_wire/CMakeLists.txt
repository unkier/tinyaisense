# Pure C with no IDF calls: the native test env builds the same sources (see library.json).
idf_component_register(SRCS "src/tas_json.c" "src/tas_wire.c" "src/tas_args.c"
                       INCLUDE_DIRS "include")
target_compile_options(${COMPONENT_LIB} PRIVATE -std=c11 -Wall -Wextra -Werror)
