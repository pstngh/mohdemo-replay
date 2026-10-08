#
# Demo index tests, on real demos (see test_demoindex.cpp)
#

add_executable(test_demoindex
    ${SOURCE_DIR}/client/tests/test_demoindex.cpp
    ${SOURCE_DIR}/client/cl_demoindex.cpp
    ${SOURCE_DIR}/qcommon/msg.cpp
    ${SOURCE_DIR}/qcommon/huffman.cpp
    ${SOURCE_DIR}/qcommon/bg_compat.cpp
    ${SOURCE_DIR}/qcommon/q_shared.c
    ${SOURCE_DIR}/qcommon/q_math.c
    ${SOURCE_DIR}/qcommon/common_light.c
)

add_test(NAME test_demoindex COMMAND test_demoindex)
set_tests_properties(test_demoindex PROPERTIES TIMEOUT 600 SKIP_RETURN_CODE 77)
