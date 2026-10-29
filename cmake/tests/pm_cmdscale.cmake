#
# Movement wish speeds must match the stock games (client prediction)
#

add_executable(test_pm_cmdscale
    ${SOURCE_DIR}/fgame/tests/test_pm_cmdscale.cpp
    ${SOURCE_DIR}/fgame/bg_slidemove.cpp
    ${SOURCE_DIR}/qcommon/q_math.c
    ${SOURCE_DIR}/qcommon/q_shared.c
    ${SOURCE_DIR}/qcommon/common_light.c
)

target_include_directories(test_pm_cmdscale PRIVATE
    ${SOURCE_DIR}
    ${SOURCE_DIR}/fgame
    ${SOURCE_DIR}/qcommon
)

add_test(NAME test_pm_cmdscale COMMAND test_pm_cmdscale)
set_tests_properties(test_pm_cmdscale PROPERTIES TIMEOUT 15)
