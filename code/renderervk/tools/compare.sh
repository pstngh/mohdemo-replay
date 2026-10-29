#!/bin/sh
#
# compare.sh <view list> [+set args...]
#
# Turns the view list into a cfg (shots.py), runs it with two builds (run.sh)
# and compares every screenshot with tgadiff.py: A, the reference, and B, the
# build under test. A and B (default base and vulkan) are renderers of
# $MOH_BINDIR (renderer_<name>.so: keep a copy of the build before as
# renderer_base.so); A_BIN and B_BIN (default openmohaa) are engine binaries
# of $MOH_BINDIR, e.g. a copy of an older openmohaa, or a wrapper script
# `exec <other bin dir>/openmohaa "$@"` for a build with its own cgame.so
# (cgame is loaded from the binary's directory). Results:
#   $MOH_HOMEPATH/main/screenshots/<view>[_w]_{a,b}.tga
#   $MOH_OUT/<view>[_w]_{a,b,diff}.png
#   $MOH_OUT/<list>[_w].txt (the statistics of every view)
# WIDE=1 runs at 4160x240: wider than 4096 pixels RADV rasterizes with 1/256
# pixel precision (see README.md, Testing). DIFFARGS is passed to
# tgadiff.py. Some views differ between two runs of the same build (see
# README.md, Testing): compare with a same-build pair (A=vulkan B=vulkan)
# before reading anything into small differences.
# Shots taken from outside the world (view cluster -1, see shots.py) are
# reported: parts of their frame are undefined.
# Menu shots (`menu` lines) are compared like the others; `mask <view>
# x,y,w,h` lines of the list leave a rectangle of the 640x480 UI screen out
# of that view's statistics (scaled to the frame size, e.g. the ceiling fan
# of the main menu, which turns with the wall clock).
# A <list>.shader file next to the view list is installed as
# main/scripts/aaa_<list>.shader for the runs (the first shader file in
# alphabetical order overrides the others), to test shaders no game data has.
# For many lists and more than two builds: cmplists.sh.

. "$(dirname "$0")/env.sh"

LIST=$1; shift
CFG=$(basename "$LIST" .txt)
A=${A:-base}; B=${B:-vulkan}
TAG=
if [ -n "$WIDE" ]; then
	TAG=_w
	set -- +set r_mode -1 +set r_customwidth 4160 +set r_customheight 240 "$@"
fi
SS=$MOH_HOMEPATH/main/screenshots
python3 "$TOOLS/shots.py" "$LIST" "$CFG" >/dev/null || exit 2
SHADER=${LIST%.txt}.shader
INSTALLED=
if [ -f "$SHADER" ]; then
	INSTALLED=$MOH_HOMEPATH/main/scripts/aaa_$CFG.shader
	mkdir -p "$MOH_HOMEPATH/main/scripts"
	cp "$SHADER" "$INSTALLED"
fi
VIEWS=$(awk '$1 == "shot" || $1 == "menu" { print $2 }' "$LIST")

for S in a b; do
	if [ $S = a ]; then R=$A; BIN=${A_BIN:-openmohaa}; else R=$B; BIN=${B_BIN:-openmohaa}; fi
	for v in $VIEWS; do rm -f "$SS/$v.tga"; done
	# the logs of the two runs apart, also when A and B are the same renderer
	MOH_BIN=$BIN MOH_OUT=$MOH_OUT/$S "$TOOLS/run.sh" "$R" "$CFG" "$@"
	for v in $VIEWS; do
		if [ -f "$SS/$v.tga" ]; then
			mv "$SS/$v.tga" "$SS/$v${TAG}_$S.tga"
		else
			echo "missing $v ($S: $R, $BIN)"
		fi
	done
done

[ -n "$INSTALLED" ] && rm -f "$INSTALLED"

awk '/^cluster:/ { c = $1 } $1 == "shot" && c == "cluster:-1" { print "warning: " $2 ": the camera is outside the world (cluster -1)" }' \
	"$MOH_OUT/b/${CFG}_$B.log"

SUMMARY=$MOH_OUT/$CFG$TAG.txt
: > "$SUMMARY"
for v in $VIEWS; do
	MASKS=$(awk -v v="$v" '$1 == "mask" && $2 == v { printf "--mask %s ", $3 }' "$LIST")
	[ -n "$MASKS" ] && MASKS="$MASKS --mask-res 640x480"
	echo "== $v$TAG" | tee -a "$SUMMARY"
	python3 "$TOOLS/tgadiff.py" "$SS/$v${TAG}_a.tga" "$SS/$v${TAG}_b.tga" --diff "$MOH_OUT/$v${TAG}_diff.png" \
		--png-a "$MOH_OUT/$v${TAG}_a.png" --png-b "$MOH_OUT/$v${TAG}_b.png" $MASKS $DIFFARGS | tee -a "$SUMMARY"
done
