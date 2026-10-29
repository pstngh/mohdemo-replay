#!/bin/sh
#
# run.sh <renderer> <cfg name without .cfg> [+set args...]
#
# Runs the game once with renderer_<renderer>.so of $MOH_BINDIR (vulkan, or
# a copy of another build's renderer_vulkan.so under another name), the cfg
# from $MOH_HOMEPATH/main (see shots.py), a 640x480 window, no sound, the
# mouse ignored, no game message box (the "Game Saved", "An objective has
# been added!" lines of single-player maps), r_uselod 1 (upstream's default,
# as in the lists' older results; the renderer's is 0 since 2026-10-23) and
# r_vkValidation $VAL (default 1). main/configs and main/save are deleted first so every run
# starts from the same state. The console output goes to
# $MOH_OUT/<cfg>_<renderer>.log; validation messages are printed. Extra
# arguments are passed to the game (e.g. +set r_mode -1 ...).
# TIMEOUT (seconds, default 180) stops a run that hangs. MOH_BIN (default
# openmohaa) runs another engine binary of $MOH_BINDIR (a copy of an older
# build, to check that an engine change keeps the frames).

. "$(dirname "$0")/env.sh"

R=$1; CFG=$2; shift 2
LOG=$MOH_OUT/${CFG}_$R.log
rm -rf "$MOH_HOMEPATH/main/configs" "$MOH_HOMEPATH/main/save"
cd "$MOH_BINDIR" || exit 2
timeout "${TIMEOUT:-180}" "./${MOH_BIN:-openmohaa}" +set fs_basepath "$MOH_BASEPATH" +set fs_homepath "$MOH_HOMEPATH" \
	+set cl_renderer "$R" +set r_fullscreen 0 +set r_mode 3 +set s_volume 0 +set s_musicvolume 0 \
	+set s_ambientvolume 0 +set in_nograb 1 +set in_mouse 0 +set ui_gmbox 0 +set com_maxfps 60 +set r_uselod 1 +set r_vkValidation "${VAL:-1}" \
	"$@" +exec "$CFG.cfg" > "$LOG" 2>&1
STATUS=$?
[ $STATUS -ne 0 ] && echo "$R $CFG: exit status $STATUS, see $LOG"
grep "Vulkan validation" "$LOG" | grep -v "validation enabled"
exit $STATUS
