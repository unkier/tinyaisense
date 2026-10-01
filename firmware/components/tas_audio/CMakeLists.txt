# Pure C with no IDF calls: the native test env builds the same sources (see library.json).
idf_component_register(SRCS "src/tas_ramp.c" "src/tas_jbuf.c" "src/tas_cut.c" "src/tas_dsp.c"
                            "src/tas_stats.c"
                       INCLUDE_DIRS "include")
target_compile_options(${COMPONENT_LIB} PRIVATE -std=c11 -Wall -Wextra -Werror)
