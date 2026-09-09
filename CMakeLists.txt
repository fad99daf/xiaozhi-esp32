# The following lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

# Add this line to disable the specific warning
add_compile_options(-Wno-missing-field-initializers)

set(EXTRA_COMPONENT_DIRS "${CMAKE_SOURCE_DIR}/components")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# "Trim" the build. Include the minimal set of components, main, and anything it depends on.
idf_build_set_property(MINIMAL_BUILD ON)

set(PROJECT_VER "2.2.8")
project(xiaozhi)

# TAI buffer sizes: upstream defaults (~37 KB total context) assume PSRAM.
# On C3-class targets (no PSRAM, ~120 KB SRAM) shrink the reassembly buffer and
# per-fragment size so the context fits in internal RAM alongside WiFi/BLE.
# Must be global (after project()): struct tai_ctx is defined in esp-agentic-kit,
# sized in main — both must see the same layout.
if(CONFIG_IDF_TARGET_ESP32C3 OR CONFIG_IDF_TARGET_ESP32C5 OR CONFIG_IDF_TARGET_ESP32C6)
    idf_build_set_property(COMPILE_DEFINITIONS "TAI_MAX_FRAGMENT_PAYLOAD=1024;TAI_FRAG_BUF_SIZE=8192" APPEND)
endif()
