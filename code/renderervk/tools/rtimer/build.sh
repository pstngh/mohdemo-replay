#!/bin/sh
# build.sh -- builds renderer_timed.so (rtimer.c), libstacks.so (stacks.c)
# and libinlat.so (inlat.c) into $MOH_BINDIR
# (default build-vk/RelWithDebInfo), see rtimer.c and ../perf.py
DIR=$(cd "$(dirname "$0")" && pwd)
MOH_BINDIR=${MOH_BINDIR:-$DIR/../../../../build-vk/RelWithDebInfo}
cc -O2 -Wall -Wno-unused-variable -Wno-unused-function -DUSE_RENDERER_DLOPEN -shared -fPIC \
	-o "$MOH_BINDIR/renderer_timed.so" "$DIR/rtimer.c" -ldl -lm
cc -O2 -Wall -shared -fPIC -o "$MOH_BINDIR/libstacks.so" "$DIR/stacks.c" -lpthread
cc -O2 -Wall -shared -fPIC -I/usr/include/libdrm -o "$MOH_BINDIR/libinlat.so" "$DIR/inlat.c" -ldl -ldrm -lpthread
