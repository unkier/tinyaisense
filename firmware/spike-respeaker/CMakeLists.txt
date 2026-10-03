cmake_minimum_required(VERSION 3.16)
# The real tas_cut, under test in slave mode with 32-bit slots.
list(APPEND EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_LIST_DIR}/../components/tas_audio)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(spike-respeaker)
