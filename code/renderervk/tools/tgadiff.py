#!/usr/bin/env python3
"""Compare two screenshots of the same frame (standard library only).

Reads uncompressed 24/32-bit TGA files (the format of the `screenshot`
command: 18-byte header, BGR pixels, bottom row first) and PNG files written
by this script, then reports per-channel max and mean absolute difference and
the number of differing pixels. Optionally writes a diff PNG and PNG copies
of the inputs.

  tgadiff.py a.tga b.tga [--diff diff.png] [--scale 8] [--png-a a.png]
             [--png-b b.png] [--mask x,y,w,h ...] [--mask-res WxH]
             [--threshold N]

--diff     writes |a - b| per channel multiplied by --scale (default 8) and
           clamped to 255; pixels that are equal are drawn as a dark copy of
           image a so the differences can be located
--mask     rectangles (top-left origin, in pixels) left out of the
           statistics, e.g. a HUD element; may be repeated
--mask-res the masks are given for a WxH frame and scaled to the size of
           the images (rounded outwards), e.g. 640x480 for UI elements,
           which the UI stretches to the screen
--threshold pixels whose largest channel difference is <= N are counted as
           equal in the "differing pixels" figure (default 0)

Exit status: 0 if the images are identical outside the masks, 1 if they
differ, 2 on errors.
"""

import argparse
import struct
import sys
import zlib


def read_tga(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 18:
        raise ValueError(f"{path}: too short for a TGA header")
    id_len, cmap_type, img_type = data[0], data[1], data[2]
    width, height = struct.unpack_from("<HH", data, 12)
    bpp, desc = data[16], data[17]
    if cmap_type != 0 or img_type != 2 or bpp not in (24, 32):
        raise ValueError(f"{path}: only uncompressed 24/32-bit true-color TGA is supported "
                         f"(type {img_type}, {bpp} bpp)")
    bytes_pp = bpp // 8
    offset = 18 + id_len
    size = width * height * bytes_pp
    pix = data[offset:offset + size]
    if len(pix) != size:
        raise ValueError(f"{path}: truncated pixel data")
    top_first = bool(desc & 0x20)
    rows = []
    stride = width * bytes_pp
    for y in range(height):
        row = pix[y * stride:(y + 1) * stride]
        # BGR(A) -> RGB
        rgb = bytearray(width * 3)
        rgb[0::3] = row[2::bytes_pp]
        rgb[1::3] = row[1::bytes_pp]
        rgb[2::3] = row[0::bytes_pp]
        rows.append(bytes(rgb))
    if not top_first:
        rows.reverse()
    return width, height, b"".join(rows)


def read_png(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")
    pos = 8
    idat = b""
    width = height = None
    while pos < len(data):
        length, ctype = struct.unpack_from(">I4s", data, pos)
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or color != 2 or interlace != 0:
                raise ValueError(f"{path}: only 8-bit RGB non-interlaced PNG is supported")
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = width * 3
    out = bytearray(stride * height)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        ftype = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for i in range(stride):
            a = line[i - 3] if i >= 3 else 0
            b = prev[i]
            c = prev[i - 3] if i >= 3 else 0
            if ftype == 1:
                line[i] = (line[i] + a) & 255
            elif ftype == 2:
                line[i] = (line[i] + b) & 255
            elif ftype == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ftype == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, bytes(out)


def read_image(path):
    if path.lower().endswith(".png"):
        return read_png(path)
    return read_tga(path)


def write_png(path, width, height, rgb):
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(ctype, payload):
        c = struct.pack(">I", len(payload)) + ctype + payload
        return c + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))


def parse_rect(text):
    parts = [int(v) for v in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("mask is x,y,w,h")
    return parts


def parse_res(text):
    parts = [int(v) for v in text.lower().split("x")]
    if len(parts) != 2 or min(parts) <= 0:
        raise argparse.ArgumentTypeError("resolution is WxH")
    return parts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--diff")
    ap.add_argument("--scale", type=int, default=8)
    ap.add_argument("--png-a")
    ap.add_argument("--png-b")
    ap.add_argument("--mask", type=parse_rect, action="append", default=[])
    ap.add_argument("--mask-res", type=parse_res)
    ap.add_argument("--threshold", type=int, default=0)
    args = ap.parse_args()

    try:
        wa, ha, pa = read_image(args.a)
        wb, hb, pb = read_image(args.b)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if (wa, ha) != (wb, hb):
        print(f"error: sizes differ: {wa}x{ha} vs {wb}x{hb}", file=sys.stderr)
        return 2
    w, h = wa, ha

    if args.mask_res:
        rw, rh = args.mask_res
        # integer arithmetic: floor of the start, ceiling of the end
        args.mask = [(x * w // rw, y * h // rh, -(-(x + mw) * w // rw) - x * w // rw, -(-(y + mh) * h // rh) - y * h // rh)
                     for x, y, mw, mh in args.mask]

    masked = bytearray(w * h)
    for x, y, mw, mh in args.mask:
        for yy in range(max(y, 0), min(y + mh, h)):
            masked[yy * w + max(x, 0):yy * w + min(x + mw, w)] = b"\x01" * (min(x + mw, w) - max(x, 0))

    # per-channel absolute differences
    diff = bytes(abs(x - y) for x, y in zip(pa, pb))

    ch_max = [0, 0, 0]
    ch_sum = [0, 0, 0]
    counted = 0
    differing = 0
    buckets = {"1": 0, "2-3": 0, "4-15": 0, "16-63": 0, ">=64": 0}
    for i in range(w * h):
        if masked[i]:
            continue
        counted += 1
        d = diff[i * 3:i * 3 + 3]
        m = max(d)
        if m:
            for c in range(3):
                ch_sum[c] += d[c]
                if d[c] > ch_max[c]:
                    ch_max[c] = d[c]
            if m == 1:
                buckets["1"] += 1
            elif m < 4:
                buckets["2-3"] += 1
            elif m < 16:
                buckets["4-15"] += 1
            elif m < 64:
                buckets["16-63"] += 1
            else:
                buckets[">=64"] += 1
            if m > args.threshold:
                differing += 1

    names = "RGB"
    print(f"{w}x{h}, {counted} pixels compared" + (f" ({w * h - counted} masked)" if counted != w * h else ""))
    for c in range(3):
        print(f"  {names[c]}: max {ch_max[c]:3d}  mean {ch_sum[c] / max(counted, 1):.4f}")
    print(f"  differing pixels (max channel diff > {args.threshold}): {differing}"
          f" ({100.0 * differing / max(counted, 1):.3f}%)")
    print("  by max channel diff: " + ", ".join(f"{k}: {v}" for k, v in buckets.items()))

    if args.diff:
        out = bytearray(w * h * 3)
        for i in range(w * h):
            d = diff[i * 3:i * 3 + 3]
            if masked[i]:
                out[i * 3:i * 3 + 3] = b"\x00\x00\x40"
            elif max(d):
                out[i * 3:i * 3 + 3] = bytes(min(255, v * args.scale) for v in d)
            else:
                out[i * 3:i * 3 + 3] = bytes(v // 5 for v in pa[i * 3:i * 3 + 3])
        write_png(args.diff, w, h, out)
    if args.png_a:
        write_png(args.png_a, w, h, pa)
    if args.png_b:
        write_png(args.png_b, w, h, pb)

    return 1 if differing else 0


if __name__ == "__main__":
    sys.exit(main())
