#!/usr/bin/env python3
"""A stand-in for openmohaa in the app's tests, without game files.

It takes the same command line, reads commands from its com_pipefile and
writes demostate.json and demoindex.json like the game (cl_demoFiles 1).
Demos are JSON files with a .dm3 extension holding the index the game would
make (duration, kills, rounds…). Every command read is appended to
main/commands.log in its home folder, and demovideo writes
main/videos/<name>.mp4 holding what would have been recorded, as JSON.

Run as mohdemoindex (a link with that name), it is the indexing tool:
mohdemoindex <output folder> <demo>...

Environment, for the tests:
    FAKEGAME_SPEED          how much faster than real time demos play (1)
    FAKEGAME_FAIL_DRIVER    quit at once with an error on this SDL_VIDEODRIVER
    FAKEGAME_HANG           1: ignore quit and SIGTERM
    FAKEGAME_FFMPEG_FAIL    1: recordings fail as if FFmpeg were missing,
                            mid: FFmpeg quits half a second into them
"""

import json
import os
import signal
import sys
import time


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def parse_time(text):
    """msec from "90" or "1:30", as demoseek takes."""
    total = 0.0
    for part in text.split(":"):
        total = total * 60 + float(part)
    return int(total * 1000)


def index_tool(folder, demos):
    failed = 0
    for demo in demos:
        name = os.path.splitext(os.path.basename(demo))[0]
        try:
            with open(demo, encoding="utf-8") as f:
                index = json.load(f)
        except (OSError, ValueError):
            print("Couldn't open " + demo, file=sys.stderr)
            failed += 1
            continue
        index["demo"] = name
        write_json(os.path.join(folder, name + ".json"), index)
        print(name, flush=True)
    return 1 if failed else 0


class FakeGame:
    def __init__(self, cvars):
        self.cvars = cvars
        self.main = os.path.join(cvars["fs_homepath"], "main")
        self.speed = float(os.environ.get("FAKEGAME_SPEED", "1"))
        self.state = {"demo": "", "time": 0, "duration": 0, "paused": False, "seeking": False,
                      "only": "", "player": "", "recording": ""}
        self.written = None
        self.last_write = 0
        self.index = {}
        self.timescale = 1.0
        self.carry = 0.0  # msec not yet added to the time
        self.video = None
        self.pipe = None
        self.buffer = b""

    def open_pipe(self):
        path = os.path.join(self.main, self.cvars["com_pipefile"])
        if os.path.exists(path):
            os.unlink(path)
        os.mkfifo(path)
        self.pipe = os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def read_commands(self):
        try:
            data = os.read(self.pipe, 4096)
        except BlockingIOError:
            return []
        self.buffer += data
        lines = self.buffer.split(b"\n")
        self.buffer = lines.pop()
        return [line.decode("latin-1") for line in lines]

    def log(self, command):
        with open(os.path.join(self.main, "commands.log"), "a", encoding="latin-1") as f:
            f.write(command + "\n")

    def run(self):
        self.open_pipe()
        print("fakegame: started", flush=True)
        last = time.monotonic()
        while True:
            for command in self.read_commands():
                self.log(command)
                self.execute(command)
            now = time.monotonic()
            self.advance((now - last) * 1000 * self.speed * self.timescale)
            last = now
            self.write_state()
            time.sleep(0.01)

    def execute(self, command):
        words = command.split(" ", 1)
        name, rest = words[0], words[1].strip() if len(words) > 1 else ""
        arg = rest.strip('"')
        state = self.state
        if name == "quit":
            if not os.environ.get("FAKEGAME_HANG"):
                sys.exit(0)
        elif name == "demo":
            self.play(arg)
        elif name == "demoseek" and state["demo"]:
            state["time"] = max(0, min(state["duration"], parse_time(arg)))
        elif name == "demoskip" and state["demo"]:
            state["time"] = max(0, min(state["duration"], state["time"] + parse_time(arg.lstrip("-")) * (-1 if arg.startswith("-") else 1)))
        elif name == "demopause" and state["demo"]:
            state["paused"] = not state["paused"] if not arg else arg != "0"
        elif name == "demoonly":
            only, _, player = rest.partition(" ")
            state["only"], state["player"] = only, player.strip('"')
        elif name == "timescale":
            self.timescale = float(arg)
        elif name == "demovideo" and state["demo"] and not self.video:
            parts = rest.split()
            self.video = {"name": parts[0], "start": state["time"],
                          "end": parse_time(parts[1]) if len(parts) > 1 else None,
                          "only": state["only"], "player": state["player"], "cvars": self.cvars}
            state["recording"] = parts[0]
        elif name == "stopvideo":
            self.finish_video()

    def play(self, name):
        try:
            with open(os.path.join(self.main, "demos", name + ".dm3"), encoding="utf-8") as f:
                self.index = json.load(f)
        except (OSError, ValueError):
            print(f"ERROR: Couldn't load demos/{name}.dm3", flush=True)
            return
        self.index["demo"] = name
        write_json(os.path.join(self.main, "demoindex.json"), self.index)
        self.finish_video()
        self.state.update(demo=name, time=0, duration=self.index.get("duration", 0), paused=False,
                          only="", player="")

    def advance(self, msec):
        state = self.state
        if not state["demo"] or state["paused"]:
            return
        self.carry += msec
        whole = int(self.carry)
        self.carry -= whole
        state["time"] = min(state["duration"], state["time"] + whole)
        video = self.video
        if video:
            # demoonly's stretches are taken as 2 seconds in all
            end = video["end"] or (video["start"] + 2000 if video["only"] else None)
            if os.environ.get("FAKEGAME_FFMPEG_FAIL") == "mid" and state["time"] >= video["start"] + 500:
                self.finish_video(midway=True)
            elif end is not None and state["time"] >= end:
                self.finish_video()
        if state["time"] >= state["duration"]:
            self.finish_video()
            state.update(demo="", time=0, duration=0)

    def finish_video(self, midway=False):
        video, self.video = self.video, None
        if not video:
            return
        self.state["recording"] = ""
        folder = os.path.join(self.main, "videos")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, video["name"] + ".mp4")
        if midway:
            print(f"^1Couldn't write videos/{video['name']}.mp4, see its .log file, the recording stopped", flush=True)
            with open(path + ".log", "w") as f:
                f.write("fakegame: FFmpeg quit\n")
            with open(path, "w") as f:
                f.write("half a video")
            return
        if os.environ.get("FAKEGAME_FFMPEG_FAIL"):
            print("Couldn't run FFmpeg", flush=True)
            with open(path + ".log", "w") as f:
                f.write("fakegame: no FFmpeg\n")
            return
        video["stop"] = self.state["time"]
        write_json(path, video)

    def write_state(self):
        now = time.monotonic()
        if self.state == self.written:
            return
        time_only = self.written and {**self.written, "time": self.state["time"]} == self.state
        if time_only and now - self.last_write < 0.1:
            return
        write_json(os.path.join(self.main, "demostate.json"), self.state)
        self.written = dict(self.state)
        self.last_write = now


def main():
    if os.path.basename(sys.argv[0]).startswith("mohdemoindex"):
        if len(sys.argv) < 3:
            print("Usage: mohdemoindex <output folder> <demo>...", file=sys.stderr)
            return 2
        return index_tool(sys.argv[1], sys.argv[2:])

    cvars, args, i = {}, sys.argv[1:], 0
    while i < len(args):
        if args[i] == "+set" and i + 2 < len(args):
            cvars[args[i + 1]] = args[i + 2]
            i += 3
        else:
            i += 1
    if os.environ.get("SDL_VIDEODRIVER") and os.environ.get("SDL_VIDEODRIVER") == os.environ.get("FAKEGAME_FAIL_DRIVER"):
        print("fakegame: no video driver", flush=True)
        return 1
    if os.environ.get("FAKEGAME_HANG"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    FakeGame(cvars).run()


if __name__ == "__main__":
    sys.exit(main())
