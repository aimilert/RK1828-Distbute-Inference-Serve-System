cmake_minimum_required(VERSION 3.15)

project(rk182x_multisession LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ---------------------------------------------------------------------------
# Rockchip prebuilt libraries
#
# The runtime/tokenizer binaries shipped with the RKNN3 SDK are vendored under
# third_party/ for the supported Linux targets. The defaults below select them;
# override any of them on the command line if you use a different SDK build.
#
#   TARGET_ARCH     aarch64 (RK3588 board) or x86_64 (host)
#   LIBRKNNAPI      path to librknn3_api.so
#   LIBRKNNAPI_RKCP path to librknn3_api_rkcp.so (installed next to it)
#   LIBRGA          path to librga.so (installed next to it; optional)
#   LIBTOKENIZER    path to libtokenizer.a
# ---------------------------------------------------------------------------
set(TARGET_ARCH "aarch64" CACHE STRING "Target arch used to pick the vendored libs")
set(_VENDOR "${CMAKE_CURRENT_SOURCE_DIR}/third_party")

set(LIBRKNNAPI "${_VENDOR}/rknpu3/Linux/${TARGET_ARCH}/librknn3_api.so"
    CACHE FILEPATH "Path to librknn3_api.so")
set(LIBRKNNAPI_RKCP "${_VENDOR}/rknpu3/Linux/${TARGET_ARCH}/librknn3_api_rkcp.so"
    CACHE FILEPATH "Path to librknn3_api_rkcp.so")
set(LIBRGA "${_VENDOR}/librga/Linux/${TARGET_ARCH}/librga.so"
    CACHE FILEPATH "Path to librga.so (optional)")
set(LIBTOKENIZER "${_VENDOR}/tokenizer/lib/Linux/${TARGET_ARCH}/libtokenizer.a"
    CACHE FILEPATH "Path to libtokenizer.a")

if(NOT EXISTS "${LIBRKNNAPI}")
    message(FATAL_ERROR
        "librknn3_api.so not found at '${LIBRKNNAPI}'.\n"
        "Set -DTARGET_ARCH=<aarch64|x86_64> or -DLIBRKNNAPI=/path/to/librknn3_api.so")
endif()

if(NOT EXISTS "${LIBTOKENIZER}")
    message(FATAL_ERROR
        "libtokenizer.a not found at '${LIBTOKENIZER}'.\n"
        "Set -DTARGET_ARCH=<aarch64|x86_64> or -DLIBTOKENIZER=/path/to/libtokenizer.a")
endif()

# The deployed binary looks for the shared libraries next to itself, under lib/.
set(CMAKE_INSTALL_RPATH "$ORIGIN/lib")

add_executable(rknn_multicard_demo src/main.cc)

target_include_directories(rknn_multicard_demo PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include)

target_link_libraries(rknn_multicard_demo PRIVATE
    "${LIBRKNNAPI}"
    "${LIBTOKENIZER}"
    dl
    pthread)

install(TARGETS rknn_multicard_demo DESTINATION .)
install(FILES "${LIBRKNNAPI}" DESTINATION lib)

foreach(_lib "${LIBRKNNAPI_RKCP}" "${LIBRGA}")
    if(EXISTS "${_lib}")
        install(FILES "${_lib}" DESTINATION lib)
    endif()
endforeach()
