#!/bin/sh
#
# cmplists.sh <out dir> <variants> <list>...
#
# Build against build over many view lists: runs every list with each
# variant (run.sh: 640x480, r_vkValidation $VAL, default 1) and compares
# each variant's shots with those of the first variant, the reference
# (tgadiff.py, with the list's masks). variants: comma separated
#   name=renderer[=r_vkFallbacks[=binary]]
# renderer: a renderer of $MOH_BINDIR (renderer_<renderer>.so), binary: an
# engine binary there (default openmohaa; a wrapper script `exec <other bin
# dir>/openmohaa "$@"` runs a build with its own cgame.so). EXTRA holds more
# +set arguments for every run. Results: <out dir>/<variant>/<view>.tga, the
# logs in <out dir>/logs/<variant>/, and <out dir>/summary.txt with one line
# per view and variant ("identical" or "DIFFERS <pixels> px, max r/g/b"),
# "<list> done" after each list and ALLDONE at the end. Some views differ
# between two runs of the same build (README.md, Testing): add a second
# variant with the reference's renderer to see that noise.
# Example: cmplists.sh out base=base,new=vulkan views/mohdm1.txt views/m5l1a.txt

. "$(dirname "$0")/env.sh"

SS=$MOH_HOMEPATH/main/screenshots
OUT=$1; VARIANTS=$2; shift 2
mkdir -p "$OUT"
FIRST=${VARIANTS%%=*}
for LIST in "$@"; do
	CFG=$(basename "$LIST" .txt)
	# vehicle starts ignore the teleports (README.md, Testing)
	case $CFG in m1l1) TO=300;; m3l1a) TO=420;; *) TO=200;; esac
	python3 "$TOOLS/shots.py" "$LIST" "$CFG" >/dev/null || { echo "$CFG: shots.py failed" >> "$OUT/summary.txt"; continue; }
	SHADER=${LIST%.txt}.shader; INSTALLED=
	if [ -f "$SHADER" ]; then
		INSTALLED=$MOH_HOMEPATH/main/scripts/aaa_$CFG.shader
		mkdir -p "$MOH_HOMEPATH/main/scripts"; cp "$SHADER" "$INSTALLED"
	fi
	VIEWS=$(awk '$1 == "shot" || $1 == "menu" { print $2 }' "$LIST")
	for V in $(echo "$VARIANTS" | tr , ' '); do
		NAME=${V%%=*}; REST=${V#*=}; R=${REST%%=*}
		FB=0; BIN=openmohaa
		case $REST in *=*) REST=${REST#*=}; FB=${REST%%=*}; case $REST in *=*) BIN=${REST#*=};; esac;; esac
		mkdir -p "$OUT/$NAME" "$OUT/logs/$NAME"
		for v in $VIEWS; do rm -f "$SS/$v.tga"; done
		MOH_BIN=$BIN MOH_OUT=$OUT/logs/$NAME TIMEOUT=$TO sh "$TOOLS/run.sh" "$R" "$CFG" +set r_vkFallbacks "$FB" $EXTRA \
			> "$OUT/logs/$NAME/$CFG.run" 2>&1 < /dev/null
		[ -s "$OUT/logs/$NAME/$CFG.run" ] && sed "s/^/$CFG $NAME: /" "$OUT/logs/$NAME/$CFG.run" >> "$OUT/summary.txt"
		for v in $VIEWS; do
			if [ -f "$SS/$v.tga" ]; then mv "$SS/$v.tga" "$OUT/$NAME/$v.tga"; else echo "$CFG $v $NAME: missing" >> "$OUT/summary.txt"; fi
		done
	done
	[ -n "$INSTALLED" ] && rm -f "$INSTALLED"
	for v in $VIEWS; do
		MASKS=$(awk -v v="$v" '$1 == "mask" && $2 == v { printf "--mask %s ", $3 }' "$LIST")
		[ -n "$MASKS" ] && MASKS="$MASKS --mask-res 640x480"
		for V in $(echo "$VARIANTS" | tr , ' '); do
			NAME=${V%%=*}; [ "$NAME" = "$FIRST" ] && continue
			[ -f "$OUT/$FIRST/$v.tga" ] && [ -f "$OUT/$NAME/$v.tga" ] || continue
			if python3 "$TOOLS/tgadiff.py" "$OUT/$FIRST/$v.tga" "$OUT/$NAME/$v.tga" $MASKS > "$OUT/logs/$NAME/$v.diff" 2>&1; then
				echo "$CFG $v $NAME: identical" >> "$OUT/summary.txt"
			else
				echo "$CFG $v $NAME: DIFFERS $(grep 'differing pixels' "$OUT/logs/$NAME/$v.diff" | sed 's/.*: //') px, max $(grep -o 'max *[0-9]*' "$OUT/logs/$NAME/$v.diff" | awk '{print $2}' | tr '\n' /)" >> "$OUT/summary.txt"
			fi
		done
	done
	echo "$CFG done $(date +%T)" >> "$OUT/summary.txt"
done
echo ALLDONE >> "$OUT/summary.txt"
