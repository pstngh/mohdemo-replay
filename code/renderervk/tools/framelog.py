#!/usr/bin/env python3
"""Frame time distributions and the causes of the slowest frames (standard library only).

  framelog.py FILE...             summary of each file
  framelog.py --worst N FILE...   also the N slowest frames of each, with their parts and events
  framelog.py --batch DIR         a tools/perf.py --framelog directory: per view, size
                                  and renderer the medians over the runs, the causes
                                  of the slow frames, and the renderers against the
                                  first one (by name) frame by frame (demo views show
                                  the same frames in every run)
  framelog.py --svg OUT.svg ...   with --batch: frame time percentile curves and the
                                  frame times of the first run of each view and size
                                  (--labels base=Before,vulkan=After,...: the renderers
                                  drawn, their names and the order of the colors)
  framelog.py --display DIR       when each frame reached the display: a perf.py --framelog
                                  directory of runs with libinlat.so and INLAT_DISPLAY=1
                                  (its .pt.txt files), per view and variant the input to
                                  present, GPU done, X11 completion and display times, the
                                  share of frames shown, direct scanout, tearing and the
                                  slack before the compositor's latch (--table: markdown,
                                  --labels variant=name,...; --json OUT: every value)
  framelog.py --stacks S RT       what the main thread was doing in the slow frames of
                                  renderer_timed.so's frame file RT, from the stack
                                  samples S of the same run (perf.py --stacks and
                                  --framelog): functions by their share of the samples
                                  in slow frames against all frames, and with --worst N
                                  the samples of the N slowest frames

FILE is a frame log of the Vulkan renderer (`r_vkFrameLog 1`, then
`vkframelog [file]`, written to <fs_homepath>/main) or the frames that
renderer_timed.so writes to $RTIMER_FRAMES (tools/rtimer/rtimer.c), for
either renderer. Both have one line per frame, in microseconds: the frame
time (`interval`, from one BeginFrame to the next) and its parts, the time in
the renderer's exports (`begin`, `scene`: the front end, `2d`: the 2D calls,
in which the 3D view's back end runs in OpenMoHAA, `end`: EndFrame, `load`:
the registration calls), `cgame` (the rest of BeginFrame..EndFrame: the UI and
cgame) and `engine` (EndFrame's return to the next BeginFrame: sound, the
frame limiter's sleep, events, the local server). The Vulkan log adds what
the backend did in the frame: the fence wait at its start, mid-frame flushes,
the submission, acquires and present, pipelines, textures and memory created,
uploads, the GPU time of the main command buffer and when it started and
ended on the GPU.

The causes: each slow frame (above the run's 99th percentile, or --above MS)
is split into parts that don't overlap (engine, cgame, load, scene, and for
the Vulkan log the fence wait, flushes, pipeline creation, submit/present/
acquire, the wait for the submit thread (r_vkSubmitThread) and the rest of
the back end; for rtimer files the back end with the swap) and every part's
excess over its median in the run is summed: which parts make the slow
frames slow. The frame's events (pipelines, textures, uploads, allocations,
flushes, a vertex buffer grown, the depth image or swapchain replaced) are
counted too. With r_vkSubmitThread the log also has the submit thread's
times for each frame (from the hand-off to its start, its vkQueueSubmit,
acquire and present), printed as distributions and with the slowest frames.

Input to present: FILE can also be libinlat.so's $INLAT_FRAMES
(tools/rtimer/inlat.c; perf.py --preload libinlat.so --framelog), every
frame's first input poll and its present; --batch keeps the frames
renderer_timed.so measured and prints, per renderer, the time from the
input read to the present (what a frame limiter or the renderer must not
lengthen) and the intervals between input reads (the frame starts) and
between presents (what reaches the display).

Present timing (--display, see code/renderervk/README.md, Performance,
Present timing): display_times() says how each case is worked out (Wayland
presentation feedback, X11 completions and frame callbacks, the vblanks,
the framebuffer changes with tearing) and scanout_agreement() how direct
scanout checks it.

Percentiles: the value at index int(n * q) of the sorted frame times
(p99 of 15000 frames is the 150th slowest, p99.9 the 15th). The last frame
of an rtimer file is left out: its interval holds the console commands that
stopped the recording.
"""

import argparse
import bisect
import collections
import glob
import itertools
import json
import math
import os
import re
import statistics
import sys

PARTS_VK = ["engine", "cgame", "load", "scene", "fence", "flush", "pipelines", "swap", "thread", "backend"]
PARTS_RT = ["engine", "cgame", "load", "scene", "backend"]
EVENTS = [("pipelines", "pipelines"), ("textures", "textures"), ("staging_bytes", "uploads"),
          ("allocations", "allocations"), ("flushes", "flushes"), ("vertex_growth", "vertex buffer grown"),
          ("depth_targets", "depth image replaced"), ("swapchains", "swapchain created"),
          ("skin_uploads", "skin data")]


class Log:
    def __init__(self, path):
        self.path = path
        self.kind = None
        self.header = []
        cols = None
        rows = []
        with open(path) as f:
            for line in f:
                if line.startswith("#"):
                    self.header.append(line[1:].strip())
                    if "vkframelog" in line and self.kind is None:
                        self.kind = "vk"
                    elif "rtimer frames" in line and self.kind is None:
                        self.kind = "rt"
                    elif "inlat frames" in line and self.kind is None:
                        self.kind = "in"
                    m = re.match(r"# columns[^:]*: (.*)", line)
                    if m:
                        cols = m.group(1).split()
                elif line[:1].isdigit() and cols:
                    rows.append([int(x) for x in line.split()])
        if not cols:
            raise SystemExit(f"{path}: no columns line")
        if self.kind == "rt" and rows:
            # its interval holds the console commands that stopped the
            # recording (and, in perf.py runs, wrote the Vulkan frame log)
            rows.pop()
        self.cols = {c: [r[i] for r in rows] for i, c in enumerate(cols)}
        self.n = len(rows)
        self.clock = None  # CLOCK_MONOTONIC ms of frame 0 (rtimer files)
        for h in self.header:
            m = re.match(r"clock ([\d.]+):", h)
            if m:
                self.clock = float(m.group(1))
        if self.kind == "in":
            return
        self.interval = self.cols["interval"]
        if self.kind == "vk":
            self.parts = self.vk_parts()
        else:
            self.parts = self.rt_parts()

    def col(self, name):
        return self.cols.get(name, [0] * self.n)

    def medians(self):
        if not hasattr(self, "_medians"):
            self._medians = {k: statistics.median(v) for k, v in self.parts.items()}
        return self._medians

    def rt_parts(self):
        c = self.cols
        return {"engine": c["engine"], "cgame": c["cgame"], "load": c["load"], "scene": c["scene"],
                "backend": [b + t + e for b, t, e in zip(c["begin"], c["2d"], c["end"])]}

    def vk_parts(self):
        c = self.cols
        swap = [s + a + aa + p for s, a, aa, p in zip(c["submit"], c["acquire"], c["acquire_ahead"], c["present"])]
        # r_vkSubmitThread: the main thread waiting for the submit thread
        thread = self.col("thread_wait")
        backend = []
        for i in range(self.n):
            inside = c["begin"][i] + c["2d"][i] + c["end"][i]
            backend.append(max(inside - c["fence"][i] - c["flush"][i] - c["pipeline_us"][i] - swap[i] - thread[i], 0))
        return {"engine": c["engine"], "cgame": c["cgame"], "load": c["load"], "scene": c["scene"],
                "fence": c["fence"], "flush": c["flush"], "pipelines": c["pipeline_us"], "swap": swap,
                "thread": thread, "backend": backend}

    def submit_thread(self):
        """r_vkSubmitThread: the submit thread's times per frame (us; not the
        main thread's, so not parts of the frame time), or None"""
        if not any(self.col("thread_start")):
            return None
        return {k: self.col("thread_" + k) for k in ("start", "submit", "acquire", "present")}

    def events(self, i):
        out = []
        for key, name in EVENTS:
            v = self.col(key)[i]
            if v:
                if key == "staging_bytes":
                    out.append(f"{name} {v / 1024:.0f} KB")
                elif key == "vertex_growth":
                    out.append(f"{name} to {v / 1024:.0f} KB")
                elif key == "pipelines":
                    out.append(f"{name} {v} ({self.col('pipeline_us')[i] / 1000:.2f} ms)")
                elif key == "textures":
                    out.append(f"{name} {v} ({self.col('texture_us')[i] / 1000:.2f} ms)")
                elif key == "skin_uploads":
                    out.append(f"{name} {v} ({self.col('skin_bytes')[i] / 1024:.0f} KB)")
                else:
                    out.append(f"{name} {v}")
        return out


def pct(sorted_v, q):
    return sorted_v[min(int(len(sorted_v) * q), len(sorted_v) - 1)]


def dist(values):
    v = sorted(values)
    n = len(v)
    mean = sum(v) / n
    return {"n": n, "mean": mean / 1000, "p1": pct(v, 0.01) / 1000, "median": pct(v, 0.5) / 1000, "p90": pct(v, 0.9) / 1000,
            "p99": pct(v, 0.99) / 1000, "p999": pct(v, 0.999) / 1000, "max": v[-1] / 1000,
            "sd": math.sqrt(sum((x - mean) ** 2 for x in v) / n) / 1000}


def dist_line(d):
    return (f"median {d['median']:.3f}  p90 {d['p90']:.3f}  p99 {d['p99']:.3f} ({1000 / d['p99']:.0f} fps)"
            f"  p99.9 {d['p999']:.3f} ({1000 / d['p999']:.0f} fps)  max {d['max']:.3f}"
            f"  mean {d['mean']:.3f} ({1000 / d['mean']:.0f} fps)  sd {d['sd']:.3f}")


def present_intervals(log):
    p = log.col("present_at")
    return [b - a for a, b in zip(p, p[1:]) if a and b]


def gpu_intervals(log):
    e = log.col("gpu_end")
    return [b - a for a, b in zip(e, e[1:]) if a and b and b > a]


def gpu_gaps(log):
    """GPU idle time before each frame's main command buffer"""
    s, e = log.col("gpu_start"), log.col("gpu_end")
    return [max(s[i] - e[i - 1], 0) for i in range(1, log.n) if s[i] and e[i - 1] and e[i]]


def slow_frames(log, above=None):
    """indices of the frames above the run's p99 (or above `above` ms)"""
    limit = above * 1000 if above else pct(sorted(log.interval), 0.99)
    return [i for i in range(log.n) if log.interval[i] > limit]


def causes(log, frames):
    """the excess over the median of every part, summed over the frames, and
    the frames counted by their largest excess and by event"""
    med = log.medians()
    excess = {k: 0 for k in log.parts}
    largest = {k: 0 for k in log.parts}
    events = {name: 0 for _, name in EVENTS}
    for i in frames:
        ex = {k: max(log.parts[k][i] - med[k], 0) for k in log.parts}
        for k in ex:
            excess[k] += ex[k]
        largest[max(ex, key=ex.get)] += 1
        for key, name in EVENTS:
            if log.col(key)[i]:
                events[name] += 1
    return med, excess, largest, events


def causes_text(log, frames, indent="  "):
    if not frames:
        return indent + "no slow frames\n"
    med, excess, largest, events = causes(log, frames)
    total = sum(excess.values()) or 1
    medint = statistics.median(log.interval)
    over = sum(log.interval[i] - medint for i in frames)
    out = (f"{indent}{len(frames)} frames above {min(log.interval[i] for i in frames) / 1000:.3f} ms,"
           f" {over / len(frames) / 1000:.3f} ms over the median frame on average; excess over each part's median:\n")
    for k in sorted(excess, key=excess.get, reverse=True):
        if excess[k] * 100 / total >= 0.5:
            out += (f"{indent}  {k:10s} {excess[k] * 100 / total:5.1f} %  ({excess[k] / len(frames) / 1000:.3f} ms per frame,"
                    f" median {med[k] / 1000:.3f}; largest in {largest[k]} frames)\n")
    ev = [f"{name} {n}" for name, n in events.items() if n]
    if ev:
        out += f"{indent}  frames with: " + ", ".join(ev) + "\n"
    return out


def frame_text(log, i):
    p = log.parts
    s = f"frame {i}: {log.interval[i] / 1000:.3f} ms = " + ", ".join(
        f"{k} {p[k][i] / 1000:.3f}" for k in p if p[k][i] >= 20)
    if log.kind == "vk" and log.col("gpu_end")[i]:
        s += f"; gpu {log.col('gpu')[i] / 1000:.3f}"
    st = log.submit_thread() if log.kind == "vk" else None
    if st:
        s += "; submit thread " + ", ".join(f"{k} {v[i] / 1000:.3f}" for k, v in st.items())
    ev = log.events(i)
    if ev:
        s += "; " + ", ".join(ev)
    return s


def inlat_values(rows):
    """input to present, read to read and present to present (us) of
    libinlat.so's (read, present) pairs (read 0: none)"""
    return {"input to present": [p - r for r, p in rows if r],
            "read to read": [b[0] - a[0] for a, b in zip(rows, rows[1:]) if a[0] and b[0]],
            "present to present": [b[1] - a[1] for a, b in zip(rows, rows[1:])]}


def submit_thread_text(log, indent):
    """r_vkSubmitThread: the distributions of the submit thread's times"""
    st = log.submit_thread()
    if not st:
        return
    names = {"start": "hand-off to start", "submit": "vkQueueSubmit", "acquire": "acquire", "present": "present"}
    for k, v in st.items():
        print(f"{indent}submit thread {names[k] + ' ms:':22s} " + dist_line(dist(v)))


def summary(log, worst=0, above=None):
    if log.kind == "in":
        print(f"{log.path}: {log.n} frames (libinlat.so)")
        for name, v in inlat_values(list(zip(log.cols["read"], log.cols["present"]))).items():
            if len(v) > 1:
                print(f"  {name + ' ms:':22s} " + dist_line(dist(v)))
        return
    print(f"{log.path}: {log.n} frames ({'Vulkan frame log' if log.kind == 'vk' else 'renderer_timed.so'})")
    for h in log.header[1:2]:
        print("  " + h)
    print("  frame time ms:         " + dist_line(dist(log.interval)))
    if log.kind == "vk":
        pi = present_intervals(log)
        if len(pi) > 1:
            print("  present to present ms: " + dist_line(dist(pi)))
        gi = gpu_intervals(log)
        if len(gi) > 1:
            print("  GPU end to end ms:     " + dist_line(dist(gi)))
            print("  GPU main pass ms:      " + dist_line(dist([g for g, e in zip(log.col("gpu"), log.col("gpu_end")) if e])))
        submit_thread_text(log, "  ")
    print("  means ms: " + ", ".join(f"{k} {sum(v) / log.n / 1000:.3f}" for k, v in log.parts.items()))
    print("  slow frames:")
    print(causes_text(log, slow_frames(log, above), "    "), end="")
    if worst:
        print(f"  the {worst} slowest:")
        for i in sorted(range(log.n), key=lambda i: -log.interval[i])[:worst]:
            print("    " + frame_text(log, i))


# --batch

NAME = re.compile(r"(.+)_(\d+x\d+)_(.+)_(\d+)\.(rt|vk|in|pt)\.txt$")


def inlat_window(inl, rt):
    """the (read, present) pairs of libinlat.so's frames (all since startup)
    whose present falls in the frames renderer_timed.so measured"""
    t0 = rt.clock * 1000 + rt.cols["start"][0]
    t1 = rt.clock * 1000 + rt.cols["start"][-1] + rt.interval[-1]
    return [(r, p) for r, p in zip(inl.cols["read"], inl.cols["present"]) if t0 <= p < t1]


def batch(directory, above=None, svg=None, worst=0, labels=None):
    runs = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.txt"))):
        m = NAME.match(os.path.basename(path))
        if not m:
            continue
        view, size, renderer, run, kind = m.groups()
        if kind == "pt":
            continue  # --display
        runs.setdefault((view, size), {}).setdefault(renderer, {}).setdefault(int(run), {})[kind] = Log(path)
    for (view, size), rs in sorted(runs.items(), key=lambda x: (x[0][0], int(x[0][1].split("x")[0]))):
        print(f"== {view} {size}")
        dists = {}
        for renderer, byrun in rs.items():
            ds = [dist(r["rt"].interval) for r in byrun.values() if "rt" in r]
            if not ds:
                continue
            med = {k: statistics.median(d[k] for d in ds) for k in ds[0]}
            dists[renderer] = med
            print(f"  {renderer:9s} frame time ms, medians of {len(ds)}: " + dist_line(med))
            print(f"  {'':9s} runs p99 " + " ".join(f"{d['p99']:.3f}" for d in ds)
                  + "  p99.9 " + " ".join(f"{d['p999']:.3f}" for d in ds) + "  max " + " ".join(f"{d['max']:.3f}" for d in ds))
            ins = [inlat_values(inlat_window(r["in"], r["rt"])) for r in byrun.values()
                   if "in" in r and "rt" in r and r["rt"].clock]
            for name in ("input to present", "read to read", "present to present"):
                ds_ = [dist(v[name]) for v in ins if len(v[name]) > 1]
                if ds_:
                    print(f"  {'':9s} {name} ms, medians of {len(ds_)}: "
                          + dist_line({key: statistics.median(d[key] for d in ds_) for key in ds_[0]}))
        for renderer, byrun in rs.items():
            for run, r in sorted(byrun.items()):
                if "vk" in r:
                    log = r["vk"]
                    pi, gi, gg = present_intervals(log), gpu_intervals(log), gpu_gaps(log)
                    print(f"  {renderer} run {run} (frame log):")
                    if len(pi) > 1:
                        print("    present to present ms: " + dist_line(dist(pi)))
                    if len(gi) > 1:
                        print("    GPU end to end ms:     " + dist_line(dist(gi)))
                        g = [x for x, e in zip(log.col("gpu"), log.col("gpu_end")) if e]
                        print("    GPU main pass ms:      " + dist_line(dist(g)))
                        print(f"    GPU idle before a frame: median {statistics.median(gg) / 1000:.3f} ms,"
                              f" mean {sum(gg) / len(gg) / 1000:.3f}")
                    submit_thread_text(log, "    ")
                    print(causes_text(log, slow_frames(log, above), "    "), end="")
                    if worst:
                        for i in sorted(range(log.n), key=lambda i: -log.interval[i])[:worst]:
                            print("      " + frame_text(log, i))
            for run, r in sorted(byrun.items()):
                if "rt" in r and "vk" not in r:
                    print(f"  {renderer} run {run}:")
                    print(causes_text(r["rt"], slow_frames(r["rt"], above), "    "), end="")
        content_and_noise(rs, worst)
        compare_renderers(rs, above)
    if svg:
        write_svg(svg, runs, labels)


def best_shift(a, b, span=3):
    """the shift s for which frame i of b is frame i + s of a: the renderers
    can start a demo's window a frame apart; matched on the parts that the
    content drives (registration and cgame), not on the renderers' own"""
    ka = [x + y for x, y in zip(a.parts["load"], a.parts["cgame"])]
    kb = [x + y for x, y in zip(b.parts["load"], b.parts["cgame"])]
    n = min(len(ka), len(kb)) - 2 * span
    best = (None, 0)
    for s in range(-span, span + 1):
        score = sum(ka[i + s] * kb[i] for i in range(span, span + n))
        if best[0] is None or score > best[0]:
            best = (score, s)
    return best[1]


def content_and_noise(rs, worst=0):
    """demo views with several runs: frame k is the same moment in every run,
    so the median of a frame over the runs is what the content costs, and a
    run's excess over that median is noise of that run (the system, the
    driver, the clock). Percentiles of both, the parts that make the
    content's slow frames slow (the frame log's parts, medians over the
    runs), and those of the largest noise"""
    for renderer, byrun in rs.items():
        runs = [(run, r) for run, r in sorted(byrun.items()) if "rt" in r]
        if len(runs) < 3:
            continue
        ref = runs[0][1]["rt"]
        shifted = [(run, r, best_shift(ref, r["rt"])) for run, r in runs]
        # frame j of the first run is frame j - s of a run with shift s
        lo = max(max(s, 0) for _, _, s in shifted)
        hi = min(min(r["rt"].n + s, ref.n) for _, r, s in shifted)
        frames = range(lo, hi)
        med = [statistics.median(r["rt"].interval[i - s] for _, r, s in shifted) for i in frames]
        noise = [r["rt"].interval[i - s] - m for _, r, s in shifted for i, m in zip(frames, med)]
        dc = dist(med)
        pos = sorted(noise)
        q = lambda p: pct(pos, p) / 1000
        print(f"  {renderer:9s} content (median of each frame over {len(runs)} runs) ms: median {dc['median']:.3f}"
              f"  p99 {dc['p99']:.3f}  p99.9 {dc['p999']:.3f}  max {dc['max']:.3f}")
        big = sum(1 for x in noise if x > 1000)
        print(f"  {'':9s} noise (a run's frame minus that median) ms: p99 {q(0.99):.3f}  p99.9 {q(0.999):.3f}"
              f"  max {pos[-1] / 1000:.3f}; {big * 10000 / len(noise):.1f} frames in 10000 more than 1 ms slower")
        vks = [(run, r["vk"], s) for run, r, s in shifted if "vk" in r]
        if len(vks) < 3:
            continue
        # content causes: per frame and part, the median over the runs
        parts = list(vks[0][1].parts)
        pmed = {k: [statistics.median(v.parts[k][i - s] for _, v, s in vks if 0 <= i - s < v.n) for i in frames] for k in parts}
        base = {k: statistics.median(pmed[k]) for k in parts}
        limit = pct(sorted(med), 0.99)
        slow = [j for j, m in enumerate(med) if m > limit]
        ex = {k: sum(max(pmed[k][j] - base[k], 0) for j in slow) for k in parts}
        tot = sum(ex.values()) or 1
        print(f"  {'':9s} content's slow frames ({len(slow)} above {limit / 1000:.3f} ms) by part: " + ", ".join(
            f"{k} {100 * v / tot:.0f} %" for k, v in sorted(ex.items(), key=lambda x: -x[1]) if v / tot >= 0.02))
        evs = {}
        for j in slow:
            for _, v, s in vks[:1]:
                i = frames[j] - s
                for key, name in EVENTS:
                    if 0 <= i < v.n and v.col(key)[i]:
                        evs[name] = evs.get(name, 0) + 1
        if evs:
            print(f"  {'':9s} with: " + ", ".join(f"{k} {n}" for k, n in evs.items()))
        # noise causes: the part that jumped, against the other runs' median of that part
        nx = {k: 0 for k in parts}
        top = []
        for run, v, s in vks:
            for j, i in enumerate(frames):
                k_ = i - s
                if not 0 <= k_ < v.n:
                    continue
                d = v.interval[k_] - med[j]
                if d > 1000:
                    for k in parts:
                        nx[k] += max(v.parts[k][k_] - pmed[k][j], 0)
                    top.append((d, run, k_, j))
        tot = sum(nx.values())
        if tot:
            print(f"  {'':9s} noise over 1 ms ({len(top)} frames) by part: " + ", ".join(
                f"{k} {100 * v / tot:.0f} %" for k, v in sorted(nx.items(), key=lambda x: -x[1]) if v / tot >= 0.02))
        for d, run, k_, j in sorted(top, reverse=True)[:worst]:
            v = [x for r_, x, _ in vks if r_ == run][0]
            print(f"      run {run} +{d / 1000:.3f}: " + frame_text(v, k_))


def repeatability(rs, above, limit_ms=None):
    """demo views: a slow frame that is slow in most runs at the same moment
    of the demo comes from the content; one that is slow in one run only is
    noise of that run (the system, the driver). Counted per renderer, with
    the excess over the median frame time in each class, and for the frame
    log runs the parts that make the random ones slow"""
    for renderer, byrun in rs.items():
        logs = [(run, r["rt"]) for run, r in sorted(byrun.items()) if "rt" in r]
        if len(logs) < 2:
            continue
        ref = logs[0][1]
        aligned = []  # per run: set of slow frames in the frame numbers of the first run
        for run, log in logs:
            s = best_shift(ref, log)
            aligned.append((run, log, s, {i + s for i in slow_frames(log, above)}))
        counts = {}
        for _, _, _, slow in aligned:
            for i in slow:
                counts[i] = counts.get(i, 0) + 1
        need = (len(logs) + 1) // 2 + (len(logs) % 2 == 0)  # most of the runs: 2 of 3, 2 of 2
        rep_ex = rnd_ex = 0
        rep_n = rnd_n = 0
        rnd_parts = {}
        for run, log, s, slow in aligned:
            med = statistics.median(log.interval)
            for i in slow:
                ex = log.interval[i - s] - med
                if counts[i] >= need:
                    rep_ex += ex
                    rep_n += 1
                else:
                    rnd_ex += ex
                    rnd_n += 1
                    vk = byrun[run].get("vk")
                    if vk and 0 <= i - s < vk.n:
                        vmed = vk.medians()
                        for k, v in vk.parts.items():
                            rnd_parts[k] = rnd_parts.get(k, 0) + max(v[i - s] - vmed[k], 0)
        total = rep_ex + rnd_ex or 1
        line = (f"  {renderer:9s} slow frames over {len(logs)} runs: {rep_n} repeatable (slow in {need}+ runs at the same"
                f" moment; {100 * rep_ex / total:.0f} % of the excess time), {rnd_n} in one run only"
                f" ({100 * rnd_ex / total:.0f} %, {rnd_ex / max(rnd_n, 1) / 1000:.3f} ms over the median each)")
        print(line)
        if rnd_parts:
            t = sum(rnd_parts.values()) or 1
            print(f"  {'':9s} the one-run frames of the frame log runs by part: " + ", ".join(
                f"{k} {100 * v / t:.0f} %" for k, v in sorted(rnd_parts.items(), key=lambda x: -x[1]) if v / t >= 0.02))


def compare_renderers(rs, above):
    """demo views: the same frame index is the same moment in every run (up
    to a shift of a frame or so between renderers, see best_shift), so the
    frames of each renderer can be compared one to one with those of the
    first one by name (gl: that reference)"""
    gl = sorted(rs)[:1]
    if not gl or len(rs) < 2:
        return
    for renderer, byrun in rs.items():
        if renderer in gl:
            continue
        for run, r in sorted(byrun.items()):
            g = rs[gl[0]].get(run, {}).get("rt")
            v = r.get("rt")
            if not g or not v or abs(g.n - v.n) > 2:
                continue
            s = best_shift(g, v)
            idx = [i for i in range(v.n) if 0 <= i + s < g.n]
            slow = slow_frames(v, above)
            glimit = pct(sorted(g.interval), 0.99) if not above else above * 1000
            both = sum(1 for i in slow if 0 <= i + s < g.n and g.interval[i + s] > glimit)
            worse = sorted(idx, key=lambda i: g.interval[i + s] - v.interval[i])[:6]
            print(f"  {renderer} run {run} against {gl[0]} run {run} (its frame i is {gl[0]}'s i{s:+d}): of its {len(slow)}"
                  f" slow frames, {both} are slow in {gl[0]} too; largest differences (ms, this - {gl[0]}):")
            for i in worse:
                vkl = r.get("vk")
                ev = (", " + ", ".join(vkl.events(i))) if vkl and i < vkl.n and vkl.events(i) else ""
                print(f"    frame {i}: {v.interval[i] / 1000:.3f} against {g.interval[i + s] / 1000:.3f}"
                      f" ({(v.interval[i] - g.interval[i + s]) / 1000:+.3f}){ev}")


# SVG: percentile curves (frame time against the share of slower frames,
# log scale) and the frame times of the first run, per view and size

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]  # light
PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
SHARES = [0.5, 0.1, 0.01, 0.001, 0.0001]


def write_svg(path, runs, labels=None):
    """labels: {renderer: name}, the renderers drawn and the order of their
    colors (the first two also get the frame time series)"""
    labels = labels or {}
    panels = sorted(runs.items(), key=lambda x: (x[0][0], int(x[0][1].split("x")[0])))
    present = {r for _, rs in panels for r in rs}
    # with labels only those, in their order
    renderers = [r for r in labels if r in present] if labels else sorted(present)
    name = lambda r: labels.get(r, r)
    slot = {r: i % len(PALETTE) for i, r in enumerate(renderers)}
    W, PH = 1000, 230
    H = len(panels) * (PH + 56) + 40
    css = "".join(f".s{i}{{stroke:var(--series-{i + 1})}}.f{i}{{fill:var(--series-{i + 1})}}" for i in range(len(PALETTE)))
    light = "".join(f"--series-{i + 1}:{c};" for i, c in enumerate(PALETTE))
    dark = "".join(f"--series-{i + 1}:{c};" for i, c in enumerate(PALETTE_DARK))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui,sans-serif" font-size="11">',
           "<style>svg{" + light + "--surface:#fcfcfb;--text:#0b0b0b;--muted:#52514e;--grid:#e6e5e1;--axis:#b5b3ad}"
           "@media (prefers-color-scheme:dark){svg{" + dark + "--surface:#1a1a19;--text:#ffffff;--muted:#c3c2b7;--grid:#30302d;--axis:#5c5b57}}"
           ".bg{fill:var(--surface)}text{fill:var(--muted)}.t{fill:var(--text);font-weight:600;font-size:12px}"
           ".g{stroke:var(--grid);stroke-width:1}.a{stroke:var(--axis);stroke-width:1}"
           "polyline{fill:none;stroke-linejoin:round}circle.h{fill:transparent;stroke:none}circle.h:hover{fill:var(--text)}"
           + css + "</style>",
           f'<rect class="bg" width="{W}" height="{H}"/>']
    # legend
    x = 60
    for r in renderers:
        out.append(f'<rect class="f{slot[r]}" x="{x}" y="14" width="14" height="3" rx="1.5"/>'
                   f'<text x="{x + 20}" y="19" style="fill:var(--text)">{name(r)}</text>')
        x += 30 + 7 * len(name(r))
    out.append(f'<text x="{W - 20}" y="19" text-anchor="end">hover a dot for its value; one line per run</text>')
    y0 = 40
    for (view, size), rs in panels:
        lx, lw = 60, 400
        rx = lx + lw + 80
        rw = W - rx - 20
        top, bot = y0 + 24, y0 + PH
        allv = sorted(x for byrun in rs.values() for r in byrun.values() if "rt" in r for x in r["rt"].interval)
        if not allv:
            continue
        ymax = max(pct(allv, 0.9995) / 1000 * 1.15, 1.2)
        nice = [0.25, 0.5, 1, 2, 5, 10]
        step = next((s for s in nice if ymax / s <= 6), 10)
        out.append(f'<text class="t" x="{lx}" y="{y0 + 12}">{view} {size}: frame time (ms) at each percentile</text>')
        out.append(f'<text class="t" x="{rx}" y="{y0 + 12}">run 1, {" and ".join(name(r) for r in renderers[:2])}: slowest of every 20 frames</text>')
        X = lambda t: lx + lw * (math.log10(0.5) - math.log10(t)) / (math.log10(0.5) - math.log10(0.0001))
        Y = lambda ms: bot - (bot - top) * min(ms, ymax) / ymax
        for t in SHARES:
            out.append(f'<line class="g" x1="{X(t):.1f}" y1="{top}" x2="{X(t):.1f}" y2="{bot}"/>')
            label = {0.5: "median", 0.1: "p90", 0.01: "p99", 0.001: "p99.9", 0.0001: "p99.99"}[t]
            out.append(f'<text x="{X(t):.1f}" y="{bot + 14}" text-anchor="middle">{label}</text>')
        v = 0
        while v <= ymax:
            out.append(f'<line class="g" x1="{lx}" y1="{Y(v):.1f}" x2="{lx + lw}" y2="{Y(v):.1f}"/>')
            out.append(f'<line class="g" x1="{rx}" y1="{Y(v):.1f}" x2="{rx + rw}" y2="{Y(v):.1f}"/>')
            out.append(f'<text x="{lx - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
            out.append(f'<text x="{rx - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:g}</text>')
            v += step
        for r in renderers:
            byrun = rs.get(r)
            if not byrun:
                continue
            k = slot[r]
            curves = []
            for run, rr in sorted(byrun.items()):
                if "rt" not in rr:
                    continue
                desc = sorted(rr["rt"].interval, reverse=True)
                n = len(desc)
                pts = []
                for j in range(0, 240):
                    t = 0.5 * (0.0002 ** (j / 239))
                    pts.append(f"{X(t):.1f},{Y(desc[min(int(n * t), n - 1)] / 1000):.1f}")
                out.append(f'<polyline class="s{k}" stroke-width="1.5" opacity="0.85" points="{" ".join(pts)}"/>')
                curves.append(desc)
            if curves:
                # hover dots: the median over the runs at each percentile
                for t in SHARES:
                    vals = [c[min(int(len(c) * t), len(c) - 1)] / 1000 for c in curves]
                    m = statistics.median(vals)
                    out.append(f'<circle class="f{k}" cx="{X(t):.1f}" cy="{Y(m):.1f}" r="2.5"/>'
                               f'<circle class="h" cx="{X(t):.1f}" cy="{Y(m):.1f}" r="7"><title>{name(r)}, {view} {size}: '
                               f'{m:.3f} ms ({1000 / m:.0f} fps), {t * 100:g} % of the frames slower; runs '
                               + ", ".join(f"{x:.3f}" for x in vals) + "</title></circle>")
            r1 = byrun.get(min(byrun))
            if r1 and "rt" in r1 and r in renderers[:2]:
                iv = r1["rt"].interval
                pts = [f"{rx + rw * j / len(iv):.1f},{Y(max(iv[j:j + 20]) / 1000):.1f}" for j in range(0, len(iv), 20)]
                out.append(f'<polyline class="s{k}" stroke-width="1" opacity="0.8" points="{" ".join(pts)}"/>')
        out.append(f'<line class="a" x1="{lx}" y1="{bot}" x2="{lx + lw}" y2="{bot}"/>')
        out.append(f'<line class="a" x1="{rx}" y1="{bot}" x2="{rx + rw}" y2="{bot}"/>')
        n1 = max((len(r["rt"].interval) for byrun in rs.values() for r in byrun.values() if "rt" in r), default=0)
        out.append(f'<text x="{rx}" y="{bot + 14}">frame 0</text><text x="{rx + rw}" y="{bot + 14}" text-anchor="end">{n1}</text>')
        y0 += PH + 56
    out.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def stack_attribution(log, path, above=None, worst=0):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import stacks as st
    import bisect
    if log.clock is None:
        sys.exit(f"{log.path}: no clock line (an older renderer_timed.so)")
    maps, samples, _ = st.load(path)
    t0 = log.clock * 1e6
    starts = [t0 + s * 1000 for s in log.col("start")]
    ends = [a + iv * 1000 for a, iv in zip(starts, log.interval)]
    owned = []  # (frame, sample index)
    for k, (ns, pcs) in enumerate(samples):
        i = bisect.bisect_right(starts, ns) - 1
        if 0 <= i < log.n and ns < ends[i]:
            owned.append((i, k))
    if not owned:
        sys.exit("no sample in the frames")
    stacks = []
    for _, k in owned:
        pcs = samples[k][1]
        for j, pc in enumerate(pcs):
            if pc and j >= 1 and any(a <= pc < b and "libstacks" not in p for a, b, o, p in maps):
                pcs = pcs[j:]
                break
        stacks.append([pcs[0]] + [pc - 1 for pc in pcs[1:]])
    names = st.symbolize(maps, {pc for s in stacks for pc in s})
    short = lambda fn: fn.split(" [")[0]
    stacks = [[short(names[pc]) for pc in s] for s in stacks]
    stacks = [s[1:] if s and "__restore_rt" in s[0] else s for s in stacks]
    slow = set(slow_frames(log, above))
    inc_all, inc_slow = {}, {}
    n_all, n_slow = len(stacks), 0
    for (i, _), s in zip(owned, stacks):
        for fn in set(s):
            inc_all[fn] = inc_all.get(fn, 0) + 1
            if i in slow:
                inc_slow[fn] = inc_slow.get(fn, 0) + 1
        n_slow += i in slow
    print(f"{n_all} samples in {log.n} frames, {n_slow} in the {len(slow)} slow frames"
          f" (above {min(log.interval[i] for i in slow) / 1000:.3f} ms)" if slow else "no slow frames")
    if n_slow:
        print(f"{'slow %':>7s} {'all %':>7s}  function (inclusive, by the share of the slow frames' samples)")
        for fn in sorted(inc_slow, key=lambda f: -inc_slow[f])[:int(os.environ.get("FRAMELOG_TOP", "45"))]:
            print(f"{100 * inc_slow[fn] / n_slow:7.1f} {100 * inc_all[fn] / n_all:7.1f}  {fn}")
    if worst:
        by = {}
        for (i, _), s in zip(owned, stacks):
            by.setdefault(i, []).append(s)
        for i in sorted(range(log.n), key=lambda i: -log.interval[i])[:worst]:
            print(frame_text(log, i))
            for s in by.get(i, []):
                print("    " + " < ".join(s[:8]))



# --display: present timing (libinlat.so with INLAT_DISPLAY=1)

# KWin's composited frames on the dev machine; any other framebuffer on the
# primary plane is a client's buffer scanned out directly
COMPOSITOR_FORMATS = ("AR30", "XR30")
# the longest a composite is assumed to take (from its start to the frame
# callbacks it sends), for the upper bound of asynchronous X11 presents
R_MAX = 1500


class Timing:
    """libinlat.so's present timing file (.pt.txt, see rtimer/inlat.c)"""

    def __init__(self, path):
        self.path = path
        self.header = []
        self.frames, self.swapchains, self.feedback, self.gpu, self.fbs = {}, {}, {}, {}, {}
        self.completes, self.vblanks, self.polls = [], [], []
        with open(path) as f:
            for line in f:
                if line.startswith("#"):
                    self.header.append(line[1:].strip())
                    continue
                w = line.split()
                if not w:
                    continue
                t, v = w[0], w[1:]
                if t == "F":  # frame: image swapchain serial id gpu
                    self.frames[int(v[0])] = tuple(int(x) for x in v[1:6])
                elif t == "S":  # swapchain: mode eid fullscreen width height
                    self.swapchains[int(v[0])] = (v[1], int(v[2]), int(v[3]) if len(v) > 3 else 0)
                elif t == "C":  # recv eid serial kind mode ust msc
                    self.completes.append(tuple(int(x) for x in v[:7]))
                elif t == "P":  # frame: recv presented refresh flags
                    self.feedback[int(v[0])] = tuple(int(x) for x in v[1:5])
                elif t == "V":  # seq time wake fb format overlay format
                    self.vblanks.append((int(v[0]), int(v[1]), int(v[2]), int(v[3]), v[4]))
                elif t == "B":  # fb: width height format modifier
                    self.fbs[int(v[0])] = (int(v[1]), int(v[2]), v[3], int(v[4]))
                elif t == "G":  # submission: GPU done
                    self.gpu[int(v[0])] = int(v[1])
                elif t == "U":  # time fb format
                    self.polls.append((int(v[0]), int(v[1]), v[2]))
        self.complete = {(c[1], c[2]): c for c in reversed(self.completes)}
        self.wakes = [v[2] for v in self.vblanks]
        self.times = [v[1] for v in self.vblanks]
        self.poll_times = [p[0] for p in self.polls]

    def gpu_done(self, frame):
        """when the GPU finished the frame's last submission (None: unknown)"""
        fr = self.frames.get(frame)
        return self.gpu.get(fr[4]) if fr and len(fr) > 4 and fr[4] else None

    def vblank_after(self, t, k=1):
        """the timestamp of the k-th vblank that starts after t (the flip
        latches at the start of vblank; the timestamp is the start of scanout)"""
        i = bisect.bisect_right(self.wakes, t) + k - 1
        return self.times[i] if i < len(self.times) else None

    def change_before(self, t):
        """the last framebuffer change on the primary plane at or before t"""
        i = bisect.bisect_right(self.poll_times, t) - 1
        return self.poll_times[i] if i >= 0 else None


def game_fb(v):
    """a vblank (or poll) record that shows a client's buffer on the primary plane"""
    return v[3] and v[4] not in COMPOSITOR_FORMATS + ("-",)


def window_frames(inl, rt):
    """the frames (lines of libinlat.so's file) presented while renderer_timed.so measured"""
    t0 = rt.clock * 1000 + rt.cols["start"][0]
    t1 = rt.clock * 1000 + rt.cols["start"][-1] + rt.interval[-1]
    return [i for i, (r, p) in enumerate(zip(inl.cols["read"], inl.cols["present"])) if r and t0 <= p < t1]


def scanout_agreement(pt, presents, frames, vblanks, disp):
    """direct scanout: a game framebuffer on the primary plane at a vblank
    is one of the swapchain images, but which isn't reported. For each
    mapping of the framebuffers to the images, the frame a vblank shows is
    the newest presented with its image; the share of the vblanks (of a new
    framebuffer) where that frame is the one `disp` puts there, for the best
    mapping: high only if `disp` is right (24 mappings for 4 images)"""
    fbs = sorted({v[3] for v in vblanks if game_fb(v)})
    images = sorted({pt.frames[f][0] for f in frames if f in pt.frames})
    if not fbs or len(fbs) > len(images) or len(images) > 7:
        return None, 0
    by_image = {i: [f for f in frames if f in pt.frames and pt.frames[f][0] == i] for i in images}
    by_image_t = {i: [presents[f] for f in fs] for i, fs in by_image.items()}
    # the frame disp shows at each vblank: the newest shown at or before it
    shown = sorted((t, f) for f, t in disp.items())
    times = [t for t, f in shown]
    checks = []
    last = None
    for v in vblanks:
        if v[3] != last and v[3] in fbs:
            j = bisect.bisect_right(times, v[1]) - 1
            if j >= 0:
                checks.append((v, shown[j][1]))
        last = v[3]
    best = 0
    for perm in itertools.permutations(images, len(fbs)):
        m = dict(zip(fbs, perm))
        ok = 0
        for v, f in checks:
            ts = by_image_t[m[v[3]]]
            j = bisect.bisect_left(ts, v[2]) - 1
            ok += j >= 0 and by_image[m[v[3]]][j] == f
        best = max(best, ok)
    return best / max(len(checks), 1), len(checks)


def newest_ready(order, ready, limit):
    """the newest frame of `order` (sorted by present) ready before `limit`"""
    for f in reversed(order):
        if ready[f] < limit:
            return f
    return None


def display_times(inl, pt, frames):
    """when each frame of `frames` reached the display (us: the start of the
    scanout that shows it, or the tearing flip; frames never shown are left
    out), an upper bound where that is a lower one, and how they were found.
    KWin (6.6) composites on its main thread, latching each surface's newest
    commit whose buffer is ready (its fence signaled), and sends frame
    callbacks at the end of a composite; its commit thread commits a frame to
    the kernel once its buffers are ready, so with tearing the framebuffer
    change on the primary plane (INLAT_FBPOLL_US) is the flip. A frame is
    ready when its last submission is done on the GPU (libinlat.so's
    timeline semaphore), and on X11 also completed by Xwayland.
    - Wayland, vsync: the presentation feedback's time (KWin: the kernel's
      page flip timestamp), exact. Tearing (no vsync flag): KWin reports the
      last vblank's timestamp instead; each framebuffer change shows the
      newest frame ready before it.
    - X11, FIFO: Xwayland completes a present at the frame callback of the
      composite that latched it (its UST), flipped at the next vblank.
    - X11, asynchronous (immediate, mailbox): Xwayland completes a present
      when the compositor has its commit, with the UST of the last frame
      callback, so the presents completed with an earlier UST than a frame
      callback F were committed before the composite that ends with F; the
      composite started at F - r and latched the newest of them ready by
      then, r unknown: r = 0 gives a lower bound, r = R_MAX an upper one.
      With tearing (many frame callbacks per refresh, or KWin's rule: a
      fullscreen window presenting with AsyncMayTear, Mesa's immediate) as
      for Wayland.
    - direct scanout (a game buffer on the primary plane at the vblanks):
      the frames each vblank can show are known, which checks the above
      (scanout_agreement)."""
    reads, presents = inl.cols["read"], inl.cols["present"]
    t0, t1 = presents[frames[0]], presents[frames[-1]]
    vin = [v for v in pt.vblanks if t0 <= v[2] <= t1]
    info = {"refreshes": len(vin), "frames": len(frames)}
    info["direct scanout"] = sum(1 for v in vin if game_fb(v)) / max(len(vin), 1)
    pin = [p for p in pt.polls if t0 <= p[0] <= t1]
    if pin:
        info["fb changes per refresh"] = len(pin) / max(len(vin), 1)
    disp, upper, comp = {}, {}, {}
    gpu = {f: pt.gpu_done(f) for f in frames}
    info["gpu done known"] = sum(1 for f in frames if gpu[f]) / len(frames)

    def tearing_rule(order, ready):
        """each framebuffer change shows the newest frame ready before it
        (KWin composites with tearing as soon as a frame is ready, and its
        commit thread flips at once: about 0.45 ms from the frame's GPU work
        done to the change; with Wayland's feedback, the frames found are
        those KWin reports presented)"""
        ptimes = [presents[x] for x in order]
        last = None
        for p in pin:
            j = bisect.bisect_left(ptimes, p[0])
            f = newest_ready(order[max(0, j - 64):j], ready, p[0])
            if f is not None and f != last:
                disp.setdefault(f, p[0])
                last = f

    if pt.feedback:
        fb = {f: pt.feedback[f] for f in frames if f in pt.feedback}
        shown = [f for f in frames if f in fb and fb[f][1] >= 0]
        info["discarded"] = sum(1 for x in fb.values() if x[1] < 0) / len(frames)
        info["tearing"] = sum(1 for f in shown if not fb[f][3] & 1) / max(len(shown), 1)
        if info["tearing"] < 0.5:
            info["method"] = "wayland feedback"
            disp = {f: fb[f][1] for f in shown}
        elif pin:
            # KWin reports the last vblank's timestamp for a tearing flip
            info["method"] = "wayland tearing: fb changes, frames ready before them"
            order = [f for f in frames if gpu[f]]
            tearing_rule(order, {f: max(presents[f], gpu[f]) for f in order})
            info["feedback agreement"] = sum(1 for f in disp if f in shown) / max(len(disp), 1)
        else:
            info["method"] = "wayland tearing: no fb polls"
    else:
        for f in frames:
            fr = pt.frames.get(f)
            sc = pt.swapchains.get(fr[1]) if fr else None
            c = pt.complete.get((sc[1], fr[2])) if sc else None
            if c:
                comp[f] = c
        names = {0: "copy", 1: "flip", 2: "skip", 3: "suboptimal copy"}
        info["completion modes"] = dict(collections.Counter(names.get(c[4], c[4]) for c in comp.values()))
        sc = pt.swapchains[pt.frames[frames[-1]][1]] if frames[-1] in pt.frames else ("?", 0, 0)
        mode = sc[0]
        info["present mode"] = mode
        info["fullscreen"] = sc[2]
        order = sorted(comp, key=lambda f: presents[f])
        if mode == "fifo":
            info["method"] = "x11 fifo: vblank after the completion"
            for f in order:
                disp[f] = pt.vblank_after(comp[f][5])
        else:
            ready = {f: max(gpu[f] or comp[f][0], comp[f][0]) for f in comp}
            usts = sorted({comp[f][5] for f in comp})
            info["frame callbacks per refresh"] = len(usts) / max(len(vin), 1)
            # tearing: seen as many frame callbacks per refresh when frames
            # come faster than the refresh; near or below it, KWin's rule
            # (allowTearing on): a fullscreen window presenting with
            # PresentOptionAsyncMayTear, Mesa's immediate mode
            if info["frame callbacks per refresh"] > 1.5:
                info["tearing"] = "seen"
            elif mode == "immediate" and sc[2]:
                info["tearing"] = "fullscreen immediate"
            if "tearing" in info and pin:
                info["method"] = "x11 async tearing: fb changes, frames ready before them"
                tearing_rule(order, ready)
            else:
                info["method"] = "x11 async: frame callbacks, frames committed and ready before them"
                for r, out in ((0, disp), (R_MAX, upper)):
                    last, j = None, 0
                    for u in usts:
                        # the newest frame Xwayland committed before u, ready before u - r
                        while j < len(order) and comp[order[j]][5] < u:
                            j += 1
                        f = newest_ready(order[max(0, j - 64):j], ready, u - r)
                        if f is not None and f != last:
                            out.setdefault(f, pt.vblank_after(u))
                            last = f
    disp = {f: t for f, t in disp.items() if t}
    upper = {f: t for f, t in upper.items() if t}
    if info["direct scanout"] > 0.5:
        info["scanout agreement"], info["scanout checks"] = scanout_agreement(pt, presents, frames, vin, disp)
    return disp, upper, comp, info


def display_values(inl, pt, frames):
    reads, presents = inl.cols["read"], inl.cols["present"]
    disp, upper, comp, info = display_times(inl, pt, frames)
    shown = sorted((t, f) for f, t in disp.items())
    times = [t for t, f in shown]
    # the age of what each refresh of the window shows: from the input read
    # of the newest frame shown at or before it
    age = []
    for v in pt.vblanks:
        if presents[frames[0]] + 50000 <= v[1] <= presents[frames[-1]]:
            j = bisect.bisect_right(times, v[1]) - 1
            if j >= 0:
                age.append(v[1] - reads[shown[j][1]])
    vals = {
        "input to present": [presents[f] - reads[f] for f in frames],
        "input to display": [disp[f] - reads[f] for f in frames if f in disp],
        "present to display": [disp[f] - presents[f] for f in frames if f in disp],
        "age at each refresh": age,
    }
    if upper:
        vals["input to display, upper"] = [upper[f] - reads[f] for f in frames if f in upper]
    if comp:
        vals["input to completion"] = [comp[f][0] - reads[f] for f in frames if f in comp]
    gpu = [(f, pt.gpu_done(f)) for f in frames]
    vals["input to GPU done"] = [g - reads[f] for f, g in gpu if g]
    # from the frame's image ready (GPU done; on X11 also completed by
    # Xwayland) to its display: above its run's floor (the compositor's
    # latch to the scanout), how long the finished frame waited for the
    # latch, the most a later start of the frame could take off
    # (a FIFO completion comes after the composite: not a readiness)
    async_x11 = info.get("present mode") not in (None, "fifo")
    ready = {f: max(g, comp[f][0] if f in comp and async_x11 else g) for f, g in gpu if g}
    vals["ready to display"] = [disp[f] - ready[f] for f in frames if f in disp and f in ready]
    info["shown"] = len(vals["input to display"]) / len(frames)
    return vals, info


def display_batch(directory, out_json=None):
    runs = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.pt.txt"))):
        m = NAME.match(os.path.basename(path))
        if not m:
            continue
        view, size, variant, run, _ = m.groups()
        base = path[:-len(".pt.txt")]
        if not (os.path.exists(base + ".in.txt") and os.path.exists(base + ".rt.txt")):
            continue
        runs.setdefault((view, size), {}).setdefault(variant, {})[int(run)] = base
    dump = {}
    order = ("input to present", "input to GPU done", "input to completion", "input to display",
             "input to display, upper", "present to display", "ready to display", "age at each refresh")
    for (view, size), variants in sorted(runs.items()):
        print(f"== {view} {size}")
        for variant, byrun in variants.items():
            per_run, infos = [], []
            for run, base in sorted(byrun.items()):
                inl, rt, pt = Log(base + ".in.txt"), Log(base + ".rt.txt"), Timing(base + ".pt.txt")
                frames = window_frames(inl, rt) if rt.clock else []
                if len(frames) < 10:
                    continue
                vals, info = display_values(inl, pt, frames)
                per_run.append(vals)
                infos.append(info)
            if not per_run:
                continue
            print(f"  {variant}: {len(per_run)} runs, {infos[0]['method']}")
            for name in order:
                ds = [dist(v[name]) for v in per_run if len(v.get(name, [])) > 1]
                if ds:
                    med = {k: statistics.median(d[k] for d in ds) for k in ds[0]}
                    runs_ = " ".join("%.3f" % d["median"] for d in ds)
                    print(f"    {name + ' ms:':28s} median {med['median']:6.3f}  p90 {med['p90']:6.3f}  p99 {med['p99']:6.3f}"
                          f"  p99.9 {med['p999']:6.3f}  max {med['max']:6.3f}  mean {med['mean']:6.3f}  (run medians {runs_})")
            keys = sorted({k for i in infos for k in i} - {"method"})
            for k in keys:
                vs = [i[k] for i in infos if k in i]
                if all(isinstance(x, (int, float)) for x in vs):
                    print(f"    {k}: " + " ".join(f"{x:.3f}" if isinstance(x, float) else str(x) for x in vs))
                else:
                    print(f"    {k}: " + " | ".join(str(x) for x in vs))
            dump.setdefault(f"{view} {size}", {})[variant] = {
                "values": {name: [x for v in per_run for x in v.get(name, [])] for name in order},
                "runs": [{name: dist(v[name]) for name in order if len(v.get(name, [])) > 1} for v in per_run],
                "info": infos}
    if out_json:
        with open(out_json, "w") as f:
            json.dump(dump, f)
    return dump


def pooled(values, q):
    v = sorted(values)
    return pct(v, q) / 1000 if v else None


def display_table(dump, labels=None):
    """markdown tables of --display results: per view and cap, each variant's
    medians over its runs for the input to present time, and the display
    times of all runs pooled (a run of 10 s at 60 Hz shows about 600 frames)"""
    labels = labels or {}
    views = collections.OrderedDict()
    for key in dump:
        name, size = key.rsplit(" ", 1)
        m = re.match(r"(.*)_c(\d+)$", name)
        view, cap = (m.group(1), int(m.group(2))) if m else (name, None)
        views.setdefault((view, size), []).append((cap, key))
    for (view, size), caps in views.items():
        print(f"\n{view} {size}\n")
        print("| `com_maxfps` | variant | input to present: median, p99 | input to display: median, p90, p99, p99.9 (pooled) | upper bound median | frames shown | direct scanout | tearing | ready to latch slack: median |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for cap, key in sorted(caps, key=lambda x: (x[0] is None, x[0] if x[0] is not None else 0)):
            for variant, d in dump[key].items():
                runs, vals, infos = d["runs"], d["values"], d["info"]
                ip = [r["input to present"] for r in runs if "input to present" in r]
                disp = vals.get("input to display") or []
                up = vals.get("input to display, upper") or []
                rd = vals.get("ready to display") or []
                cells = [str(cap if cap is not None else ""), labels.get(variant, variant)]
                cells.append("%.3f, %.3f" % (statistics.median(x["median"] for x in ip), statistics.median(x["p99"] for x in ip)) if ip else "")
                cells.append(", ".join("%.2f" % pooled(disp, q) for q in (0.5, 0.9, 0.99, 0.999)) if len(disp) > 10 else "")
                cells.append("%.2f" % pooled(up, 0.5) if len(up) > 10 and abs(pooled(up, 0.5) - pooled(disp, 0.5)) > 0.005 else "")
                cells.append("%.0f %%" % (100 * statistics.median(i.get("shown", 0) for i in infos)))
                cells.append("%.0f %%" % (100 * statistics.median(i.get("direct scanout", 0) for i in infos)))
                t = [i.get("tearing") for i in infos]
                cells.append("yes" if any(x not in (None, 0, 0.0) for x in t) else "no")
                per_run = [d_["ready to display"] for d_ in runs if "ready to display" in d_]
                cells.append("%.2f" % statistics.median(x["median"] - x["p1"] for x in per_run) if per_run and "p1" in per_run[0] else "")
                print("| " + " | ".join(cells) + " |")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--batch")
    ap.add_argument("--worst", type=int, default=0)
    ap.add_argument("--above", type=float, help="slow frames: above this many ms instead of the run's p99")
    ap.add_argument("--svg")
    ap.add_argument("--stacks")
    ap.add_argument("--labels", default="", help="for --svg: renderer=name,... in the order of the colors")
    ap.add_argument("--display", help="a perf.py --framelog directory of runs with libinlat.so and INLAT_DISPLAY=1")
    ap.add_argument("--json", help="with --display: every run's values and distributions")
    ap.add_argument("--table", action="store_true", help="with --display: markdown tables (--labels: variant names)")
    args = ap.parse_args()
    if args.display:
        dump = display_batch(args.display, args.json)
        if args.table:
            display_table(dump, dict(x.split("=", 1) for x in args.labels.split(",") if "=" in x))
        return
    labels = dict(x.split("=", 1) for x in args.labels.split(",") if "=" in x)
    if args.stacks:
        for p in args.files:
            stack_attribution(Log(p), args.stacks, args.above, args.worst)
        return
    if args.batch:
        batch(args.batch, args.above, args.svg, args.worst, labels)
    for p in args.files:
        summary(Log(p), args.worst, args.above)
    if not args.batch and not args.files:
        ap.error("no file")


if __name__ == "__main__":
    main()
