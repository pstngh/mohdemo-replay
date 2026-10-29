#!/usr/bin/env python3
"""Grid of screenshots for side-by-side viewing (standard library only).

  montage.py out.png <columns> <downscale> image...

The images (TGA or PNG written by tgadiff.py, all the same size) are placed
left to right, top to bottom, each reduced by the integer downscale factor
(nearest neighbour), with a 4 pixel black gap. A typical row is
<view>_a.png <view>_b.png <view>_diff.png (compare.sh).
"""
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tgadiff import read_image, write_png
out, cols, sc = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
imgs = [read_image(p) for p in sys.argv[4:]]
w, h = imgs[0][0] // sc, imgs[0][1] // sc
rows = (len(imgs) + cols - 1) // cols
W, H = cols * w + (cols - 1) * 4, rows * h + (rows - 1) * 4
buf = bytearray(W * H * 3)
for k, (iw, ih, px) in enumerate(imgs):
    ox, oy = (k % cols) * (w + 4), (k // cols) * (h + 4)
    for y in range(h):
        for x in range(w):
            i = ((y * sc) * iw + x * sc) * 3
            o = ((oy + y) * W + ox + x) * 3
            buf[o:o+3] = px[i:i+3]
write_png(out, W, H, bytes(buf))
