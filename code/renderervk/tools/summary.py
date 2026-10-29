#!/usr/bin/env python3
"""The worst view of each list in compare.sh results (standard library only).

  summary.py <list>... [--wide] [--readme README.md]

For each view list, reads $MOH_OUT/<list>.txt (or <list>_w.txt with --wide)
that compare.sh wrote and prints the worst view's numbers: the highest
per-channel max over the views, the highest mean, the largest share of
differing pixels and the largest number of pixels off by more than 3. With
--readme, also prints the row of the milestone 6 table for comparison.
"""

import argparse
import os
import re


def parse(path):
    views = []
    cur = None
    with open(path) as f:
        for line in f:
            m = re.match(r"== (\S+)", line)
            if m:
                cur = {"name": m.group(1), "max": [0, 0, 0], "mean": [0.0, 0.0, 0.0], "diff": 0.0, "gt3": 0}
                views.append(cur)
                continue
            if cur is None:
                continue
            m = re.match(r"\s+([RGB]): max\s+(\d+)\s+mean ([\d.]+)", line)
            if m:
                i = "RGB".index(m.group(1))
                cur["max"][i] = int(m.group(2))
                cur["mean"][i] = float(m.group(3))
            m = re.search(r"differing pixels .*: \d+ \(([\d.]+)%\)", line)
            if m:
                cur["diff"] = float(m.group(1))
            m = re.search(r"by max channel diff: 1: \d+, 2-3: \d+, 4-15: (\d+), 16-63: (\d+), >=64: (\d+)", line)
            if m:
                cur["gt3"] = sum(int(x) for x in m.groups())
    return views


def readme_rows(path):
    rows = {}
    with open(path) as f:
        for line in f:
            m = re.match(r"\| `([^`]+)` \((\d+)\) \| (.*)", line)
            if m and line.count("|") == 8:
                cells = [c.strip() for c in m.group(3).rstrip(" |\n").split("|")]
                rows[m.group(1)] = cells
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("lists", nargs="+")
    ap.add_argument("--wide", action="store_true")
    ap.add_argument("--readme")
    args = ap.parse_args()
    out = os.environ.get("MOH_OUT") or os.path.join(os.environ.get("MOH_HOMEPATH", "/tmp/moh-vktest"), "compare")
    ref = readme_rows(args.readme) if args.readme else {}
    for lst in args.lists:
        name = os.path.basename(lst).replace(".txt", "")
        path = os.path.join(out, name + ("_w" if args.wide else "") + ".txt")
        if not os.path.exists(path):
            print(f"{name}: no results ({path})")
            continue
        views = parse(path)
        if not views:
            print(f"{name}: empty")
            continue
        mx = [max(v["max"][i] for v in views) for i in range(3)]
        mean = max(max(v["mean"]) for v in views)
        worst = max(views, key=lambda v: v["diff"])
        gt3 = max(v["gt3"] for v in views)
        line = (f"{name:24s} ({len(views)}) {mx[0]}/{mx[1]}/{mx[2]:<4} mean<={mean:.3f}  {worst['diff']:.3g} % ({worst['name']})"
                f"  >3: {gt3}")
        if name in ref:
            cells = ref[name]
            line += "   README: " + (" ".join(cells[3:6]) if args.wide else " ".join(cells[0:3]))
        print(line)


if __name__ == "__main__":
    main()
