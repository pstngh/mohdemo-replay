# Linux-like specific settings, i.e. including FreeBSD etc.

if(NOT UNIX)
    return()
endif()

list(APPEND CLIENT_DEFINITIONS USE_ICON)
list(APPEND RENDERER_DEFINITIONS USE_ICON)

# The installed binaries look for the libraries in their own folder first:
# the package ships libSDL3.so.0 beside them (Ubuntu 22.04 and 24.04 and
# Debian 12 have no SDL 3). The build tree keeps CMake's paths.
set(CMAKE_INSTALL_RPATH "$ORIGIN")

if(CMAKE_INSTALL_PREFIX_INITIALIZED_TO_DEFAULT)
    set_property(CACHE CMAKE_INSTALL_PREFIX PROPERTY VALUE /opt/mohaa)
endif()

set(CPACK_GENERATOR "DEB")
set(CPACK_PACKAGING_INSTALL_PREFIX ${CMAKE_INSTALL_PREFIX})
