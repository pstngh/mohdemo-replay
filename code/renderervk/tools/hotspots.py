#!/usr/bin/env python3
"""Zoomed views of where two screenshots differ most (standard library only).

  hotspots.py a.tga b.tga out.png [--threshold 16] [--cell 32] [--count 6]
              [--zoom 6] [--rect x,y,w,h ...]

Splits the image into cells, ranks them by the number of pixels whose largest
channel difference is >= threshold, and writes one row per cell: a | b |
amplified difference, each enlarged with nearest-neighbour sampling. --rect
picks the regions explicitly instead (top-left origin).
"""

import argparse
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tgadiff import read_image, write_png, parse_rect  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("out")
    ap.add_argument("--threshold", type=int, default=16)
    ap.add_argument("--cell", type=int, default=32)
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--zoom", type=int, default=6)
    ap.add_argument("--scale", type=int, default=8)
    ap.add_argument("--rect", type=parse_rect, action="append", default=[])
    args = ap.parse_args()

    w, h, pa = read_image(args.a)
    _, _, pb = read_image(args.b)

    rects = args.rect
    if not rects:
        c = args.cell
        counts = {}
        for y in range(h):
            for x in range(w):
                i = (y * w + x) * 3
                if max(abs(pa[i] - pb[i]), abs(pa[i + 1] - pb[i + 1]), abs(pa[i + 2] - pb[i + 2])) >= args.threshold:
                    key = (x // c, y // c)
                    counts[key] = counts.get(key, 0) + 1
        best = sorted(counts.items(), key=lambda kv: -kv[1])[:args.count]
        rects = [[cx * c, cy * c, c, c] for (cx, cy), _ in best]
        for (cx, cy), n in best:
            print(f"cell x={cx * c} y={cy * c}: {n} pixels >= {args.threshold}")
    if not rects:
        print("no differences above the threshold")
        return 0

    z = args.zoom
    gap = 4
    rw = max(r[2] for r in rects) * z
    out_w = rw * 3 + gap * 2
    rows = []
    for x0, y0, rwid, rhei in rects:
        for y in range(rhei):
            line = bytearray(out_w * 3)
            for panel in range(3):
                for x in range(rwid):
                    sx, sy = x0 + x, y0 + y
                    if 0 <= sx < w and 0 <= sy < h:
                        i = (sy * w + sx) * 3
                        if panel == 0:
                            px = pa[i:i + 3]
                        elif panel == 1:
                            px = pb[i:i + 3]
                        else:
                            px = bytes(min(255, abs(pa[i + k] - pb[i + k]) * args.scale) for k in range(3))
                    else:
                        px = b"\x00\x00\x00"
                    base = (panel * (rw + gap) + x * z) * 3
                    line[base:base + 3 * z] = px * z
            for _ in range(z):
                rows.append(bytes(line))
        for _ in range(gap):
            rows.append(bytes([64]) * (out_w * 3))
    write_png(args.out, out_w, len(rows), b"".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
