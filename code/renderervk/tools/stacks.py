#!/usr/bin/env python3
"""Sums up the stack samples of libstacks.so (rtimer/stacks.c, perf.py --stacks).

  stacks.py FILE [--top N] [--focus FUNCTION] [--callers FUNCTION]
            [--tree FUNCTION [--depth N]] [--all]

Prints, for the samples taken in the measured window (the `window` line
perf.py appends; every sample with --all), the share of wall-clock time
per function: self (the function was running or waiting itself) and
inclusive (anywhere on the stack), then the most frequent call paths
below FUNCTION (--focus, a substring), each frame as function [library],
the most frequent callers of FUNCTION (--callers), and the tree of the
functions FUNCTION calls with their inclusive shares down to N levels
(--tree, default 3; branches under 0.3 % are left out).
Addresses are symbolized with addr2line (binutils).
"""

import argparse
import collections
import os
import re
import subprocess
import sys


def load(path):
    maps, samples, window = [], [], None
    with open(path) as f:
        for line in f:
            if line.startswith("map "):
                m = re.match(r"map ([0-9a-f]+)-([0-9a-f]+) (\S+) ([0-9a-f]+) \S+ \d+\s*(.*)", line)
                if m and m.group(5).startswith("/"):
                    maps.append((int(m.group(1), 16), int(m.group(2), 16), int(m.group(4), 16), m.group(5).strip()))
            elif line.startswith("s "):
                w = line.split()
                samples.append((int(w[1]), [int(x, 16) for x in w[2:]]))
            elif line.startswith("window "):
                w = line.split()
                window = (int(w[1]), int(w[2]))
    return maps, samples, window


def module_of(maps, bases, addr):
    for start, end, off, path in maps:
        if start <= addr < end and path in bases:
            return path, addr - bases[path]
    return None, addr


def symbolize(maps, addrs):
    # load base of each file: the start of its mapping at file offset 0
    bases = {}
    for start, end, off, path in maps:
        if off == 0 and path not in bases:
            bases[path] = start
    per_module = collections.defaultdict(set)
    where = {}
    for a in addrs:
        path, rel = module_of(maps, bases, a)
        where[a] = (path, rel)
        if path:
            per_module[path].add(rel)
    names = {}
    for path, rels in per_module.items():
        rels = sorted(rels)
        try:
            out = subprocess.run(["addr2line", "-f", "-C", "-e", path], input="\n".join("%x" % r for r in rels),
                                 capture_output=True, text=True).stdout.splitlines()
        except OSError:
            out = []
        for i, rel in enumerate(rels):
            fn = out[2 * i] if 2 * i < len(out) else "??"
            names[(path, rel)] = fn if fn != "??" else "%s+0x%x" % (os.path.basename(path), rel)
    result = {}
    for a, (path, rel) in where.items():
        lib = os.path.basename(path) if path else "?"
        result[a] = "%s [%s]" % (names.get((path, rel), "0x%x" % a), lib)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--focus")
    ap.add_argument("--callers")
    ap.add_argument("--tree")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    maps, samples, window = load(args.file)
    if window and not args.all:
        samples = [s for s in samples if window[0] <= s[0] <= window[1]]
    if not samples:
        sys.exit("no samples")

    # return addresses point after the call: look them up one byte earlier;
    # the first frames are the handler and the signal trampoline
    stacks = []
    for ns, pcs in samples:
        for i, pc in enumerate(pcs):
            if pc and i >= 1 and any(start <= pc < end and "libstacks" not in path for start, end, off, path in maps):
                pcs = pcs[i:]
                break
        stacks.append([pcs[0]] + [pc - 1 for pc in pcs[1:]])
    names = symbolize(maps, {pc for st in stacks for pc in st})
    stacks = [[names[pc] for pc in st] for st in stacks]
    # drop the signal trampoline frame (libc's __restore_rt)
    stacks = [st[1:] if st and "__restore_rt" in st[0] else st for st in stacks]

    n = len(stacks)
    span = (samples[-1][0] - samples[0][0]) / 1e6
    print(f"{n} samples over {span:.0f} ms")
    self_ = collections.Counter(st[0] for st in stacks if st)
    incl = collections.Counter()
    for st in stacks:
        for fn in set(st):
            incl[fn] += 1
    print(f"\n{'self %':>7s} {'incl %':>7s}  function")
    for fn, c in incl.most_common(args.top):
        print(f"{100 * self_[fn] / n:7.1f} {100 * c / n:7.1f}  {fn}")
    print(f"\n{'self %':>7s}  function (by self time)")
    for fn, c in self_.most_common(args.top):
        print(f"{100 * c / n:7.1f}  {fn}")

    if args.focus:
        paths = collections.Counter()
        total = 0
        for st in stacks:
            for i, fn in enumerate(st):
                if args.focus in fn:
                    # the frames below the focus, innermost last
                    paths[" < ".join(st[max(0, i - 6):i + 1])] += 1
                    total += 1
                    break
        print(f"\ncall paths below {args.focus} ({100 * total / n:.1f} % of the samples), innermost first:")
        for p, c in paths.most_common(args.top):
            print(f"{100 * c / n:7.1f}  {p}")

    short = lambda fn: fn.split(" [")[0]
    if args.callers:
        callers = collections.Counter()
        for st in stacks:
            for i, fn in enumerate(st):
                if args.callers in fn:
                    callers[" < ".join(short(f) for f in st[i:i + 7])] += 1
                    break
        print(f"\ncallers of {args.callers}, innermost first:")
        for p, c in callers.most_common(args.top):
            print(f"{100 * c / n:7.1f}  {p}")

    if args.tree:
        # inclusive samples of each call path below the outermost frame
        # matching the function
        tree = collections.Counter()
        for st in stacks:
            outer = list(reversed(st))
            for i, fn in enumerate(outer):
                if args.tree in fn:
                    path = tuple(short(f) for f in outer[i:i + args.depth + 1])
                    for k in range(1, len(path) + 1):
                        tree[path[:k]] += 1
                    break

        def show(prefix):
            kids = sorted((k for k in tree if len(k) == len(prefix) + 1 and k[:len(prefix)] == prefix),
                          key=lambda k: -tree[k])
            for k in kids:
                if 100 * tree[k] / n >= 0.3:
                    print(f"{100 * tree[k] / n:7.1f}  {'  ' * (len(k) - 1)}{k[-1]}")
                    show(k)

        print(f"\ninclusive time below {args.tree}:")
        show(())


if __name__ == "__main__":
    main()
