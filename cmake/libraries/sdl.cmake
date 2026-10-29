if(NOT BUILD_CLIENT)
    return()
endif()

# SDL 3 (3.2.0 or later; the game ships 3.4.18). The code includes
# <SDL3/SDL.h> with the internal headers and with an SDL 3 package alike.
set(INTERNAL_SDL_DIR ${SOURCE_DIR}/thirdparty/SDL3-3.4.18)

include(utils/arch)

if(WIN32)
    # On Windows we have internal SDL binaries we can use
    set(HAVE_INTERNAL_SDL true)
endif()

if(USE_INTERNAL_SDL AND HAVE_INTERNAL_SDL)
    set(SDL3_INCLUDE_DIRS ${INTERNAL_SDL_DIR}/include)

    if(WIN32)
        if(ARCH STREQUAL "x86_64")
            set(LIB_DIR ${SOURCE_DIR}/thirdparty/libs/win64)
        elseif(ARCH STREQUAL "x86")
            set(LIB_DIR ${SOURCE_DIR}/thirdparty/libs/win32)
        else()
            message(FATAL_ERROR "Unknown ARCH")
        endif()

        # SDL 3 has no SDLmain library: SDL_main.h gives the client its
        # WinMain (code/sys/sys_main.c)
        if(MINGW)
            set(SDL3_LIBRARIES ${LIB_DIR}/libSDL3.dll.a)
        elseif(MSVC)
            set(SDL3_LIBRARIES ${LIB_DIR}/SDL3.lib)
        endif()

        list(APPEND CLIENT_DEPLOY_LIBRARIES ${LIB_DIR}/SDL3.dll)
    else()
        message(FATAL_ERROR "HAVE_INTERNAL_SDL set incorrectly; file a bug")
    endif()
else()
    # The shared library: the client and the renderer must share one SDL
    # (the renderer's window is the client's input window)
    find_package(SDL3 3.2.0 REQUIRED CONFIG COMPONENTS SDL3-shared)
    set(SDL3_LIBRARIES SDL3::SDL3-shared)
endif()

list(APPEND CLIENT_LIBRARIES ${SDL3_LIBRARIES})
list(APPEND CLIENT_INCLUDE_DIRS ${SDL3_INCLUDE_DIRS})
list(APPEND RENDERER_LIBRARIES ${SDL3_LIBRARIES})
list(APPEND RENDERER_INCLUDE_DIRS ${SDL3_INCLUDE_DIRS})
