#!/usr/bin/env python3
"""mohdemo-replay: rewatch Medal of Honor: Allied Assault demos with buttons.

Starts the game (OpenMoHAA built from this repository) with a throwaway home
folder, sends it console commands through its pipe (com_pipefile) and shows
what it reports in demoindex.json and demostate.json. Everything the buttons
do is a console command, so it all works with key binds too.

    python3 mohreplay.py [--exe PATH] [--game FOLDER] [--demos FOLDER] [DEMO]
"""

import argparse
import bisect
import errno
import functools
import hashlib
import html
import json
import os
import posixpath
import shutil
import sys
import tempfile
from time import monotonic

from PySide6.QtCore import (
    QDateTime, QElapsedTimer, QEvent, QFileSystemWatcher, QObject, QProcess, QProcessEnvironment,
    QSettings, QStandardPaths, Qt, QTimer, QUrl,
)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProxyStyle, QPushButton, QRadioButton, QSizePolicy, QSlider, QSplitter, QStyle,
    QStyleOptionSlider, QTabWidget, QToolBar, QToolButton, QToolTip, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

PIPE = "replay_pipe"
# the game's settings in its home folder, which is thrown away after each game
GAME_CONFIG = os.path.join("main", "configs", "omconfig.cfg")
KILL_BEFORE = 4000  # msec of a kill shown before it, as cl_demoKillBefore
KILL_AFTER = 2000  # and after it, as cl_demoKillAfter
# msec at most between a team kill and a kill of its killer that plays, for
# the team kill to play too, as the engine's DEMO_TEAMKILL_NEAR
TEAM_KILL_NEAR = 2000
MIN_LENGTH = 5 * 60000  # msec: shorter demos aren't listed, once indexed
# the Player menu's "Whoever is shown": not a name, which can't hold a NUL
SHOWN = "\0shown"
# what Record… offers for whoever is shown, as demoonly shownkills: one and
# several
SHOWN_KILLS = ("frag shown", "frags shown")
SHOWN_WHO = ": the recorder's and the players they follow"
# what Record… offers for the recorder's own frags
RECORDER_KILLS = ("recorder frag", "recorder frags")
# what demoonly plays of the recorder for those
RECORDER_ONLY = {"recorderkills": "kills"}
# the demo list's columns
DEMO_COLUMNS = ["Demo", "Player", "Length", "Map", "Recorder", "Rules", "Date"]
COL_DEMO, COL_PLAYER, COL_LENGTH, COL_MAP, COL_RECORDER, COL_RULES, COL_DATE = range(len(DEMO_COLUMNS))
# the rules of a demo's levels, from mohdemoindex: realism servers change
# the speeds and the damage of weapons
RULES = {"default": "Default", "realism": "Realism", "both": "Both"}
SPEEDS = ["0.25", "0.5", "1", "2", "4"]
# keys above 127 only: during a demo, the others only run +buttons
BINDS = {
    "PAUSE": "demopause",
    "LEFTARROW": "demoskip -5",
    "RIGHTARROW": "demoskip 5",
    "UPARROW": "demonextkill",
    "DOWNARROW": "demoprevkill",
    "PGUP": "demonextround",
    "PGDN": "demoprevround",
}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACOS = sys.platform == "darwin"
# FFmpeg's video options, {crf} from the quality slider
CODECS = {
    "H.264": "-c:v libx264 -preset medium -crf {crf} -pix_fmt yuv420p",
    "H.265": "-c:v libx265 -preset medium -crf {crf} -pix_fmt yuv420p -tag:v hvc1",
    "Hardware H.264": ("-c:v h264_videotoolbox -q:v {quality}" if MACOS else
                       "-vaapi_device /dev/dri/renderD128 -vf format=nv12,hwupload -c:v h264_vaapi -qp {crf}"),
}
# with motion blur, the frames a second the game renders, blended into the video's
BLUR_RATE = 240


def with_filters(options, filters):
    """FFmpeg output options with video filters run before their own, as a
    second -vf would replace the first."""
    if " -vf " not in f" {options}":
        return f"-vf {filters} {options}"
    return f" {options}".replace(" -vf ", f" -vf {filters},", 1)[1:]


def clock(msec):
    seconds = max(0, msec) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def seconds(msec):
    return f"{max(0, msec) / 1000:.3f}"


def quoted(name):
    return '"' + name.replace('"', "'") + '"'


def parse_clock(text):
    """msec from "90", "1:30" or "1:30.5", None if it isn't a time."""
    try:
        parts = [float(p) for p in text.strip().split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + part
    return int(total * 1000)


def game_program(path):
    """The program in a macOS app bundle (openmohaa.app), or path."""
    if path.rstrip("/").endswith(".app") and os.path.isdir(path):
        name = os.path.basename(path.rstrip("/"))[:-4]
        return os.path.join(path, "Contents", "MacOS", name)
    return path


def find_indexer(exe):
    """mohdemoindex, built next to the game or to its app bundle."""
    folders = [os.path.dirname(exe)]
    if os.path.basename(os.path.dirname(os.path.dirname(exe))) == "Contents":
        folders.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(exe)))))
    return next((p for p in (os.path.join(f, "mohdemoindex") for f in folders) if os.path.isfile(p)),
                os.path.join(folders[0], "mohdemoindex"))


def find_ffmpeg():
    for path in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if path and os.path.isfile(path):
            return path
    return "ffmpeg"


@functools.lru_cache(maxsize=4096)
def clean_name(name):
    """The engine's rule for names: without ^ and a letter or digit, only
    printable ASCII, any case (Q_CleanStr), spaces as one."""
    out, i = [], 0
    while i < len(name):
        if name[i] == "^" and i + 1 < len(name) and name[i + 1].isascii() and name[i + 1].isalnum():
            i += 2
            continue
        if " " <= name[i] <= "~":
            out.append(name[i])
        i += 1
    return " ".join(word for word in "".join(out).lower().split(" ") if word)


def same_player(a, b):
    return clean_name(a) == clean_name(b)


def is_grenade(kill):
    """Whether kill was made with a grenade or shrapnel."""
    return bool(kill.get("grenade")) or "grenade" in kill.get("text", "").lower() or "shrapnel" in kill.get("text", "").lower()


def watching(index):
    """The player shown at a time, in its level, as the engine looks them up
    (CL_DemoWatchFor): a function of msec giving (watched entry, from, to),
    None when no one is."""
    watched = index.get("watched", [])
    times = [w["time"] for w in watched]
    map_times = [m["time"] for m in index.get("maps", [])]

    def at(time):
        i = bisect.bisect_right(times, time) - 1
        mi = bisect.bisect_right(map_times, time) - 1
        map_start = map_times[mi] if mi >= 0 else 0
        if i < 0 or watched[i]["time"] < map_start:
            return None
        end = watched[i + 1]["time"] if i + 1 < len(watched) else index.get("duration", 0)
        if mi + 1 < len(map_times):
            end = min(end, map_times[mi + 1])
        return watched[i], max(watched[i]["time"], map_start), end
    return at


def is_shown(watch, client, name):
    """Whether a player, by client number or name, is the one watched: one
    who changed names since is still the same client."""
    return (watch["client"] >= 0 and watch["client"] == client) or bool(name) and same_player(watch["name"], name)


def shown_kills(index):
    """The kills made while their killer is the player shown, as demoonly
    plays a player's kills (at other times the demo shows someone else):
    [(kill, from, to)], with when they're shown around it."""
    at = watching(index)
    shown = []
    for kill in index.get("kills", []):
        watch = at(kill["time"]) if kill["killerName"] else None
        if watch and is_shown(watch[0], kill["killer"], kill["killerName"]):
            shown.append((kill, watch[1], watch[2]))
    return shown


def deaths_shown(index):
    """The ids of the kills whose victim is the player shown at the time:
    the recorder's deaths, or the followed player's, which aren't kills
    (CL_DemoDeathShown)."""
    at = watching(index)
    deaths = set()
    for kill in index.get("kills", []):
        watch = at(kill["time"])
        if watch and is_shown(watch[0], kill.get("victim", -1), kill.get("victimName", "")):
            deaths.add(id(kill))
    return deaths


def team_kills_with(kills, frags):
    """The team kills among kills, [(kill, from, to)], that play with frags,
    as demoonly plays them: those at most TEAM_KILL_NEAR from a kill by
    their killer in frags, like a teammate hit while shooting at an enemy."""
    times = {}
    for frag in frags:
        times.setdefault(clean_name(frag["killerName"]), []).append(frag["time"])
    for when in times.values():
        when.sort()
    kept = []
    for kill in kills:
        if not kill[0].get("teamKill") or not kill[0]["killerName"]:
            continue
        when = times.get(clean_name(kill[0]["killerName"]), [])
        i = bisect.bisect_left(when, kill[0]["time"] - TEAM_KILL_NEAR)
        if i < len(when) and when[i] <= kill[0]["time"] + TEAM_KILL_NEAR:
            kept.append(kill)
    return kept


def player_kills(player):
    """"name (kills)" from a summary's [name, kills]."""
    name, kills = player
    return f"{name} ({kills})"


def walk_demos(folder):
    """{name: path} of the demos in folder and the folders in it, a name
    being the path in folder without .dm3, with / between folders as the
    game takes it, and the folders looked in, but hidden ones."""
    demos, folders = {}, []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        folders.append(root)
        sub = os.path.relpath(root, folder).replace(os.sep, "/")
        for n in files:
            if n.lower().endswith(".dm3"):
                demos[n[:-4] if sub == "." else sub + "/" + n[:-4]] = os.path.join(root, n)
    return demos, folders


def list_demos(folder):
    return walk_demos(folder)[0]


def summarize(index):
    """What the demo list shows of an index: its maps, length, recorder and
    players, {clean name: [name, kills]}, counting the kills made while
    they're shown."""
    players = {}

    def add(name, kills=0):
        if name:
            players.setdefault(clean_name(name), [name, 0])[1] += kills
    add(index.get("recorder", {}).get("name", ""))
    for watched in index.get("watched", []):
        add(watched["name"])
    for kill in index.get("kills", []):
        add(kill["killerName"])
        add(kill["victimName"])
    deaths = deaths_shown(index)
    shown = [kill for kill, _, _ in shown_kills(index)
             if not kill.get("teamKill") and not is_grenade(kill) and id(kill) not in deaths]
    for kill in shown:
        add(kill["killerName"], 1)
    # "realism" or "default" when its levels tell one, "both", or ""
    kinds = {m.get("rules") for m in index.get("maps", [])} & {"realism", "default"}
    rules = "both" if len(kinds) == 2 else kinds.pop() if kinds else ""
    return {"maps": [m["map"] for m in index.get("maps", [])], "duration": index.get("duration", 0),
            "recorder": index.get("recorder", {}).get("name", ""), "players": players, "rules": rules}


class Library(QObject):
    """What's in every demo of the folder, without playing them: mohdemoindex
    (built next to the game) indexes them in the background, and the indexes
    are kept in the cache folder until the demo or mohdemoindex changes."""

    BATCH = 20  # demos per mohdemoindex run
    WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))

    def __init__(self, on_change, parent=None):
        super().__init__(parent)
        self.on_change = on_change
        self.demos = {}  # name: summarize()
        self.paths = {}  # name: path of the demos to index
        self.folder = self.cache = self.tool = None
        self.waiting = []
        self.busy = set()
        self.failed = set()
        self.workers = []
        self.total = 0
        self.watcher = QFileSystemWatcher(self)
        self.rescan_timer = QTimer(self, singleShot=True, interval=1000, timeout=self.rescan)
        self.watcher.directoryChanged.connect(lambda _: self.rescan_timer.start())
        self.changed_timer = QTimer(self, singleShot=True, interval=300, timeout=lambda: self.on_change())

    def open(self, folder, exe):
        """The demos of folder, indexed with the mohdemoindex next to exe."""
        self.stop()
        self.demos, self.failed = {}, set()
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        self.folder = os.path.abspath(folder) if folder else None
        self.tool = find_indexer(exe) if exe else None
        if not self.folder or not os.path.isdir(self.folder):
            self.on_change()
            return
        # a folder of indexes for each folder of demos
        key = hashlib.sha1(self.folder.encode()).hexdigest()[:12]
        base = QStandardPaths.writableLocation(QStandardPaths.GenericCacheLocation)
        self.cache = os.path.join(base, "mohdemo-replay", "index", key)
        self.rescan()

    def rescan(self):
        """Loads the indexes kept, and indexes the demos without one."""
        if not self.folder or not os.path.isdir(self.folder):
            return
        demos, folders = walk_demos(self.folder)
        # the folders in it too, for demos added there
        watched = set(self.watcher.directories())
        if set(folders) - watched:
            self.watcher.addPaths(sorted(set(folders) - watched))
        if watched - set(folders):
            self.watcher.removePaths(sorted(watched - set(folders)))
        try:
            made = os.path.getmtime(self.tool) if self.tool and os.path.isfile(self.tool) else None
        except OSError:
            made = None
        stale = []
        for name, path in demos.items():
            try:
                mtime = os.path.getmtime(path)
                kept = os.path.getmtime(os.path.join(self.cache, name + ".json"))
            except OSError:
                mtime, kept = 0, None
            if kept is not None and kept >= mtime and (made is None or kept >= made):
                if name not in self.demos:
                    self.load(name)
            elif made is not None and name not in self.busy and name not in self.failed:
                stale.append((mtime, name))
            elif kept is not None and name not in self.demos:
                self.load(name)  # out of date, but better than nothing
        for name in list(self.demos):
            if name not in demos:
                del self.demos[name]
                try:
                    os.remove(os.path.join(self.cache, name + ".json"))
                except OSError:
                    pass
        # newest first
        self.paths = demos
        self.waiting = [name for _, name in sorted(stale, reverse=True)]
        self.total = len(self.waiting) + len(self.busy)
        self.start_workers()
        self.on_change()

    def index(self, name):
        """The index kept of a demo, None if there's none."""
        try:
            with open(os.path.join(self.cache, name + ".json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, TypeError, ValueError):
            return None

    def load(self, name):
        try:
            with open(os.path.join(self.cache, name + ".json"), encoding="utf-8") as f:
                self.demos[name] = summarize(json.load(f))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.demos.pop(name, None)

    def start_workers(self):
        while self.waiting and len(self.workers) < self.WORKERS:
            # the demos of one folder, as mohdemoindex names an index after
            # the demo's file, in the folder of indexes for it
            folder = posixpath.dirname(self.waiting[0])
            batch = [n for n in self.waiting if posixpath.dirname(n) == folder][:self.BATCH]
            names = set(batch)
            self.waiting = [n for n in self.waiting if n not in names]
            self.busy |= names
            output = os.path.join(self.cache, folder)
            os.makedirs(output, exist_ok=True)
            worker = QProcess(self)
            worker.readyReadStandardOutput.connect(lambda w=worker, f=folder: self.indexed(w, f))
            worker.finished.connect(lambda *_, w=worker, n=names, f=folder: self.worker_done(w, n, f))
            worker.errorOccurred.connect(lambda error, w=worker, n=names, f=folder:
                                         error == QProcess.FailedToStart and self.worker_done(w, n, f))
            self.workers.append(worker)
            worker.start(self.tool, [output, *(self.paths[n] for n in batch)])
            try:
                # behind the game
                os.setpriority(os.PRIO_PROCESS, worker.processId(), 10)
            except (OSError, AttributeError):
                pass

    def indexed(self, worker, folder):
        for line in bytes(worker.readAllStandardOutput()).decode("utf-8", "replace").splitlines():
            if line:
                self.load(posixpath.join(folder, line))
        self.changed_timer.start()

    def worker_done(self, worker, names, folder):
        if worker not in self.workers:
            return
        self.indexed(worker, folder)
        self.workers.remove(worker)
        self.busy -= names
        # not indexed: not tried again until the folder is opened again
        self.failed |= {name for name in names if name not in self.demos}
        worker.deleteLater()
        self.start_workers()
        self.changed_timer.start()

    def progress(self):
        """(indexed, to index) while indexing, None when done."""
        left = len(self.waiting) + len(self.busy)
        return (self.total - left, self.total) if left else None

    def stop(self):
        workers, self.workers = self.workers, []
        self.waiting, self.busy = [], set()
        for worker in workers:
            worker.kill()
            worker.waitForFinished(1000)


class SortItem(QTreeWidgetItem):
    """Sorts by the number kept in a column's UserRole, if any; folders
    first, by name, whichever way the rest is sorted."""

    def __lt__(self, other):
        tree = self.treeWidget()
        column = tree.sortColumn() if tree else 0
        if isinstance(self, FolderItem) or isinstance(other, FolderItem):
            if isinstance(self, FolderItem) and isinstance(other, FolderItem):
                less = self.folder.lower() < other.folder.lower()
            else:
                less = isinstance(self, FolderItem)
            # sorting the other way asks other < self
            return less if not tree or tree.header().sortIndicatorOrder() == Qt.AscendingOrder else not less
        mine, theirs = self.data(column, Qt.UserRole), other.data(column, Qt.UserRole)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            return mine < theirs
        return self.text(column).lower() < other.text(column).lower()


class FolderItem(SortItem):
    """A folder of demos in the list, with those in it."""

    def __init__(self, folder):
        super().__init__([""] * len(DEMO_COLUMNS))
        self.folder = folder


def kept_config():
    """Where the game's settings are kept from one game to the next."""
    return os.path.join(QStandardPaths.writableLocation(QStandardPaths.GenericConfigLocation),
                        "mohdemo-replay", "omconfig.cfg")


def app_setting(line):
    """Whether a line of the game's config is the app's own, set again at each
    start: its keys' binds and its pipe. They aren't kept, so other games run
    from the game folder don't get them."""
    words = line.split(None, 2)
    return len(words) > 1 and (words[0].lower() == "bind" and words[1].upper() in BINDS
                               or words[0].lower() == "seta" and words[1].lower() == "com_pipefile")


class Game:
    """The game process, its throwaway home folder and its command pipe. It
    starts with the settings kept from earlier games, and keeps those changed
    in it unless keep_config is False."""

    QUIT_WAIT = 3000  # msec given to quit, then to SIGTERM
    TERM_WAIT = 2000

    def __init__(self, on_output, on_exit, keep_config=True):
        self.on_output = on_output
        self.on_exit = on_exit
        self.keep_config = keep_config
        self.process = None
        self.home = None
        self.queue = []
        self.driver = None
        self.args = None
        self.started = QElapsedTimer()
        self.flusher = QTimer(interval=100, timeout=self.flush)

    def running(self):
        return self.process is not None and self.process.state() != QProcess.NotRunning

    def start(self, exe, game, demos, width, height, extra=(), driver=None, viewmodel=None, forcemodel=None,
              kill_before=None, kill_after=None, videos=None):
        """videos: the folder its videos go to, instead of its home."""
        self.cleanup()
        self.game = game
        self.home = tempfile.mkdtemp(prefix="mohreplay-")
        main = os.path.join(self.home, "main")
        os.makedirs(main)
        os.symlink(os.path.abspath(demos), os.path.join(main, "demos"))
        if videos:
            os.symlink(os.path.abspath(videos), os.path.join(main, "videos"))
        config = os.path.join(self.home, GAME_CONFIG)
        os.makedirs(os.path.dirname(config))
        game_cfg = os.path.join(game, GAME_CONFIG)
        src = game_cfg if os.path.isfile(game_cfg) else (
            os.path.join(game, "main", "omconfig.cfg") if os.path.isfile(os.path.join(game, "main", "omconfig.cfg")) else kept_config()
        )
        try:
            shutil.copyfile(src, config)
        except OSError:
            pass  # none kept yet
        vm = "1" if (viewmodel is None or str(viewmodel) in ("1", "true", "True")) else "0"
        fm = "1" if (forcemodel is None or str(forcemodel) in ("1", "true", "True")) else "0"
        kb = str(kill_before if kill_before is not None else "4.0")
        ka = str(kill_after if kill_after is not None else "2.0")
        with open(os.path.join(main, "replay.cfg"), "w") as f:
            f.writelines(f'bind {key} "{command}"\n' for key, command in BINDS.items())

        # the settings forced: set after the ones kept are read, they win,
        # and extra comes after, as the recorder's own
        self.args = [exe, "+set", "fs_basepath", game, "+set", "fs_homepath", self.home,
                     "+set", "com_pipefile", PIPE, "+set", "cl_demoFiles", "1",
                     "+set", "cg_drawviewmodel", vm, "+set", "cg_fov", "80",
                     "+set", "r_uselod", "0",
                     "+set", "cg_forceModel", fm,
                     "+set", "dm_playermodel", "allied_airborne",
                     "+set", "dm_playergermanmodel", "german_winter_1",
                     "+set", "cl_demoKillBefore", kb,
                     "+set", "cl_demoKillAfter", ka,
                     "+set", "r_swapInterval", "1", "+set", "com_maxfps", "0",
                     "+set", "r_fullscreen", "0", "+set", "r_mode", "-1",
                     "+set", "r_customwidth", str(width), "+set", "r_customheight", str(height),
                     "+exec", "replay.cfg", *extra]
        # Wayland first, then XWayland if it doesn't start
        self.driver = driver or os.environ.get("SDL_VIDEODRIVER") or ("wayland" if sys.platform.startswith("linux") else "")
        self.launch()

    def launch(self):
        self.process = QProcess()
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        if self.driver:
            env.insert("SDL_VIDEODRIVER", self.driver)
        self.process.setProcessEnvironment(env)
        self.process.setWorkingDirectory(os.path.dirname(self.args[0]))
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.failed)
        self.process.start(self.args[0], self.args[1:])
        self.started.start()
        self.flusher.start()

    def read_output(self):
        text = bytes(self.process.readAllStandardOutput()).decode("latin-1")
        for line in text.splitlines():
            self.on_output(line)

    def failed(self, error):
        if error == QProcess.FailedToStart:
            self.on_output("ERROR: couldn't start " + self.args[0])
            self.flusher.stop()
            self.on_exit()

    def finished(self):
        quick = self.started.elapsed() < 5000
        if quick and self.driver == "wayland" and not os.environ.get("SDL_VIDEODRIVER") and self.process.exitCode():
            self.on_output("The game didn't start on Wayland, trying XWayland")
            self.driver = "x11"
            self.launch()
            return
        self.flusher.stop()
        self.queue.clear()
        self.save_config()
        self.on_exit()

    def send(self, *commands):
        """Commands sent together are written at once, so the game runs them
        in the same frame."""
        if self.running():
            self.queue.extend(commands)
            self.flush()

    def flush(self):
        """Writes the queued commands without ever waiting for the game."""
        if not self.queue or not self.home:
            return
        try:
            fd = os.open(os.path.join(self.home, "main", PIPE), os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return  # the game isn't reading yet
        try:
            data = "".join(c + "\n" for c in self.queue).encode("latin-1", "replace")
            written = os.write(fd, data)
            if written == len(data):
                self.queue.clear()
            else:
                # the pipe is full: keep what didn't fit, whole commands only
                done = data[:written].count(b"\n")
                del self.queue[:done]
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EPIPE):
                raise
        finally:
            os.close(fd)

    def read_json(self, name):
        if not self.home:
            return None
        try:
            with open(os.path.join(self.home, "main", name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def stop(self):
        """Asks the game to quit, then makes it, as it may not answer."""
        if self.running():
            # quitting now, no retry and no message
            self.process.finished.disconnect(self.finished)
            self.send("quit")
            if not self.process.waitForFinished(self.QUIT_WAIT):
                self.process.terminate()
                if not self.process.waitForFinished(self.TERM_WAIT):
                    self.process.kill()
                    self.process.waitForFinished(2000)
        self.cleanup()

    def save_config(self):
        """Keeps the settings the game wrote (it does as soon as one changes)
        for the next games and in the game main folder."""
        if not self.keep_config or not self.home:
            return
        try:
            with open(os.path.join(self.home, GAME_CONFIG), encoding="latin-1", newline="") as f:
                text = "".join(line for line in f if not app_setting(line))
        except OSError:
            return
        paths = [kept_config()]
        if getattr(self, "game", None):
            paths += [os.path.join(self.game, GAME_CONFIG), os.path.join(self.game, "main", "omconfig.cfg")]
        for path in paths:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".new", "w", encoding="latin-1", newline="") as f:
                    f.write(text)
                os.replace(path + ".new", path)
            except OSError:
                pass

    def cleanup(self):
        if self.home:
            self.save_config()
            for name in ("demos", "videos"):
                link = os.path.join(self.home, "main", name)
                if os.path.islink(link):
                    os.unlink(link)
            shutil.rmtree(self.home, ignore_errors=True)
            self.home = None


def theme_icon(name, fallback=None):
    """The desktop's icon of that name, else fallback (a QStyle standard
    pixmap or a QIcon), else none."""
    if isinstance(fallback, QStyle.StandardPixmap):
        fallback = QApplication.style().standardIcon(fallback)
    return QIcon.fromTheme(name, fallback or QIcon())


def record_icon():
    """A red dot."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#da4453"))
    painter.drawEllipse(5, 5, 22, 22)
    painter.end()
    return QIcon(pixmap)


def plain_tip(text):
    """A tooltip showing text as it is: names like <KoS>Bob aren't HTML."""
    return "<p style='white-space:pre'>" + html.escape(text) + "</p>"


class ClickStyle(QProxyStyle):
    """Sliders jump where they're clicked, instead of a page at a time."""

    def styleHint(self, hint, option=None, widget=None, data=None):
        if hint == QStyle.SH_Slider_AbsoluteSetButtons:
            return Qt.LeftButton.value
        return super().styleHint(hint, option, widget, data)


class Timeline(QSlider):
    """The time slider, with the kills listed marked under it and the rounds
    above it. Hovering tells what a mark is, or the time there."""

    MARK = 4  # pixels: how near a mark the mouse has to be

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.click_style = ClickStyle()
        self.setStyle(self.click_style)
        self.setMinimumHeight(self.sizeHint().height() + 2 * self.MARK + 4)
        self.kills = []  # (msec, text)
        self.rounds = []

    def set_marks(self, kills, rounds):
        self.kills, self.rounds = kills, rounds
        self.update()

    def geometry_of_values(self):
        """Where the minimum is and how many pixels the values span."""
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderGroove, self)
        handle = style.subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderHandle, self)
        return groove.x() + handle.width() // 2, max(1, groove.width() - handle.width())

    def x_of(self, msec):
        left, span = self.geometry_of_values()
        return left + QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), msec, span)

    def value_at(self, x):
        left, span = self.geometry_of_values()
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x - left, span)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.maximum() <= self.minimum():
            return
        painter = QPainter(self)
        palette = self.palette()
        height = self.height()
        painter.setPen(QPen(palette.highlight().color(), 1))
        for msec, _ in self.kills:
            x = self.x_of(msec)
            painter.drawLine(x, height - self.MARK - 1, x, height - 1)
        painter.setPen(QPen(palette.windowText().color(), 2))
        for msec, _ in self.rounds:
            x = self.x_of(msec)
            painter.drawLine(x, 0, x, self.MARK)

    def mark_at(self, x, y):
        """The text of the nearest mark on that side, None if none is near."""
        marks = self.rounds if y < self.height() // 2 else self.kills
        near = [(abs(self.x_of(msec) - x), msec, text) for msec, text in marks]
        near = [mark for mark in near if mark[0] <= self.MARK]
        if not near:
            return None
        # several kills close together: all of them
        best = min(near)[0]
        return "\n".join(f"{clock(msec)}  {text}" for distance, msec, text in sorted(near, key=lambda m: m[1])
                         if distance <= best + 1)

    def event(self, event):
        if event.type() == QEvent.ToolTip:
            if self.maximum() > self.minimum():
                position = event.position().toPoint() if hasattr(event, "position") else event.pos()
                text = self.mark_at(position.x(), position.y()) or clock(self.value_at(position.x()))
                # names like <KoS>Bob aren't HTML
                QToolTip.showText(event.globalPos(), "<p style='white-space:pre'>" + html.escape(text) + "</p>", self)
            return True
        return super().event(event)


def is_on(value):
    return value in ("1", "true", True)


def parse_size(text):
    """(width, height) from "1920x1080", None if it isn't a size."""
    try:
        width, height = (int(v) for v in str(text).lower().replace("×", "x").split("x"))
    except ValueError:
        return None
    return (width, height) if width > 0 and height > 0 else None


def seconds_box(settings, key, default, tip, fallback=None):
    """A box for a time in seconds, as kept in key, else in fallback."""
    box = QDoubleSpinBox()
    box.setRange(0.5, 30.0)
    box.setSingleStep(0.5)
    box.setSuffix(" s")
    box.setToolTip(tip)
    try:
        box.setValue(float(settings.value(key, settings.value(fallback, default) if fallback else default)))
    except (ValueError, TypeError):
        box.setValue(default)
    return box


def lead_row(before, after, text):
    """Before and after boxes in a row: "4.00 s before, 2.00 s after"."""
    row = QHBoxLayout()
    row.addWidget(before)
    row.addWidget(QLabel("before,"))
    row.addWidget(after)
    row.addWidget(QLabel("after " + text))
    row.addStretch(1)
    return row


def browse_row(edit, browse):
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    button = QPushButton("Browse…")
    button.clicked.connect(browse)
    row.addWidget(button)
    return row


def video_settings(settings):
    """How videos are recorded, from the settings: a job's FFmpeg options,
    size, frames per second the game draws, sound, viewmodel, models forced
    and FFmpeg."""
    try:
        crf = int(settings.value("rec/crf", 20))
    except (ValueError, TypeError):
        crf = 20
    codec = settings.value("rec/codec", "H.264")
    sound = settings.value("rec/sound", "true") == "true"
    options = settings.value("rec/advanced", "").strip() or " ".join([
        CODECS.get(codec, CODECS["H.264"]).format(crf=crf, quality=max(1, min(100, 120 - 3 * crf))),
        f"-c:a aac -b:a {settings.value('rec/bitrate', '192k')}" if sound else "-an",
        "-movflags +faststart"])
    fps = int(settings.value("rec/fps", "60"))
    if settings.value("rec/blur", "false") == "true":
        # the game draws several frames for each of the video, which FFmpeg blends
        frames = max(2, BLUR_RATE // fps)
        options = with_filters(options, f"tmix=frames={frames},framestep={frames}")
        fps *= frames
    width, height = parse_size(settings.value("rec/size", "1920x1080")) or (1920, 1080)
    return {"options": options, "width": width, "height": height, "fps": str(fps), "sound": sound,
            "viewmodel": "1" if is_on(settings.value("rec/viewmodel", settings.value("viewmodel", "1"))) else "0",
            "forcemodel": "1" if is_on(settings.value("rec/forcemodel", settings.value("forcemodel", "1"))) else "0",
            "ffmpeg": settings.value("rec/ffmpeg", "").strip() or find_ffmpeg()}


def video_summary(settings):
    """The video settings in a line: "1920x1080, 60 fps, H.264 (CRF 20),
    sound 192k"."""
    parts = [settings.value("rec/size", "1920x1080"), settings.value("rec/fps", "60") + " fps"
             + (" with motion blur" if settings.value("rec/blur", "false") == "true" else "")]
    if settings.value("rec/advanced", "").strip():
        parts.append("FFmpeg options of your own")
    else:
        parts.append(f"{settings.value('rec/codec', 'H.264')} (CRF {settings.value('rec/crf', 20)})")
        parts.append(f"sound {settings.value('rec/bitrate', '192k')}" if settings.value("rec/sound", "true") == "true"
                     else "no sound")
    if not is_on(settings.value("rec/viewmodel", settings.value("viewmodel", "1"))):
        parts.append("no viewmodel")
    if is_on(settings.value("rec/forcemodel", settings.value("forcemodel", "1"))):
        parts.append("models forced")
    return ", ".join(parts)


class VideoSettings(QWidget):
    """How videos are recorded, kept as rec/*: Settings' Videos tab, and
    the Record dialog's Change…."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        form = QFormLayout(self)
        self.size = QComboBox()
        self.size.addItems(["1280x720", "1920x1080", "2560x1440", "3840x2160"])
        self.size.setEditable(True)
        self.size.setCurrentText(settings.value("rec/size", "1920x1080"))
        form.addRow("Size", self.size)
        self.fps = QComboBox()
        self.fps.addItems(["30", "60", "120"])
        self.fps.setCurrentText(settings.value("rec/fps", "60"))
        self.blur = QCheckBox("Motion blur")
        self.blur.setToolTip(f"Each frame blends the {BLUR_RATE} frames a second the game draws in its time: "
                             "smoother motion, slower recording")
        self.blur.setChecked(settings.value("rec/blur", "false") == "true")
        row = QHBoxLayout()
        row.addWidget(self.fps)
        row.addWidget(self.blur)
        row.addStretch(1)
        form.addRow("Frames per second", row)
        self.quality = QSlider(Qt.Horizontal)
        self.quality.setRange(14, 32)
        self.quality.setInvertedAppearance(True)
        self.quality.setValue(int(settings.value("rec/crf", 20)))
        quality_label = QLabel()
        self.quality.valueChanged.connect(lambda v: quality_label.setText(f"CRF {v}"))
        self.quality.valueChanged.emit(self.quality.value())
        row = QHBoxLayout()
        row.addWidget(QLabel("Smaller"))
        row.addWidget(self.quality, 1)
        row.addWidget(QLabel("Better"))
        row.addWidget(quality_label)
        form.addRow("Quality", row)
        self.codec = QComboBox()
        self.codec.addItems(list(CODECS))
        self.codec.setCurrentText(settings.value("rec/codec", "H.264"))
        form.addRow("Codec", self.codec)
        self.sound = QCheckBox("Sound")
        self.sound.setChecked(settings.value("rec/sound", "true") == "true")
        self.bitrate = QComboBox()
        self.bitrate.addItems(["128k", "192k", "256k", "320k"])
        self.bitrate.setCurrentText(settings.value("rec/bitrate", "192k"))
        self.sound.toggled.connect(self.bitrate.setEnabled)
        self.bitrate.setEnabled(self.sound.isChecked())
        row = QHBoxLayout()
        row.addWidget(self.sound)
        row.addWidget(self.bitrate)
        row.addStretch(1)
        form.addRow("Audio", row)
        self.viewmodel = QCheckBox("The gun in the player's hands (viewmodel)")
        self.viewmodel.setChecked(is_on(settings.value("rec/viewmodel", settings.value("viewmodel", "1"))))
        self.forcemodel = QCheckBox("Everyone as an airborne soldier or a winter German")
        self.forcemodel.setChecked(is_on(settings.value("rec/forcemodel", settings.value("forcemodel", "1"))))
        shown = QVBoxLayout()
        shown.addWidget(self.viewmodel)
        shown.addWidget(self.forcemodel)
        form.addRow("Show", shown)
        self.advanced = QLineEdit(settings.value("rec/advanced", ""))
        self.advanced.setPlaceholderText("FFmpeg output options, instead of the ones above")
        form.addRow("Advanced", self.advanced)
        self.ffmpeg = QLineEdit(settings.value("rec/ffmpeg", "") or find_ffmpeg())
        form.addRow("FFmpeg", self.ffmpeg)

    def error(self):
        """What's wrong with what's entered, None if nothing."""
        if not parse_size(self.size.currentText()):
            return "The video size should be like 1920x1080."
        return None

    def save(self):
        width, height = parse_size(self.size.currentText())
        for key, value in (("size", f"{width}x{height}"), ("fps", self.fps.currentText()),
                           ("blur", "true" if self.blur.isChecked() else "false"),
                           ("crf", self.quality.value()), ("codec", self.codec.currentText()),
                           ("sound", "true" if self.sound.isChecked() else "false"),
                           ("bitrate", self.bitrate.currentText()),
                           ("viewmodel", "1" if self.viewmodel.isChecked() else "0"),
                           ("forcemodel", "1" if self.forcemodel.isChecked() else "0"),
                           ("advanced", self.advanced.text().strip()), ("ffmpeg", self.ffmpeg.text().strip())):
            self.settings.setValue("rec/" + key, value)


class SettingsDialog(QDialog):
    """The game, its files and demos, and how videos are recorded, in tabs
    ("game" and "videos")."""

    def __init__(self, settings, parent=None, tab="game"):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(760)
        self.settings = settings
        self.tabs = QTabWidget()
        game = QWidget()
        form = QFormLayout(game)
        self.fields = {}
        for key, label, folder in (("exe", "Game program", False),
                                   ("game", "Game files folder", True),
                                   ("demos", "Demos folder", True)):
            edit = QLineEdit(settings.value(key, ""))
            edit.setToolTip({"exe": "OpenMoHAA built from this repository",
                             "game": "The folder with main/Pak0.pk3",
                             "demos": "The folders in it too"}[key])
            form.addRow(label, browse_row(edit, lambda _=False, e=edit, d=folder: self.browse(e, d)))
            self.fields[key] = edit
        self.size = QComboBox()
        self.size.addItems(["1280x720", "1600x900", "1920x1080", "2560x1440"])
        self.size.setEditable(True)
        self.size.setCurrentText(settings.value("size", "1280x720"))
        form.addRow("Game window size", self.size)
        self.kill_before = seconds_box(settings, "kill_before", 4.0, "Time shown before each kill (cl_demoKillBefore)")
        self.kill_after = seconds_box(settings, "kill_after", 2.0, "Time shown after each kill (cl_demoKillAfter)")
        form.addRow("Each kill plays", lead_row(self.kill_before, self.kill_after, "it"))
        self.tabs.addTab(game, "Game")
        self.videos = VideoSettings(settings)
        self.tabs.addTab(self.videos, "Videos")
        self.tabs.setCurrentIndex(1 if tab == "videos" else 0)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box = QVBoxLayout(self)
        box.addWidget(self.tabs)
        box.addWidget(buttons)

    def browse(self, edit, folder):
        if folder:
            path = QFileDialog.getExistingDirectory(self, "Choose a folder", edit.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose the game program", edit.text())
        if path:
            edit.setText(path)

    def accept(self):
        error = self.videos.error()
        if error:
            self.tabs.setCurrentWidget(self.videos)
            QMessageBox.warning(self, "Settings", error)
            return
        for key, edit in self.fields.items():
            value = edit.text().strip()
            self.settings.setValue(key, game_program(value) if key == "exe" else value)
        self.settings.setValue("size", self.size.currentText().strip())
        self.settings.setValue("kill_before", str(self.kill_before.value()))
        self.settings.setValue("kill_after", str(self.kill_after.value()))
        self.videos.save()
        super().accept()


class VideoSettingsDialog(QDialog):
    """The Videos tab of Settings on its own."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Video settings")
        self.videos = VideoSettings(settings)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box = QVBoxLayout(self)
        box.addWidget(self.videos)
        box.addWidget(buttons)

    def accept(self):
        error = self.videos.error()
        if error:
            QMessageBox.warning(self, "Video settings", error)
            return
        self.videos.save()
        super().accept()


def range_clip(demo, start, end):
    """A clip of demo from start to end, in msec. {part} in the commands is
    the name of the video the recorder writes, and spans what it plays."""
    return {"demo": demo, "start": start, "end": end, "spans": [(start, end)],
            "commands": ["demoonly", f"demoseek {seconds(start)}", "demopause 0", f"demovideo {{part}} {seconds(end)}"]}


def only_clip(demo, only="", player="", spans=None):
    """A clip of what demoonly plays ("kills", "shownkills" or "watched"), or
    of all of demo; spans: what that is,
    from demoonly_plan(), if known."""
    command = f"demoonly {only}" + (" " + quoted(player) if player else "") if only else "demoonly"
    start = spans[0][0] if (spans and spans[0][0] > 0) else 0
    return {"demo": demo, "start": start, "end": None, "spans": spans,
            "commands": [f"demoseek {seconds(start)}", "demopause 0", command, "demovideo {part}"]}


def stretches(spans, switches=()):
    """(start, end) spans in msec, joined as demoonly does: when less than a
    second apart, unless the player shown changes in between (switches: the
    times it does, in order), which stays a cut."""
    joined = []
    for start, end in sorted(spans):
        if joined and start > joined[-1][1]:
            i = bisect.bisect_left(switches, joined[-1][1])
            cut = start > joined[-1][1] + 1000 or (i < len(switches) and switches[i] <= start)
        else:
            cut = not joined
        if cut:
            joined.append([max(0, start), end])
        else:
            joined[-1][1] = max(joined[-1][1], end)
    return [tuple(span) for span in joined]


def kill_span(index, kill, start, end, before=KILL_BEFORE, after=KILL_AFTER):
    """When kill plays, in msec: from before it to after it, while it can be
    played (start to end), inside its level, as the engine clamps it."""
    levels = [level["time"] for level in index.get("maps", [])]
    i = bisect.bisect_right(levels, kill["time"])
    first = levels[i - 1] if i else 0
    last = levels[i] if i < len(levels) else index.get("duration", 0)
    return max(start, first, kill["time"] - before), min(end, last, kill["time"] + after)


def demoonly_plan(index, only="", player="", kill_before=None, kill_after=None):
    """What demoonly <only> [player] plays in a demo, as the engine does,
    from its index: (how many kills, [(start, end)] in msec), all of the demo
    without only. Team kills aren't counted, and only play with a kill of
    their killer near them; the deaths of the player shown aren't kills."""
    duration = index.get("duration", 0)
    watched = index.get("watched", [])
    switches = [w["time"] for w in watched]
    if not only:
        return 0, [(0, duration)]
    if only == "watched":
        spans = [(w["time"], watched[i + 1]["time"] if i + 1 < len(watched) else duration)
                 for i, w in enumerate(watched) if same_player(w["name"], player)]
        return 0, stretches(spans, switches)
    if only.startswith("shown") or player:
        kills = [(k, start, end) for k, start, end in shown_kills(index)
                 if not player or same_player(player, k["killerName"])]
    else:
        # anyone's, whoever is shown
        kills, switches = [(k, 0, duration) for k in index.get("kills", [])], []
    deaths = deaths_shown(index)
    frags = [x for x in kills if not x[0].get("teamKill") and not is_grenade(x[0]) and id(x[0]) not in deaths]
    count = len(frags)
    kills = frags + team_kills_with(kills, [k for k, _, _ in frags])
    kb = KILL_BEFORE if kill_before is None else kill_before
    ka = KILL_AFTER if kill_after is None else kill_after
    spans = [kill_span(index, k, start, end, kb, ka) for k, start, end in kills]
    return count, stretches([span for span in spans if span[1] > span[0]], switches)


def the(count, one, many):
    """"The 3 kills", "The kill" or "No kills"."""
    return f"The {count} {many}" if count > 1 else f"The {one}" if count else f"No {many}"


def about(msec):
    """How long, roughly: "about 40 min"."""
    minutes = round(msec / 60000)
    if minutes < 1:
        return "under a minute"
    return f"about {minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"about {minutes} min"


def length(spans):
    return sum(end - start for start, end in spans)


class RecordDialog(QDialog):
    """What to record and how, kept in the settings.

    choices: [(key, label, enabled)], what can be recorded, after a stretch
    of time from time when it isn't None. join: offer one video for all.
    relabel(before, after): the choices again for that lead-in and out, in
    msec, as their lengths change with them."""

    def __init__(self, settings, choices, time=None, join=False, parent=None, relabel=None):
        super().__init__(parent)
        self.setWindowTitle("Record a video")
        self.settings = settings
        form = QFormLayout(self)

        group = QButtonGroup(self)
        what = QVBoxLayout()
        self.range = None
        if time is not None:
            self.range = QRadioButton("From")
            self.start = QLineEdit(clock(time))
            self.end = QLineEdit(clock(time + 30000))
            row = QHBoxLayout()
            for widget in (self.range, self.start, QLabel("to"), self.end):
                row.addWidget(widget)
            what.addLayout(row)
            group.addButton(self.range)
        self.choices = {}
        for key, label, enabled in choices:
            button = self.choices[key] = QRadioButton(label)
            group.addButton(button)
            what.addWidget(button)
        # only what there is to record
        self.nothing = QLabel("Nothing to record")
        what.addWidget(self.nothing)
        self.relabel(choices)
        # the last choice, if it's offered
        last = self.range if settings.value("rec/what") == "range" else self.choices.get(settings.value("rec/what"))
        first = self.range or next((b for b in self.choices.values() if b.isEnabled()), None)
        if last and last.isEnabled():
            last.setChecked(True)
        elif first:
            first.setChecked(True)
        form.addRow("Record", what)
        self.join = QCheckBox("One video for all")
        self.join.setChecked(settings.value("rec/join", "true") == "true")
        self.offers_join = join
        if join:
            form.addRow("", self.join)
        else:
            self.join.hide()

        self.kill_before = seconds_box(settings, "rec/kill_before", 4.0,
                                       "Time recorded before each kill (cl_demoKillBefore)", "kill_before")
        self.kill_after = seconds_box(settings, "rec/kill_after", 2.0,
                                      "Time recorded after each kill (cl_demoKillAfter)", "kill_after")
        form.addRow("Each kill", lead_row(self.kill_before, self.kill_after, "it"))
        if relabel:
            # once they stop changing, as a choice of many demos takes a while
            timer = QTimer(self, singleShot=True, interval=150)
            timer.timeout.connect(lambda: self.relabel(relabel(*self.kill_times())))
            self.kill_before.valueChanged.connect(lambda _: timer.start())
            self.kill_after.valueChanged.connect(lambda _: timer.start())

        self.pattern = QLineEdit(settings.value("rec/pattern", "{demo} {start}"))
        self.pattern.setToolTip("{demo} (the demo's file name), {start}, {player} and {date} are replaced")
        form.addRow("File name", self.pattern)
        self.folder = QLineEdit(settings.value("rec/folder", os.path.expanduser("~/Videos")))
        form.addRow("Folder", browse_row(self.folder, self.browse))
        self.summary = QLabel(video_summary(settings))
        self.summary.setWordWrap(True)
        change = QPushButton("Change…")
        change.setToolTip("The video settings, also in Settings")
        change.clicked.connect(self.change_video)
        row = QHBoxLayout()
        row.addWidget(self.summary, 1)
        row.addWidget(change)
        form.addRow("Video", row)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.addButton("Record", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose a folder", self.folder.text())
        if path:
            self.folder.setText(path)

    def change_video(self):
        if VideoSettingsDialog(self.settings, self).exec():
            self.summary.setText(video_summary(self.settings))

    def kill_times(self):
        """The lead-in and out chosen, in msec."""
        return int(self.kill_before.value() * 1000), int(self.kill_after.value() * 1000)

    def relabel(self, choices):
        for key, label, enabled in choices:
            self.choices[key].setText(label)
            self.choices[key].setEnabled(enabled)
            self.choices[key].setHidden(not enabled)
        self.nothing.setHidden(bool(self.range) or any(enabled for _, _, enabled in choices))

    def what(self):
        """"range" or the key of the choice made."""
        if self.range and self.range.isChecked():
            return "range"
        return next((key for key, button in self.choices.items() if button.isChecked()), None)

    def times(self):
        """The stretch of time chosen, in msec."""
        return parse_clock(self.start.text()), parse_clock(self.end.text())

    def accept(self):
        if self.what() is None:
            QMessageBox.warning(self, "Record a video", "There's nothing to record.")
            return
        if self.what() == "range":
            start, end = self.times()
            if start is None or end is None or end <= start:
                QMessageBox.warning(self, "Record a video", "The start and end times aren't right.")
                return
        for key, value in (("folder", self.folder.text()), ("pattern", self.pattern.text()),
                           ("kill_before", str(self.kill_before.value())),
                           ("kill_after", str(self.kill_after.value()))):
            self.settings.setValue("rec/" + key, value)
        if self.offers_join:
            self.settings.setValue("rec/join", "true" if self.join.isChecked() else "false")
        if self.range or len(self.choices) > 1:
            self.settings.setValue("rec/what", self.what())
        super().accept()

    def job(self, label, clips, demo, player=""):
        """A video of clips for the recorder, with the settings chosen; demo
        and player name it."""
        # a demo's file name, without its folders
        demo = demo.rsplit("/", 1)[-1]
        try:
            name = self.pattern.text().format_map({
                "demo": demo, "start": clock(clips[0]["start"]).replace(":", "-"), "player": player or "",
                "date": QDateTime.currentDateTime().toString("yyyy-MM-dd hh-mm")})
        except (KeyError, ValueError, IndexError):
            name = demo
        name = "".join(c for c in name if c not in '/\\:*?"<>|').strip() or demo
        return {"label": label, "clips": clips, **video_settings(self.settings),
                "kill_before": str(self.kill_before.value()),
                "kill_after": str(self.kill_after.value()),
                "output": os.path.join(self.folder.text(), name + ".mp4"), "status": "waiting"}


class Recorder(QObject):
    """Records a job in a second game, offscreen, rendering the sound: its
    clips one after the other, loading their demos, then FFmpeg joins them
    when there are several. on_progress(job, text) tells how it goes, and
    on_done(job, error, video) when it's over."""

    def __init__(self, window, job, on_progress, on_done):
        super().__init__(window)
        self.window = window
        self.job = job
        self.on_progress = on_progress
        self.on_done = on_done
        self.phase = "loading"
        self.error = ""
        self.clip = -1
        self.loaded = None  # the demo the recorder plays
        self.parts = []
        self.joiner = None
        self.parts_dir = None
        # with the settings of the game watched, but changing none
        self.game = Game(self.output, self.exited, keep_config=False)
        self.clock = QElapsedTimer()
        self.elapsed = QElapsedTimer()
        self.timer = QTimer(self, interval=200, timeout=self.poll)

    def start_game(self):
        settings = self.window.settings
        job = self.job
        extra = ["+set", "s_loopback", "1" if job["sound"] else "0", "+set", "s_khz", "44",
                 "+set", "s_volume", "1", "+set", "cl_aviFrameRate", job["fps"],
                 "+set", "cl_aviFFmpeg", job["ffmpeg"], "+set", "cl_aviPipeFormat", job["options"],
                 "+set", "com_maxfps", "0", "+set", "r_swapInterval", "0"]
        # offscreen where SDL can, the recorder doesn't need a window
        driver = "offscreen" if sys.platform.startswith("linux") else None
        viewmodel = job.get("viewmodel", settings.value("rec/viewmodel", settings.value("viewmodel", "1")))
        forcemodel = job.get("forcemodel", settings.value("rec/forcemodel", settings.value("forcemodel", "1")))
        kill_before = job.get("kill_before", settings.value("rec/kill_before", settings.value("kill_before", "4.0")))
        kill_after = job.get("kill_after", settings.value("rec/kill_after", settings.value("kill_after", "2.0")))
        self.game.start(settings.value("exe"), settings.value("game"), settings.value("demos"),
                        job["width"], job["height"], extra, driver,
                        viewmodel=viewmodel, forcemodel=forcemodel,
                        kill_before=kill_before, kill_after=kill_after, videos=self.parts_dir)

    def start(self):
        # the clips are written next to the video, as the game's home may be
        # in memory (/tmp), and the video then only moves
        try:
            folder = os.path.dirname(self.job["output"]) or "."
            os.makedirs(folder, exist_ok=True)
            self.parts_dir = tempfile.mkdtemp(prefix=".mohreplay-parts-", dir=folder)
        except OSError:
            self.parts_dir = tempfile.mkdtemp(prefix="mohreplay-parts-")
        self.start_game()
        self.elapsed.start()
        self.on_progress(self.job, "Starting the recorder…")
        self.timer.start()
        self.next_clip()

    def output(self, line):
        if line.startswith("ERROR:") or "Couldn't write" in line or "Couldn't run" in line:
            self.error = line.replace("^1", "")

    def part(self):
        """The name of the video of this clip."""
        return f"part{self.clip}"

    def made(self):
        """The video the recorder writes."""
        return os.path.join(self.parts_dir, self.part() + ".mp4")

    def next_clip(self):
        self.clip += 1
        self.clock.start()
        if self.clip == len(self.job["clips"]):
            self.join()
            return
        demo = self.job["clips"][self.clip]["demo"]
        if demo != self.loaded:
            if self.loaded is not None:
                self.phase = "restarting"
                self.game.stop()
                self.error = ""
                self.start_game()
            self.game.send("demo " + quoted(demo))
            self.loaded = demo
            self.phase = "loading"
        else:
            self.send_clip()

    def send_clip(self):
        commands = self.job["clips"][self.clip]["commands"]
        self.game.send(*(command.replace("{part}", self.part()) for command in commands))
        self.phase = "starting"

    def poll(self):
        # looked at first: the game says it records before FFmpeg makes the
        # file, and that it's done once FFmpeg has quit
        made = os.path.isfile(self.made())
        state = self.game.read_json("demostate.json") or {}
        if self.phase == "loading":
            if state.get("demo") == self.loaded and state.get("time", 0) > 0 and not state.get("seeking"):
                self.send_clip()
            elif self.clock.elapsed() > 120000:
                self.finish(f"The recorder didn't load {self.loaded}. {self.error}")
        elif self.phase == "starting":
            if state.get("recording"):
                self.phase = "recording"
            elif made:
                self.clip_done()  # over between two polls
            elif self.clock.elapsed() > 180000 or self.error:
                self.finish("The recording didn't start. " + self.error)
        elif self.phase == "recording":
            text = "Recording " + self.how_far(state.get("time", 0))
            if len(self.job["clips"]) > 1:
                text += f" (clip {self.clip + 1} of {len(self.job['clips'])})"
            self.on_progress(self.job, text + "…")
            if not state.get("recording"):
                self.clip_done()

    def how_far(self, time):
        """How much of the video is done, and how long the rest will take,
        from what the clips play, else the time in the demo."""
        clips = self.job["clips"]
        total = sum(length(clip["spans"] or []) for clip in clips)
        if not total or any(clip["spans"] is None for clip in clips):
            return clock(time)
        done = sum(length(clip["spans"]) for clip in clips[:self.clip])
        done += sum(max(0, min(end, time) - start) for start, end in clips[self.clip]["spans"])
        share = min(0.99, done / total)
        text = f"{int(share * 100)}%"
        # once it's going
        if share >= 0.02:
            text += ", " + about(self.elapsed.elapsed() * (1 - share) / share) + " left"
        return text

    def clip_done(self):
        log_file = self.made() + ".log"
        log_content = ""
        if os.path.isfile(log_file):
            try:
                with open(log_file, errors="replace") as f:
                    log_content = f.read()
            except OSError:
                pass

        if self.error:
            # FFmpeg quit midway: what it wrote isn't the whole video
            self.finish("The recording stopped. " + self.error)
        elif not os.path.isfile(self.made()) or os.path.getsize(self.made()) == 0:
            err = "FFmpeg didn't write the video."
            if log_content:
                err += "\n" + log_content[-1500:]
            self.finish(err)
        elif "No space left on device" in log_content:
            self.finish("FFmpeg ran out of disk space while writing the clip.\n" + log_content[-1500:])
        else:
            self.parts.append(self.made())
            self.next_clip()

    def join(self):
        self.phase = "joining"
        # each clip was checked when it was done: all of them go in
        if not self.parts:
            self.finish("There was nothing to record.")
            return
        if len(self.parts) == 1:
            self.finish(video=self.parts[0])
            return
        self.on_progress(self.job, f"Joining {len(self.parts)} clips…")
        folder = self.parts_dir
        with open(os.path.join(folder, "parts.txt"), "w") as f:
            f.writelines("file '" + part.replace("'", "'\\''") + "'\n" for part in self.parts)
        self.joiner = QProcess(self)
        self.joiner.setProcessChannelMode(QProcess.MergedChannels)
        self.joiner.finished.connect(self.joined)
        self.joiner.errorOccurred.connect(
            lambda error: error == QProcess.FailedToStart and self.finish("Couldn't run FFmpeg to join the clips."))
        self.joiner.start(self.job["ffmpeg"], ["-loglevel", "warning", "-y", "-f", "concat", "-safe", "0",
                                               "-i", os.path.join(folder, "parts.txt"), "-c", "copy",
                                               "-movflags", "+faststart", os.path.join(folder, "joined.mp4")])

    def joined(self, code=0, status=None):
        if self.phase != "joining":
            return
        video = os.path.join(self.parts_dir, "joined.mp4")
        if code or not os.path.isfile(video) or os.path.getsize(video) == 0:
            text = bytes(self.joiner.readAll()).decode("utf-8", "replace")
            self.finish("FFmpeg couldn't join the clips.\n" + text[-1500:])
        else:
            self.finish(video=video)

    def finish(self, error="", video=None):
        """Over: the video goes to the folder chosen, or the error is told,
        with FFmpeg's log."""
        self.timer.stop()
        self.phase = "done"
        log = self.made() + ".log"
        if not os.path.isfile(log) and self.parts:
            log = self.parts[-1] + ".log"
        if error and os.path.isfile(log):
            with open(log, errors="replace") as f:
                error += "\n" + f.read()[-1500:]
        output = self.job["output"]
        if video:
            try:
                os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
                base, number = output[:-4], 2
                while os.path.exists(output):
                    output = f"{base} ({number}).mp4"
                    number += 1
                shutil.move(video, output)
            except OSError as e:
                error, video = f"Couldn't move the video to {output}: {e}", None
        self.game.stop()
        if self.parts_dir:
            shutil.rmtree(self.parts_dir, ignore_errors=True)
        self.on_done(self.job, error.strip(), output if video else None)

    def cancel(self):
        if self.phase != "done":
            self.phase = "done"
            self.timer.stop()
            if self.joiner:
                self.joiner.kill()
                self.joiner.waitForFinished(1000)
            self.game.stop()
            if self.parts_dir:
                shutil.rmtree(self.parts_dir, ignore_errors=True)
            self.on_done(self.job, "canceled", None)

    def exited(self):
        if self.phase != "done":
            self.finish("The recorder quit. " + self.error)


class Window(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.game = Game(self.game_output, self.game_exited)
        self.state = {}
        self.index = None
        self.pending_demo = None
        self.seeking_slider = False
        self.recorder = None
        self.jobs = []
        self.last_jump = (None, 0)
        self.play_mode_asked = -1
        self.round_marks = self.kill_marks = []
        self.library = Library(self.library_changed, self)
        self.demo_items = {}  # name: item
        self.folder_items = {}  # folder in the demos folder: item
        self.wanted_player = None  # to choose once the demo plays

        self.setWindowTitle("MoH Demo Replay")
        self.resize(1200, 720)
        style = self.style()

        # demos
        self.filter = QLineEdit(placeholderText="Filter demos by name, map or player")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.filter_demos)
        self.rules = QComboBox()
        self.rules.addItem("All demos", "")
        self.rules.addItem("Default", "default")
        self.rules.addItem("Realism", "realism")
        self.rules.setToolTip("Default or realism servers, told by the speeds of the recorder's weapons")
        self.rules.setCurrentIndex(max(0, self.rules.findData(settings.value("rules", ""))))
        self.rules.currentIndexChanged.connect(self.rules_changed)
        self.demos = self.make_list(DEMO_COLUMNS, fit=False)
        self.demos.setSortingEnabled(True)
        self.demos.sortByColumn(COL_DATE, Qt.DescendingOrder)
        self.demos.itemActivated.connect(lambda item: item.data(COL_DEMO, Qt.UserRole)
                                         and self.play_demo(item.data(COL_DEMO, Qt.UserRole)))
        self.add_menu(self.demos, "Record the demos selected…", self.record_demos)
        left = QWidget()
        box = QVBoxLayout(left)
        box.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.filter, 1)
        row.addWidget(self.rules)
        box.addLayout(row)
        box.addWidget(self.demos)

        # what's in the demo: its name, what it is and what plays
        self.title = QLabel("No demo")
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        self.title.setFont(font)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.info = QLabel("Double-click a demo to play it")
        self.info.setWordWrap(True)
        palette = self.info.palette()
        palette.setColor(QPalette.WindowText, palette.color(QPalette.PlaceholderText))
        self.info.setPalette(palette)
        self.player = QComboBox()
        self.player.setToolTip("Whose kills are listed, played and recorded")
        self.player.currentIndexChanged.connect(self.fill_kills)
        # what plays: one of three, as the game tells
        self.play_modes = QButtonGroup(self)
        self.everything_button = QPushButton("Everything")
        self.only_kills_button = QPushButton("These kills")
        self.watched_button = QPushButton("While watched")
        modes = QHBoxLayout()
        modes.setSpacing(0)
        for button, text, action in (
                (self.everything_button, "Play all of the demo", lambda: self.game.send("demoonly") or True),
                (self.only_kills_button, "Play only the kills listed, from the one selected or the first",
                 self.only_kills),
                (self.watched_button, "Play only while this player is shown, from the first time", self.only_watched)):
            button.setCheckable(True)
            button.setToolTip(text)
            button.clicked.connect(lambda _=False, a=action: self.play_mode_clicked(a))
            self.play_modes.addButton(button)
            modes.addWidget(button)
        self.everything_button.setChecked(True)
        players = QHBoxLayout()
        players.addWidget(QLabel("Player"))
        players.addWidget(self.player, 1)
        players.addSpacing(12)
        players.addWidget(QLabel("Play"))
        players.addLayout(modes)

        self.kills = self.make_list(["Time", "Killer", "Victim"])
        self.rounds = self.make_list(["Time", "Round", "Result"])
        # a click jumps, so does Enter
        for tree in (self.kills, self.rounds):
            tree.itemClicked.connect(self.jump_to_item)
            tree.itemActivated.connect(self.jump_to_item)
        self.add_menu(self.kills, "Record the ones selected…", lambda: self.record_selected(self.kills))
        self.tabs = tabs = QTabWidget()
        tabs.addTab(self.kills, "Kills")
        tabs.addTab(self.rounds, "Rounds")
        tabs.currentChanged.connect(self.tab_changed)
        right = QWidget()
        box = QVBoxLayout(right)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.title)
        box.addWidget(self.info)
        box.addSpacing(4)
        box.addLayout(players)
        box.addWidget(tabs)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([560, 640])

        # playback
        self.slider = Timeline()
        self.slider.sliderPressed.connect(lambda: setattr(self, "seeking_slider", True))
        self.slider.sliderReleased.connect(self.slider_released)
        self.slider.sliderMoved.connect(lambda v: self.time.setText(f"{clock(v)} / {clock(self.slider.maximum())}"))
        self.time = QLabel("0:00 / 0:00")
        self.play = self.make_button(QStyle.SP_MediaPause, "Pause or play (Pause key)", lambda: self.game.send("demopause"))
        self.speed = QComboBox()
        self.speed.addItems([s + "×" for s in SPEEDS])
        self.speed.setCurrentText("1×")
        self.speed.setToolTip("Playback speed")
        self.speed.currentTextChanged.connect(lambda s: self.game.send("timescale " + s.rstrip("×")))
        controls = QHBoxLayout()
        controls.setSpacing(2)
        # the kill buttons go to the kills listed
        for icon, tip, command in ((QStyle.SP_MediaSkipBackward, "Previous round (PgDn)", "demoprevround"),
                                   (QStyle.SP_MediaSeekBackward, "Previous kill listed (Down)",
                                    lambda: self.kill_commands()[1]),
                                   (None, "Back 10 seconds (Left: 5)", "demoskip -10")):
            controls.addWidget(self.make_button(icon, tip, lambda _=False, c=command: self.game.send(
                c() if callable(c) else c), "-10 s"))
        controls.addWidget(self.play)
        for icon, tip, command in ((None, "Forward 10 seconds (Right: 5)", "demoskip 10"),
                                   (QStyle.SP_MediaSeekForward, "Next kill listed (Up)", lambda: self.kill_commands()[0]),
                                   (QStyle.SP_MediaSkipForward, "Next round (PgUp)", "demonextround")):
            controls.addWidget(self.make_button(icon, tip, lambda _=False, c=command: self.game.send(
                c() if callable(c) else c), "+10 s"))
        controls.addSpacing(8)
        controls.addWidget(self.slider, 1)
        controls.addSpacing(8)
        controls.addWidget(self.time)
        controls.addSpacing(8)
        controls.addWidget(self.speed)
        controls.addSpacing(8)
        record = QPushButton(theme_icon("media-record", record_icon()), "Record…")
        record.setToolTip("Record a video of this demo")
        record.clicked.connect(self.record)
        controls.addWidget(record)

        central = QWidget()
        box = QVBoxLayout(central)
        box.addWidget(splitter, 1)
        box.addLayout(controls)
        self.setCentralWidget(central)

        # the videos recorded and to record
        self.queue = self.make_list(["Video", "What", "Status"])
        self.queue.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.queue.itemActivated.connect(self.open_video)
        buttons = QHBoxLayout()
        for text, action in (("Cancel", self.cancel_jobs), ("Remove finished", self.remove_finished),
                             ("Open folder", self.open_folder)):
            button = QPushButton(text)
            button.clicked.connect(action)
            buttons.addWidget(button)
        buttons.addStretch(1)
        videos = QWidget()
        box = QVBoxLayout(videos)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.queue)
        box.addLayout(buttons)
        self.queue_dock = QDockWidget("Videos", self)
        self.queue_dock.setObjectName("videos")
        self.queue_dock.setWidget(videos)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.queue_dock)
        self.queue_dock.hide()

        toolbar = QToolBar("Toolbar")
        toolbar.setObjectName("toolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)
        self.folder_action = toolbar.addAction(theme_icon("folder-open", QStyle.SP_DirOpenIcon), "")
        self.folder_action.triggered.connect(self.choose_folder)
        self.restart_action = toolbar.addAction(theme_icon("view-refresh", QStyle.SP_BrowserReload), "Restart game")
        self.restart_action.setToolTip("Start the game again, with the demo playing")
        self.restart_action.triggered.connect(self.start_game)
        toolbar.addSeparator()
        self.viewmodel_action = toolbar.addAction("Viewmodel")
        self.viewmodel_action.setToolTip("The gun in the player's hands")
        self.viewmodel_action.setCheckable(True)
        self.viewmodel_action.setChecked(is_on(settings.value("viewmodel", "1")))
        self.viewmodel_action.toggled.connect(self.toggle_viewmodel)
        self.forcemodel_action = toolbar.addAction("Force models")
        self.forcemodel_action.setToolTip("Everyone as an airborne soldier or a winter German")
        self.forcemodel_action.setCheckable(True)
        self.forcemodel_action.setChecked(is_on(settings.value("forcemodel", "1")))
        self.forcemodel_action.toggled.connect(self.toggle_forcemodel)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)
        videos_action = self.queue_dock.toggleViewAction()
        videos_action.setIcon(theme_icon("video-x-generic", QStyle.SP_FileDialogContentsView))
        toolbar.addAction(videos_action)
        settings_action = toolbar.addAction(theme_icon("configure", QStyle.SP_FileDialogDetailedView), "Settings…")
        settings_action.triggered.connect(lambda: self.edit_settings())
        self.show_folder()

        self.indexing = QLabel()
        self.statusBar().addPermanentWidget(self.indexing)

        self.filter.setFocus()

        self.poller = QTimer(interval=100, timeout=self.poll)
        self.poller.start()
        self.fill_demos()
        # once the window is up: the indexes kept take a moment to load
        self.library_opener = QTimer(self, singleShot=True, interval=0, timeout=lambda: self.library.open(
            settings.value("demos", ""), settings.value("exe", "")))
        self.library_opener.start()

    def make_list(self, columns, fit=True):
        """fit: columns fit their contents, which costs a look at every row
        each time one changes; else fit_columns() does it."""
        tree = QTreeWidget()
        tree.setHeaderLabels(columns)
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.header().setSectionResizeMode(QHeaderView.ResizeToContents if fit else QHeaderView.Interactive)
        tree.header().setStretchLastSection(True)
        return tree

    def fit_columns(self, tree):
        for column in range(tree.columnCount() - 1):
            tree.resizeColumnToContents(column)

    def add_menu(self, tree, text, action):
        """Several items can be selected, and a right-click offers action."""
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tree.setContextMenuPolicy(Qt.ActionsContextMenu)
        menu_action = QAction(text, tree)
        menu_action.triggered.connect(action)
        tree.addAction(menu_action)

    def make_button(self, icon, tip, action, text=""):
        button = QToolButton()
        if icon is not None:
            button.setIcon(self.style().standardIcon(icon))
        else:
            button.setText(text)
        button.setToolTip(tip)
        button.clicked.connect(action)
        return button

    # settings and game

    def ready(self):
        exe, game, demos = (self.settings.value(k, "") for k in ("exe", "game", "demos"))
        return os.path.isfile(exe) and os.path.isdir(game) and os.path.isdir(demos)

    def show_folder(self):
        folder = self.settings.value("demos", "")
        self.folder_action.setText(os.path.basename(folder.rstrip("/\\")) or "Choose the demos folder")
        self.folder_action.setToolTip((folder + "\n" if folder else "") + "Choose another folder of demos")

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose the demos folder", self.settings.value("demos", ""))
        if not folder or folder == self.settings.value("demos", ""):
            return
        self.settings.setValue("demos", folder)
        self.show_folder()
        self.fill_demos()
        self.library.open(folder, self.settings.value("exe", ""))
        # the game has the folder it started with
        if self.ready() and self.game.running():
            self.start_game()

    def toggle_viewmodel(self, checked):
        val = "1" if checked else "0"
        self.settings.setValue("viewmodel", val)
        if self.game.running():
            self.game.send(f"set cg_drawviewmodel {val}")
        self.statusBar().showMessage(f"Viewmodel: {'ON' if checked else 'OFF'}", 2000)

    def toggle_forcemodel(self, checked):
        val = "1" if checked else "0"
        self.settings.setValue("forcemodel", val)
        if self.game.running():
            self.game.send(f"set cg_forceModel {val}")
        self.statusBar().showMessage(f"Force models: {'ON' if checked else 'OFF'} (Airborne / Winter 1)", 2000)

    def get_kill_times(self):
        """Pre-kill and post-kill times in msec from settings for playback."""
        try:
            kb = int(float(self.settings.value("kill_before", 4.0)) * 1000)
        except (ValueError, TypeError):
            kb = KILL_BEFORE
        try:
            ka = int(float(self.settings.value("kill_after", 2.0)) * 1000)
        except (ValueError, TypeError):
            ka = KILL_AFTER
        return max(500, kb), max(500, ka)

    def get_rec_kill_times(self):
        """Pre-kill and post-kill times in msec from settings for recording."""
        try:
            kb = int(float(self.settings.value("rec/kill_before", self.settings.value("kill_before", 4.0))) * 1000)
        except (ValueError, TypeError):
            kb = KILL_BEFORE
        try:
            ka = int(float(self.settings.value("rec/kill_after", self.settings.value("kill_after", 2.0))) * 1000)
        except (ValueError, TypeError):
            ka = KILL_AFTER
        return max(500, kb), max(500, ka)

    def edit_settings(self, tab="game"):
        if SettingsDialog(self.settings, self, tab).exec():
            self.show_folder()
            self.fill_demos()
            self.fill_kills()
            kb = self.settings.value("kill_before", "4.0")
            ka = self.settings.value("kill_after", "2.0")
            if self.game.running():
                self.game.send(f"seta cl_demoKillBefore {kb}", f"seta cl_demoKillAfter {ka}")
            self.library.open(self.settings.value("demos", ""), self.settings.value("exe", ""))
            if self.ready() and self.game.running():
                self.start_game()

    def start_game(self):
        if not self.ready():
            self.statusBar().showMessage("Set the game program, game files and demos folders in Settings")
            return
        self.game.stop()
        try:
            width, height = (int(v) for v in self.settings.value("size", "1280x720").split("x"))
        except ValueError:
            width, height = 1280, 720
        vm = self.settings.value("viewmodel", "1")
        fm = self.settings.value("forcemodel", "1")
        kb = self.settings.value("kill_before", "4.0")
        ka = self.settings.value("kill_after", "2.0")
        self.game.start(self.settings.value("exe"), self.settings.value("game"), self.settings.value("demos"), width, height,
                        viewmodel=vm, forcemodel=fm, kill_before=kb, kill_after=ka)
        self.statusBar().showMessage("Starting the game…")
        if self.pending_demo:
            self.game.send("demo " + quoted(self.pending_demo))

    def game_output(self, line):
        if line.startswith("ERROR:") or "Couldn't load" in line or line.startswith("The game didn't start"):
            self.statusBar().showMessage(line.replace("ERROR: ", ""))

    def game_exited(self):
        self.statusBar().showMessage("The game quit. Double-click a demo or use Restart game to start it again.")
        self.state = {}
        self.show_state()

    # demos

    def fill_demos(self):
        """Lists the demos of the folder, in the folders they're in, with
        what the library knows of them, keeping the items there."""
        demos = list_demos(self.settings.value("demos", ""))
        for name in [n for n in self.demo_items if n not in demos]:
            item = self.demo_items.pop(name)
            (item.parent() or self.demos.invisibleRootItem()).removeChild(item)
        # the folders left empty, those in them first
        for folder in sorted(self.folder_items, key=len, reverse=True):
            item = self.folder_items[folder]
            if not item.childCount():
                (item.parent() or self.demos.invisibleRootItem()).removeChild(item)
                del self.folder_items[folder]
        self.demos.setSortingEnabled(False)
        for name, path in demos.items():
            item = self.demo_items.get(name)
            if item is None:
                try:
                    mtime = int(os.path.getmtime(path))
                except OSError:
                    mtime = 0
                date = QDateTime.fromSecsSinceEpoch(mtime).toString("yyyy-MM-dd hh:mm")
                item = self.demo_items[name] = SortItem([""] * len(DEMO_COLUMNS))
                item.setText(COL_DATE, date)
                item.setText(COL_DEMO, name.rsplit("/", 1)[-1])
                item.setData(COL_DEMO, Qt.UserRole, name)
                self.folder_item(posixpath.dirname(name)).addChild(item)
            summary = self.library.demos.get(name)
            if summary and item.data(COL_LENGTH, Qt.UserRole) != summary["duration"]:
                item.setText(COL_LENGTH, clock(summary["duration"]))
                item.setData(COL_LENGTH, Qt.UserRole, summary["duration"])
                item.setText(COL_RULES, RULES.get(summary["rules"], ""))
                item.setText(COL_RECORDER, summary["recorder"])
                item.setText(COL_MAP, ", ".join(m.rsplit("/", 1)[-1] for m in summary["maps"]))
                players = sorted(summary["players"].values(), key=lambda p: (-p[1], p[0].lower()))
                rules = f", {RULES[summary['rules']].lower()}" if summary["rules"] else ""
                tip = (f"<b>{html.escape(', '.join(summary['maps']))}</b> — {clock(summary['duration'])}{rules}<br>"
                       + html.escape(", ".join(player_kills(p) for p in players)))
                for column in range(len(DEMO_COLUMNS)):
                    item.setToolTip(column, tip)
        self.demos.setRootIsDecorated(bool(self.folder_items))
        self.demos.setSortingEnabled(True)
        self.filter_demos()

    def folder_item(self, folder):
        """The item of a folder in the demos folder, with those it's in; the
        list itself for none."""
        if not folder:
            return self.demos.invisibleRootItem()
        item = self.folder_items.get(folder)
        if item is None:
            parent = self.folder_item(posixpath.dirname(folder))
            item = self.folder_items[folder] = FolderItem(folder)
            item.setIcon(COL_DEMO, theme_icon("folder", QStyle.SP_DirIcon))
            item.setToolTip(COL_DEMO, plain_tip(folder))
            parent.addChild(item)
            item.setExpanded(True)
        return item

    def demos_of(self, items):
        """The names of the demos shown among items, and in the folders among
        them."""
        names = []
        for item in items:
            if item.isHidden():
                continue
            if isinstance(item, FolderItem):
                names += self.demos_of(item.child(i) for i in range(item.childCount()))
            elif item.data(COL_DEMO, Qt.UserRole):
                names.append(item.data(COL_DEMO, Qt.UserRole))
        return list(dict.fromkeys(names))

    def rules_changed(self):
        self.settings.setValue("rules", self.rules.currentData())
        self.filter_demos()

    def filter_demos(self):
        """Shows the demos where each word is in the name, a map or a
        player's name, and the players found; only those of the rules
        chosen, if any, and not those shorter than MIN_LENGTH."""
        words = [w for w in (clean_name(w) for w in self.filter.text().split()) if w]
        rules = self.rules.currentData()
        found_any = False
        for name, item in self.demo_items.items():
            summary = self.library.demos.get(name) or {}
            players = summary.get("players", {})
            maps = " ".join(summary.get("maps", [])).lower()
            found, shown = {}, not rules or summary.get("rules") in (rules, "both")
            shown = shown and summary.get("duration", MIN_LENGTH) >= MIN_LENGTH
            for word in words if shown else ():
                if word in name.lower() or word in maps:
                    continue
                hits = [key for key in players if word in key]
                if not hits:
                    shown = False
                    break
                found.update((key, players[key]) for key in hits)
            item.setHidden(not shown)
            best = sorted(found.values(), key=lambda p: (-p[1], p[0].lower()))
            item.setText(COL_PLAYER, ", ".join(player_kills(p) for p in best))
            item.setData(COL_PLAYER, Qt.UserRole, best[0][0] if best else None)
            found_any |= shown and bool(best)
        # a folder shows how many of its demos are shown, those in it first
        counts = {}
        for folder in sorted(self.folder_items, key=len, reverse=True):
            item = self.folder_items[folder]
            children = [item.child(i) for i in range(item.childCount())]
            count = counts[folder] = sum(counts[c.folder] if isinstance(c, FolderItem) else not c.isHidden()
                                         for c in children)
            item.setText(COL_DEMO, f"{posixpath.basename(folder)} ({count})")
            item.setHidden(not count)
            if words and count:
                item.setExpanded(True)
        self.demos.setColumnHidden(COL_PLAYER, not found_any)
        self.fit_columns(self.demos)

    def library_changed(self):
        self.fill_demos()
        progress = self.library.progress()
        self.indexing.setText(f"Indexing demos: {progress[0]} of {progress[1]}" if progress else "")

    def play_demo(self, name):
        self.pending_demo = name
        # found by a player's name: that player's kills
        item = self.demo_items.get(name)
        self.wanted_player = (item.data(COL_PLAYER, Qt.UserRole)
                              if item and not self.demos.isColumnHidden(COL_PLAYER) else None)
        if self.game.running():
            self.game.send("demo " + quoted(name))
        else:
            self.start_game()
        self.statusBar().showMessage("Loading " + name + "…")

    # what the game reports

    def poll(self):
        state = self.game.read_json("demostate.json") if self.game.running() else None
        if state is None or state == self.state:
            return
        if state.get("demo") and state.get("demo") != self.state.get("demo"):
            self.load_index(state["demo"])
        self.state = state
        self.show_state()

    def load_index(self, demo):
        index = self.game.read_json("demoindex.json")
        self.index = index if index and index.get("demo") == demo else None
        self.pending_demo = None
        self.statusBar().showMessage("Playing " + demo)
        self.fill_index()

    def show_state(self):
        demo = self.state.get("demo")
        duration = self.state.get("duration", 0) if demo else 0
        time = self.state.get("time", 0) if demo else 0
        paused = self.state.get("paused", False)
        self.play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay if paused else QStyle.SP_MediaPause))
        if not self.seeking_slider:
            self.slider.setMaximum(duration)
            self.slider.setValue(time)
            self.time.setText(f"{clock(time)} / {clock(duration)}")
        only = self.state.get("only")
        if only in ("kills", "multikills"):
            mode = ("only the kills" if only == "kills" else "only the multi-kills") + (
                " by " + self.state["player"] if self.state.get("player") else "")
        elif only in ("shownkills", "shownmultikills"):
            mode = ("only the kills" if only == "shownkills" else "only the multi-kills") + " of whoever is shown"
        elif only == "watched":
            mode = "only while " + self.state.get("player", "") + " is watched"
        else:
            mode = ""
        self.show_play_mode()
        if not demo:
            self.title.setText("No demo")
            self.info.setText("Double-click a demo to play it")
            if not self.pending_demo:
                self.index = None
                self.fill_index()
        elif self.index:
            # its file name, and the folders it's in with the rest
            folder, _, name = demo.rpartition("/")
            self.title.setText(name)
            maps = ", ".join(m["map"] + (f" ({m['rules']})" if m.get("rules") in ("realism", "default") else "")
                             for m in self.index["maps"])
            recorder = self.index["recorder"]["name"]
            parts = [folder.replace("/", " › ")] if folder else []
            parts += [maps, clock(duration)] + ([f"recorded by {recorder}"] if recorder else [])
            text = html.escape(" · ".join(parts)) + (" (truncated)" if self.index.get("truncated") else "")
            text += f"<br>Playing {html.escape(mode)}" if mode else ""
            text += " — paused" if paused else ""
            self.info.setText(text)

    def play_mode_clicked(self, action):
        if action():
            # checked until the game tells it plays that
            self.play_mode_asked = monotonic()
        else:
            self.show_play_mode(True)

    def show_play_mode(self, now=False):
        """The Play button of what the game plays, but for a second after
        one is clicked."""
        if not now and monotonic() - self.play_mode_asked < 1:
            return
        only = self.state.get("only") or ""
        (self.watched_button if only == "watched" else self.only_kills_button if only
         else self.everything_button).setChecked(True)

    def fill_index(self):
        index = self.index or {}
        names = set()
        for kill in index.get("kills", []):
            names.update(n for n in (kill["killerName"], kill["victimName"]) if n)
        current = self.player.currentData()
        self.player.blockSignals(True)
        self.player.clear()
        self.player.addItem("All players", "")
        self.player.addItem("Whoever is shown", SHOWN)
        for name in sorted(names, key=str.lower):
            self.player.addItem(name, name)
        found = self.player.findData(current)
        if self.wanted_player:
            found = next((i for i in range(2, self.player.count())
                          if same_player(self.player.itemData(i), self.wanted_player)), found)
            self.wanted_player = None
        self.player.setCurrentIndex(max(0, found))
        self.player.blockSignals(False)

        self.rounds.clear()
        ends = index.get("roundEnds", [])
        starts = [0] + [r["time"] for r in index.get("rounds", [])]
        for number, start in enumerate(starts, 1):
            end = starts[number] if number < len(starts) else index.get("duration", 0) + 1
            result = next((e["text"] for e in ends if start <= e["time"] < end), "")
            item = QTreeWidgetItem([clock(start), str(number), result])
            item.setData(0, Qt.UserRole, start)
            self.rounds.addTopLevelItem(item)
        self.round_marks = [(start, f"Round {number}") for number, start in enumerate(starts, 1) if number > 1]
        self.fill_kills()

    def player_kills(self, player):
        """The kills to list, all of them, the player's while they're shown or
        whoever's is shown (SHOWN), as {id(kill): (kill, from, to)} in order,
        with when they can be played: as demoonly kills plays them. Team
        kills too, for the clips of the kills near them."""
        index = self.index or {}
        if not player:
            return {id(k): (k, 0, index.get("duration", 0) + KILL_AFTER) for k in index.get("kills", [])}
        return {id(k): (k, start, end) for k, start, end in shown_kills(index)
                if player == SHOWN or same_player(player, k["killerName"])}

    def kill_commands(self):
        """The commands to the next and previous kills listed: anyone's, the
        player's while shown or whoever's is shown."""
        player = self.player.currentData() or ""
        if player == SHOWN:
            return "demonextshownkill", "demoprevshownkill"
        by = " " + quoted(player) if player else ""
        return "demonextkill" + by, "demoprevkill" + by

    def switches(self):
        """When the player shown changes: the kills of a player or of whoever
        is shown aren't joined across it, all the kills are."""
        return [w["time"] for w in (self.index or {}).get("watched", [])] if self.player.currentData() else []

    def fill_kills(self):
        """The kills listed, each item with when it starts and its clips: the
        stretches demoonly plays. Team kills aren't kills: they're only in
        the clips of a kill of their killer near them, as demoonly plays.
        Nor are the deaths of the player shown."""
        player = self.player.currentData() or ""
        kills = list(self.player_kills(player).values())
        kb, ka = self.get_kill_times()
        spans = {id(kill): kill_span(self.index or {}, kill, start, end, kb, ka) for kill, start, end in kills}
        switches = self.switches()
        # items keep their kills by place in the index: Qt gives copies back
        place = {id(kill): i for i, kill in enumerate((self.index or {}).get("kills", []))}
        deaths = deaths_shown(self.index or {})
        self.kills.clear()
        self.kill_marks = []
        for kill, _, _ in kills:
            if kill.get("teamKill") or is_grenade(kill) or id(kill) in deaths:
                continue
            killer, victim, how = kill["killerName"], kill["victimName"], kill["text"]
            span = spans[id(kill)]
            clips = stretches([span] + [spans[id(k)] for k, _, _ in team_kills_with(kills, [kill])], switches)
            item = QTreeWidgetItem([clock(kill["time"]), killer, victim])
            for column in range(3):
                item.setToolTip(column, plain_tip(how))
            item.setData(0, Qt.UserRole, span[0])
            item.setData(1, Qt.UserRole, clips)
            item.setData(2, Qt.UserRole, [place[id(kill)]])
            self.kills.addTopLevelItem(item)
            self.kill_marks.append((kill["time"], how))
        self.tabs.setTabText(0, f"Kills ({self.kills.topLevelItemCount()})")
        self.tabs.setTabText(1, f"Rounds ({self.rounds.topLevelItemCount()})")
        # Up and Down too
        next_kill, prev_kill = self.kill_commands()
        self.game.send("bind UPARROW " + next_kill, "bind DOWNARROW " + prev_kill)
        self.tab_changed()

    def tab_changed(self):
        """The slider marks the kills listed and the rounds."""
        self.slider.set_marks(self.kill_marks, self.round_marks)

    # actions

    def jump_to_item(self, item):
        # a double-click also activates the item: one jump is enough
        if item is self.last_jump[0] and monotonic() - self.last_jump[1] < 1:
            return
        self.last_jump = (item, monotonic())
        self.game.send("demoseek " + seconds(item.data(0, Qt.UserRole)))

    def slider_released(self):
        self.seeking_slider = False
        self.game.send("demoseek " + seconds(self.slider.value()))

    def only_kills(self):
        """Plays the kills listed, from the one selected or the first; False
        if there are none."""
        item = self.kills.currentItem() or self.kills.topLevelItem(0)
        if not item:
            QMessageBox.information(self, "Only these kills", "There are no kills to play.")
            return False
        player = self.player.currentData() or ""
        if player == SHOWN:
            only = "demoonly shownkills"
        else:
            only = "demoonly kills" + (" " + quoted(player) if player else "")
        self.game.send("demoseek " + seconds(item.data(0, Qt.UserRole)), only)
        return True

    def only_watched(self):
        """Plays while the player is watched, from the first time; False if
        they aren't."""
        player = self.player.currentData()
        if not player or player == SHOWN:
            QMessageBox.information(self, "Only while watched", "Choose a player first.")
            return False
        watched = (self.index or {}).get("watched", [])
        start = next((w["time"] for w in watched if same_player(w["name"], player)), None)
        if start is None:
            QMessageBox.information(self, "Only while watched", player + " isn't watched in this demo.")
            return False
        self.game.send("demoseek " + seconds(start), "demoonly watched " + quoted(player))
        return True

    # recording

    def record(self):
        """Record…: a stretch of the demo playing, its kills (by the player
        chosen), those of whoever is shown, or while a player is watched."""
        demo = self.state.get("demo")
        if not demo:
            QMessageBox.information(self, "Record a video", "Play a demo first.")
            return
        player = self.player.currentData() or ""
        index = self.index or {}
        recorder = (index.get("recorder") or {}).get("name", "")
        keys = ["kills", "shownkills", "watched"]
        # whoever is shown in the Player menu: their kills are the frags shown
        if player == SHOWN:
            player, keys = "", keys[1:]
        elif recorder and not same_player(player, recorder):
            keys = ["kills", "recorderkills", "shownkills", "watched"]
        by = " by " + player if player else ""
        texts = {"kills": ("kill" + by, "kills" + by), "recorderkills": RECORDER_KILLS, "shownkills": SHOWN_KILLS}
        # whose each choice plays
        whose = {key: recorder if key in RECORDER_ONLY else "" if key.startswith("shown") else player for key in keys}

        def plans_for(before, after):
            """{key: (count, spans)} with that lead-in and out."""
            return {key: (demoonly_plan(index, key, player) if player else (0, [])) if key == "watched"
                    else demoonly_plan(index, RECORDER_ONLY.get(key, key), whose[key], before, after)
                    for key in keys}

        def choices(plans):
            listed = []
            for key in keys:
                enabled = bool(plans[key][1]) if key == "watched" else plans[key][0] > 0
                listed.append((key, labels[key] + (", " + about(length(plans[key][1])) if enabled else ""), enabled))
            return listed

        plans = plans_for(*self.get_rec_kill_times())
        labels = {key: the(plans[key][0], *texts[key]) + (SHOWN_WHO if key == "shownkills" else "")
                  for key in keys if key != "watched"}
        labels["watched"] = f"While {player} is watched" if player else "While a player is watched"
        dialog = RecordDialog(self.settings, choices(plans), self.state.get("time", 0), parent=self,
                              relabel=lambda before, after: choices(plans_for(before, after)))
        if not dialog.exec():
            return
        what = dialog.what()
        if what == "range":
            start, end = dialog.times()
            label = f"{demo}, {clock(start)} to {clock(end)}"
            clip = range_clip(demo, start, end)
        else:
            label = demo + ", " + labels[what][0].lower() + labels[what][1:]
            player = whose[what]
            clip = only_clip(demo, RECORDER_ONLY.get(what, what), player, plans_for(*dialog.kill_times())[what][1])
        self.add_job(dialog.job(label, [clip], demo, player))

    def record_selected(self, tree):
        """The kills selected, in one video or one each."""
        demo = self.state.get("demo")
        items = tree.selectedItems()
        if not demo or not items:
            QMessageBox.information(self, "Record a video", "Select kills of the demo playing first.")
            return
        one = "kill"
        kind = f"{len(items)} {one}s" if len(items) > 1 else one
        player = self.player.currentData() or ""
        # whoever is shown too: their kills only while they're shown
        kills = self.player_kills(player)
        player = "" if player == SHOWN else player
        index = self.index or {}
        chains = [[index["kills"][i] for i in item.data(2, Qt.UserRole)] for item in items]

        def spans_for(before, after):
            """The clips of the kills selected, and of the team kills near
            them, with that lead-in and out."""
            spans = {id(k): kill_span(index, k, start, end, before, after) for k, start, end in kills.values()}
            return stretches([spans[id(k)] for chain in chains for k in chain if id(k) in spans]
                             + [spans[id(k)] for chain in chains
                                for k, _, _ in team_kills_with(list(kills.values()), chain)], self.switches())

        def choices(spans):
            return [("selected", f"{the(len(items), one, one + 's')} selected, {about(length(spans))}", True)]

        spans = spans_for(*self.get_rec_kill_times())
        dialog = RecordDialog(self.settings, choices(spans), join=len(spans) > 1, parent=self,
                              relabel=lambda before, after: choices(spans_for(before, after)))
        if not dialog.exec():
            return
        clips = [range_clip(demo, start, end) for start, end in spans_for(*dialog.kill_times())]
        if dialog.join.isChecked() or len(clips) == 1:
            self.add_job(dialog.job(f"{demo}, {kind}", clips, demo, player))
        else:
            for clip in clips:
                self.add_job(dialog.job(f"{demo}, {clock(clip['start'])} to {clock(clip['end'])}", [clip], demo, player))

    def record_demos(self):
        """The demos selected, all of them, the kills of the player found by
        the filter, or those of whoever is shown, in one video or one each."""
        names = self.demos_of(self.demos.selectedItems())
        if not names:
            QMessageBox.information(self, "Record a video", "Select demos first.")
            return
        # the player found in each demo, the one with the most kills
        found = {}
        if not self.demos.isColumnHidden(COL_PLAYER):
            found = {name: self.demo_items[name].data(COL_PLAYER, Qt.UserRole) for name in names}
        player = next((p for p in found.values() if p), "")

        indexes = {name: self.library.index(name) for name in names}
        # whose kills each choice plays in each demo
        recorders = {name: (index.get("recorder") or {}).get("name", "") for name, index in indexes.items() if index}
        keys = ("kills", "recorderkills", "shownkills", "everything")

        def plans_for(before, after):
            """What each choice records of each demo with that lead-in and
            out, from the indexes kept: {key: {name: (count, spans or None)}},
            the demos with some."""
            plans = {}
            for key in keys:
                plans[key] = {}
                for name, index in indexes.items():
                    if index is None:
                        if key == "everything":
                            plans[key][name] = (0, None)
                        continue
                    if key in RECORDER_ONLY:
                        plan = (demoonly_plan(index, RECORDER_ONLY[key], recorders[name], before, after)
                                if recorders[name] else (0, []))
                    else:
                        plan = demoonly_plan(index, "" if key == "everything" else key,
                                             found.get(name) or "" if key == "kills" else "",
                                             before, after)
                    if plan[0] or key == "everything":
                        plans[key][name] = plan
            return plans

        def choices(plans):
            def text(key, label):
                """label, in how many demos, how long."""
                spans = [plan[1] for plan in plans[key].values()]
                if len(names) > 1 and key != "everything":
                    label += f", in {len(spans)} of the {len(names)} demos"
                if spans and None not in spans:
                    label += ", " + about(sum(length(s) for s in spans))
                return key, label, bool(spans)
            count = {key: sum(plan[0] for plan in plans[key].values()) for key in plans}
            by = " by " + player if player else ""
            return [
                text("kills", the(count["kills"], "kill" + by, "kills" + by)),
                text("recorderkills", the(count["recorderkills"], *RECORDER_KILLS)),
                text("shownkills", the(count["shownkills"], *SHOWN_KILLS) + SHOWN_WHO),
                text("everything", "All of the demo" if len(names) == 1 else f"All of the {len(names)} demos"),
            ]

        dialog = RecordDialog(self.settings, choices(plans_for(*self.get_rec_kill_times())), join=len(names) > 1,
                              parent=self, relabel=lambda before, after: choices(plans_for(before, after)))
        if not dialog.exec():
            return
        what = dialog.what()
        plans = plans_for(*dialog.kill_times())
        chosen = list(plans[what])
        whose = player + "'s" if player else "all the"
        kind = {"kills": f"{whose} kills", "recorderkills": "recorder frags", "shownkills": SHOWN_KILLS[1],
                "everything": ""}[what]
        # no player for whoever is shown
        if what.startswith("shown"):
            found, player = {}, ""
        elif what in RECORDER_ONLY:
            found = recorders
            player = found.get(chosen[0], "") if len(chosen) == 1 else ""
        only = "" if what == "everything" else RECORDER_ONLY.get(what, what)
        clips = [only_clip(name, only, found.get(name) or "", plans[what][name][1]) for name in chosen]
        if dialog.join.isChecked() or len(clips) == 1:
            demo = chosen[0] if len(chosen) == 1 else f"{len(chosen)} demos"
            self.add_job(dialog.job(demo + (f", {kind}" if kind else ""), clips, demo, player))
        else:
            for name, clip in zip(chosen, clips):
                label = name + (f", {kind}" if kind else "")
                self.add_job(dialog.job(label, [clip], name, found.get(name) or player))

    def add_job(self, job):
        """Records it after the ones before it."""
        job["item"] = item = QTreeWidgetItem([os.path.basename(job["output"]), job["label"], "Waiting"])
        self.jobs.append(job)
        self.queue.addTopLevelItem(item)
        if self.queue_dock.isHidden():
            self.queue_dock.show()
            self.resizeDocks([self.queue_dock], [160], Qt.Vertical)
        self.next_job()

    def next_job(self):
        if self.recorder:
            return
        job = next((j for j in self.jobs if j["status"] == "waiting"), None)
        if job:
            job["status"] = "recording"
            self.recorder = Recorder(self, job, self.job_progress, self.job_done)
            self.recorder.start()

    def job_progress(self, job, text):
        job["item"].setText(2, text)

    def job_done(self, job, error, video):
        self.recorder = None
        item = job["item"]
        if video:
            job["status"], job["output"] = "done", video
            item.setText(0, os.path.basename(video))
            item.setText(2, "Done")
            item.setToolTip(0, video)
            self.statusBar().showMessage("Recorded " + video)
        elif error == "canceled":
            job["status"] = "canceled"
            item.setText(2, "Canceled")
        else:
            job["status"] = "failed"
            item.setText(2, "Failed: " + error.splitlines()[0])
            item.setToolTip(2, error)
            item.setForeground(2, QColor(Qt.red))
            self.statusBar().showMessage(f"Couldn't record {item.text(0)}: {error.splitlines()[0]}")
        self.next_job()

    def cancel_jobs(self):
        for job in [j for j in self.jobs if j["item"].isSelected()]:
            if job["status"] == "waiting":
                job["status"] = "canceled"
                job["item"].setText(2, "Canceled")
            elif job["status"] == "recording" and self.recorder:
                self.recorder.cancel()

    def remove_finished(self):
        for job in [j for j in self.jobs if j["status"] in ("done", "failed", "canceled")]:
            self.jobs.remove(job)
            self.queue.takeTopLevelItem(self.queue.indexOfTopLevelItem(job["item"]))

    def open_video(self, item):
        job = next((j for j in self.jobs if j["item"] is item), None)
        if job and job["status"] == "done":
            QDesktopServices.openUrl(QUrl.fromLocalFile(job["output"]))

    def open_folder(self):
        job = next((j for j in self.jobs if j["item"].isSelected()), None)
        folder = os.path.dirname(job["output"]) if job else self.settings.value("rec/folder", "")
        if folder and os.path.isdir(folder):
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def closeEvent(self, event):
        left = sum(1 for j in self.jobs if j["status"] in ("waiting", "recording"))
        if left and QMessageBox.question(
                self, "Quit", f"{left} video{'s are' if left > 1 else ' is'} still to record. Quit anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            event.ignore()
            return
        self.poller.stop()
        self.library_opener.stop()
        self.library.stop()
        for job in self.jobs:
            if job["status"] == "waiting":
                job["status"] = "canceled"
        if self.recorder:
            self.recorder.cancel()
        self.game.stop()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description="Rewatch Medal of Honor: Allied Assault demos.")
    parser.add_argument("--exe", help="the openmohaa program")
    parser.add_argument("--game", help="the folder with the game files (main/Pak0.pk3)")
    parser.add_argument("--demos", help="the folder with the demos (.dm3)")
    parser.add_argument("demo", nargs="?", help="a demo to play: its path in the demos folder, without .dm3")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("mohdemo-replay")
    settings = QSettings("mohdemo-replay", "mohdemo-replay")
    for key in ("exe", "game", "demos"):
        if getattr(args, key):
            settings.setValue(key, os.path.abspath(getattr(args, key)))
    if not settings.value("exe"):
        built = [os.path.join(REPO, ".cmake", "RelWithDebInfo", name)
                 for name in ("openmohaa", os.path.join("openmohaa.app", "Contents", "MacOS", "openmohaa"))]
        settings.setValue("exe", next((p for p in built if os.path.isfile(p)), shutil.which("openmohaa") or ""))
    settings.setValue("exe", game_program(settings.value("exe")))

    window = Window(settings)
    window.show()
    if not window.ready():
        window.edit_settings()
    elif args.demo:
        window.play_demo(args.demo)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
