#
# The replay app's tests, with a fake game (see replay/tests/test_mohreplay.py)
#

# the python3 run from the shell, which has PySide6
find_program(PYTHON3_PROGRAM python3)

if(PYTHON3_PROGRAM)
    add_test(NAME test_mohreplay
        COMMAND ${PYTHON3_PROGRAM} ${CMAKE_SOURCE_DIR}/replay/tests/test_mohreplay.py)
    set_tests_properties(test_mohreplay PROPERTIES
        TIMEOUT 600 SKIP_RETURN_CODE 77 ENVIRONMENT QT_QPA_PLATFORM=offscreen)
endif()
