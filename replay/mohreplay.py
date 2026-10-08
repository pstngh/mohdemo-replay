#!/usr/bin/env python3
"""mohdemo-replay: rewatch Medal of Honor: Allied Assault demos with buttons.

Starts the game (OpenMoHAA built from this repository) with a throwaway home
folder, sends it console commands through its pipe (com_pipefile) and shows
what it reports in demoindex.json and demostate.json. Everything the buttons
do is a console command, so it all works with key binds too.

    python3 mohreplay.py [--exe PATH] [--game FOLDER] [--demos FOLDER] [DEMO]
"""

import argparse
import errno
import functools
import hashlib
import html
import json
import os
import shutil
import sys
import tempfile
from time import monotonic

from PySide6.QtCore import (
    QDateTime, QElapsedTimer, QEvent, QFileSystemWatcher, QObject, QProcess, QProcessEnvironment,
    QSettings, QStandardPaths, Qt, QTimer, QUrl,
)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDockWidget, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProxyStyle, QPushButton, QRadioButton, QSlider, QSplitter, QStyle,
    QStyleOptionSlider, QTabWidget, QToolButton, QToolTip, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

PIPE = "replay_pipe"
KILL_BEFORE = 4000  # msec of a kill shown before it, as cl_demoKillBefore
KILL_AFTER = 2000  # and after it, as cl_demoKillAfter
MULTI_KILL_GAP = 3000  # msec at most between a player's kills in a multi-kill, as cl_demoMultiKill
SPEEDS = ["0.25", "0.5", "1", "2", "4"]
# keys above 127 only: the others stop a demo
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


def find_ffmpeg():
    for path in (shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if path and os.path.isfile(path):
            return path
    return "ffmpeg"


@functools.lru_cache(maxsize=4096)
def clean_name(name):
    """The engine's rule for names: without ^ and a letter or digit, only
    printable ASCII, any case (Q_CleanStr)."""
    out, i = [], 0
    while i < len(name):
        if name[i] == "^" and i + 1 < len(name) and name[i + 1].isascii() and name[i + 1].isalnum():
            i += 2
            continue
        if " " <= name[i] <= "~":
            out.append(name[i])
        i += 1
    return "".join(out).lower()


def same_player(a, b):
    return clean_name(a) == clean_name(b)


def multi_kills(kills):
    """The multi-kills, as demoonly multikills plays them: lists of two
    kills or more by a player, each at most MULTI_KILL_GAP after the one
    before, in the order they start."""
    chains, last = [], {}
    for kill in kills:
        if not kill["killerName"]:
            continue
        key = clean_name(kill["killerName"])
        chain = last.get(key)
        if chain and kill["time"] - chain[-1]["time"] <= MULTI_KILL_GAP:
            chain.append(kill)
        else:
            last[key] = chain = [kill]
            chains.append(chain)
    return [chain for chain in chains if len(chain) > 1]


def player_kills(player):
    """"name (kills, multi-kills)" from a summary's [name, kills, multi-kills]."""
    name, kills, multi = player
    return f"{name} ({kills}, {multi} multi)" if multi else f"{name} ({kills})"


def list_demos(folder):
    """{name without .dm3: path} of the demos in folder."""
    try:
        return {n[:-4]: os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".dm3")}
    except OSError:
        return {}


def summarize(index):
    """What the demo list shows of an index: its maps, length and players,
    {clean name: [name, kills, multi-kills]}."""
    players = {}

    def add(name, kills=0):
        if name:
            players.setdefault(clean_name(name), [name, 0, 0])[1] += kills
    add(index.get("recorder", {}).get("name", ""))
    for watched in index.get("watched", []):
        add(watched["name"])
    for kill in index.get("kills", []):
        add(kill["killerName"], 1)
        add(kill["victimName"])
    for chain in multi_kills(index.get("kills", [])):
        players[clean_name(chain[0]["killerName"])][2] += 1
    return {"maps": [m["map"] for m in index.get("maps", [])], "duration": index.get("duration", 0),
            "players": players}


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
        self.tool = os.path.join(os.path.dirname(exe), "mohdemoindex") if exe else None
        if not self.folder or not os.path.isdir(self.folder):
            self.on_change()
            return
        # a folder of indexes for each folder of demos
        key = hashlib.sha1(self.folder.encode()).hexdigest()[:12]
        base = QStandardPaths.writableLocation(QStandardPaths.GenericCacheLocation)
        self.cache = os.path.join(base, "mohdemo-replay", "index", key)
        self.watcher.addPath(self.folder)
        self.rescan()

    def rescan(self):
        """Loads the indexes kept, and indexes the demos without one."""
        if not self.folder or not os.path.isdir(self.folder):
            return
        demos = list_demos(self.folder)
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
        self.waiting = [demos[name] for _, name in sorted(stale, reverse=True)]
        self.total = len(self.waiting) + len(self.busy)
        self.start_workers()
        self.on_change()

    def load(self, name):
        try:
            with open(os.path.join(self.cache, name + ".json"), encoding="utf-8") as f:
                self.demos[name] = summarize(json.load(f))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.demos.pop(name, None)

    def start_workers(self):
        if self.waiting:
            os.makedirs(self.cache, exist_ok=True)
        while self.waiting and len(self.workers) < self.WORKERS:
            batch, self.waiting = self.waiting[:self.BATCH], self.waiting[self.BATCH:]
            names = {os.path.basename(path)[:-4] for path in batch}
            self.busy |= names
            worker = QProcess(self)
            worker.readyReadStandardOutput.connect(lambda w=worker: self.indexed(w))
            worker.finished.connect(lambda *_, w=worker, n=names: self.worker_done(w, n))
            worker.errorOccurred.connect(lambda error, w=worker, n=names:
                                         error == QProcess.FailedToStart and self.worker_done(w, n))
            self.workers.append(worker)
            worker.start(self.tool, [self.cache, *batch])
            try:
                # behind the game
                os.setpriority(os.PRIO_PROCESS, worker.processId(), 10)
            except (OSError, AttributeError):
                pass

    def indexed(self, worker):
        for line in bytes(worker.readAllStandardOutput()).decode("utf-8", "replace").splitlines():
            if line:
                self.load(line)
        self.changed_timer.start()

    def worker_done(self, worker, names):
        if worker not in self.workers:
            return
        self.indexed(worker)
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
    """Sorts by the number kept in a column's UserRole, if any."""

    def __lt__(self, other):
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        mine, theirs = self.data(column, Qt.UserRole), other.data(column, Qt.UserRole)
        if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
            return mine < theirs
        return self.text(column).lower() < other.text(column).lower()


class Game:
    """The game process, its throwaway home folder and its command pipe."""

    QUIT_WAIT = 3000  # msec given to quit, then to SIGTERM
    TERM_WAIT = 2000

    def __init__(self, on_output, on_exit):
        self.on_output = on_output
        self.on_exit = on_exit
        self.process = None
        self.home = None
        self.queue = []
        self.driver = None
        self.args = None
        self.started = QElapsedTimer()
        self.flusher = QTimer(interval=100, timeout=self.flush)

    def running(self):
        return self.process is not None and self.process.state() != QProcess.NotRunning

    def start(self, exe, game, demos, width, height, extra=(), driver=None):
        self.cleanup()
        self.home = tempfile.mkdtemp(prefix="mohreplay-")
        main = os.path.join(self.home, "main")
        os.makedirs(main)
        os.symlink(os.path.abspath(demos), os.path.join(main, "demos"))
        with open(os.path.join(main, "replay.cfg"), "w") as f:
            f.writelines(f'bind {key} "{command}"\n' for key, command in BINDS.items())

        self.args = [exe, "+set", "fs_basepath", game, "+set", "fs_homepath", self.home,
                     "+set", "com_pipefile", PIPE, "+set", "cl_demoFiles", "1",
                     "+set", "r_swapInterval", "1", "+set", "com_maxfps", "60",
                     "+set", "r_fullscreen", "0", "+set", "r_mode", "-1",
                     "+set", "r_customwidth", str(width), "+set", "r_customheight", str(height),
                     "+set", "cl_skipintro", "1", "+exec", "replay.cfg", *extra]
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
        self.on_exit()

    def send(self, command):
        if self.running():
            self.queue.append(command)
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

    def cleanup(self):
        if self.home:
            demos = os.path.join(self.home, "main", "demos")
            if os.path.islink(demos):
                os.unlink(demos)
            shutil.rmtree(self.home, ignore_errors=True)
            self.home = None


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


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings
        form = QFormLayout(self)
        self.fields = {}
        for key, label, folder in (("exe", "Game program (openmohaa)", False),
                                   ("game", "Game files folder", True),
                                   ("demos", "Demos folder", True)):
            edit = QLineEdit(settings.value(key, ""))
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _=False, e=edit, d=folder: self.browse(e, d))
            row = QHBoxLayout()
            row.addWidget(edit)
            row.addWidget(browse)
            form.addRow(label, row)
            self.fields[key] = edit
        self.size = QComboBox()
        self.size.addItems(["1280x720", "1600x900", "1920x1080", "2560x1440"])
        self.size.setEditable(True)
        self.size.setCurrentText(settings.value("size", "1280x720"))
        form.addRow("Game window size", self.size)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def browse(self, edit, folder):
        if folder:
            path = QFileDialog.getExistingDirectory(self, "Choose a folder", edit.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Choose the game program", edit.text())
        if path:
            edit.setText(path)

    def accept(self):
        for key, edit in self.fields.items():
            self.settings.setValue(key, edit.text().strip())
        self.settings.setValue("size", self.size.currentText().strip())
        super().accept()


def range_clip(demo, start, end):
    """A clip of demo from start to end, in msec. {part} in the commands is
    the name of the video the recorder writes."""
    return {"demo": demo, "start": start, "end": end,
            "commands": ["demoonly", f"demoseek {seconds(start)}", "demopause 0", f"demovideo {{part}} {seconds(end)}"]}


def only_clip(demo, only="", player=""):
    """A clip of what demoonly plays ("kills", "multikills", "watched"), or of
    all of demo."""
    only = f"demoonly {only}" + (" " + quoted(player) if player else "") if only else "demoonly"
    return {"demo": demo, "start": 0, "end": None,
            "commands": ["demoseek 0", "demopause 0", only, "demovideo {part}"]}


def stretches(spans):
    """(start, end) spans in msec, joined when less than a second apart, as
    demoonly does."""
    joined = []
    for start, end in sorted(spans):
        if joined and start <= joined[-1][1] + 1000:
            joined[-1][1] = max(joined[-1][1], end)
        else:
            joined.append([max(0, start), end])
    return joined


class RecordDialog(QDialog):
    """What to record and how, kept in the settings.

    choices: [(key, label, enabled)], what can be recorded, after a stretch
    of time from time when it isn't None. join: offer one video for all."""

    def __init__(self, settings, choices, time=None, join=False, parent=None):
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
            button.setEnabled(enabled)
            group.addButton(button)
            what.addWidget(button)
        first = self.range or next((b for b in self.choices.values() if b.isEnabled()), None)
        if first:
            first.setChecked(True)
        form.addRow("Record", what)
        self.join = QCheckBox("One video for all")
        self.join.setChecked(settings.value("rec/join", "true") == "true")
        self.offers_join = join
        if join:
            form.addRow("", self.join)
        else:
            self.join.hide()

        self.folder = QLineEdit(settings.value("rec/folder", os.path.expanduser("~/Videos")))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        row = QHBoxLayout()
        row.addWidget(self.folder)
        row.addWidget(browse)
        form.addRow("Folder", row)
        self.pattern = QLineEdit(settings.value("rec/pattern", "{demo} {start}"))
        self.pattern.setToolTip("{demo}, {start}, {player} and {date} are replaced")
        form.addRow("File name", self.pattern)

        self.size = QComboBox()
        self.size.addItems(["1280x720", "1920x1080", "2560x1440", "3840x2160"])
        self.size.setEditable(True)
        self.size.setCurrentText(settings.value("rec/size", "1920x1080"))
        form.addRow("Size", self.size)
        self.fps = QComboBox()
        self.fps.addItems(["30", "60", "120"])
        self.fps.setCurrentText(settings.value("rec/fps", "60"))
        form.addRow("Frames per second", self.fps)
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
        row.addWidget(self.bitrate, 1)
        form.addRow("Audio", row)
        self.advanced = QLineEdit(settings.value("rec/advanced", ""))
        self.advanced.setPlaceholderText("FFmpeg output options, instead of the ones above")
        form.addRow("Advanced", self.advanced)
        self.ffmpeg = QLineEdit(settings.value("rec/ffmpeg", "") or find_ffmpeg())
        form.addRow("FFmpeg", self.ffmpeg)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.addButton("Record", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose a folder", self.folder.text())
        if path:
            self.folder.setText(path)

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
        try:
            width, height = (int(v) for v in self.size.currentText().lower().split("x"))
        except ValueError:
            QMessageBox.warning(self, "Record a video", "The size should be like 1920x1080.")
            return
        for key, value in (("folder", self.folder.text()), ("pattern", self.pattern.text()),
                           ("size", f"{width}x{height}"), ("fps", self.fps.currentText()),
                           ("crf", self.quality.value()), ("codec", self.codec.currentText()),
                           ("sound", "true" if self.sound.isChecked() else "false"),
                           ("bitrate", self.bitrate.currentText()), ("advanced", self.advanced.text().strip()),
                           ("ffmpeg", self.ffmpeg.text().strip())):
            self.settings.setValue("rec/" + key, value)
        if self.offers_join:
            self.settings.setValue("rec/join", "true" if self.join.isChecked() else "false")
        super().accept()

    def job(self, label, clips, demo, player=""):
        """A video of clips for the recorder, with the settings chosen; demo
        and player name it."""
        crf = self.quality.value()
        options = self.advanced.text().strip() or " ".join([
            CODECS[self.codec.currentText()].format(crf=crf, quality=max(1, min(100, 120 - 3 * crf))),
            f"-c:a aac -b:a {self.bitrate.currentText()}" if self.sound.isChecked() else "-an",
            "-movflags +faststart"])
        width, height = (int(v) for v in self.size.currentText().lower().split("x"))
        try:
            name = self.pattern.text().format_map({
                "demo": demo, "start": clock(clips[0]["start"]).replace(":", "-"), "player": player or "",
                "date": QDateTime.currentDateTime().toString("yyyy-MM-dd hh-mm")})
        except (KeyError, ValueError, IndexError):
            name = demo
        name = "".join(c for c in name if c not in '/\\:*?"<>|').strip() or demo
        return {"label": label, "clips": clips, "options": options, "width": width, "height": height,
                "fps": self.fps.currentText(), "sound": self.sound.isChecked(),
                "ffmpeg": self.ffmpeg.text().strip() or "ffmpeg",
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
        self.game = Game(self.output, self.exited)
        self.clock = QElapsedTimer()
        self.timer = QTimer(self, interval=200, timeout=self.poll)

    def start(self):
        settings = self.window.settings
        job = self.job
        extra = ["+set", "s_loopback", "1" if job["sound"] else "0", "+set", "s_khz", "44",
                 "+set", "s_volume", "1", "+set", "cl_aviFrameRate", job["fps"],
                 "+set", "cl_aviFFmpeg", job["ffmpeg"], "+set", "cl_aviPipeFormat", job["options"],
                 "+set", "com_maxfps", "0", "+set", "r_swapInterval", "0"]
        # offscreen where SDL can, the recorder doesn't need a window
        driver = "offscreen" if sys.platform.startswith("linux") else None
        self.game.start(settings.value("exe"), settings.value("game"), settings.value("demos"),
                        job["width"], job["height"], extra, driver)
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
        return os.path.join(self.game.home or "", "main", "videos", self.part() + ".mp4")

    def next_clip(self):
        self.clip += 1
        self.clock.start()
        if self.clip == len(self.job["clips"]):
            self.join()
            return
        demo = self.job["clips"][self.clip]["demo"]
        if demo != self.loaded:
            self.game.send("demo " + quoted(demo))
            self.loaded = demo
            self.phase = "loading"
        else:
            self.send_clip()

    def send_clip(self):
        for command in self.job["clips"][self.clip]["commands"]:
            self.game.send(command.replace("{part}", self.part()))
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
            clip, time = self.job["clips"][self.clip], state.get("time", 0)
            text = f"Recording {clock(time)}"
            if clip["end"]:
                text += f", {max(0, min(99, (time - clip['start']) * 100 // max(1, clip['end'] - clip['start'])))}%"
            if len(self.job["clips"]) > 1:
                text += f" (clip {self.clip + 1} of {len(self.job['clips'])})"
            self.on_progress(self.job, text + "…")
            if not state.get("recording"):
                self.clip_done()

    def clip_done(self):
        if self.error:
            # FFmpeg quit midway: what it wrote isn't the whole video
            self.finish("The recording stopped. " + self.error)
        elif not os.path.isfile(self.made()):
            self.finish("FFmpeg didn't write the video.")
        else:
            self.parts.append(self.made())
            self.next_clip()

    def join(self):
        self.phase = "joining"
        if len(self.parts) == 1:
            self.finish(video=self.parts[0])
            return
        self.on_progress(self.job, f"Joining {len(self.parts)} clips…")
        folder = os.path.dirname(self.parts[0])
        with open(os.path.join(folder, "parts.txt"), "w") as f:
            f.writelines("file '" + part.replace("'", "'\\''") + "'\n" for part in self.parts)
        self.joiner = QProcess(self)
        self.joiner.setProcessChannelMode(QProcess.MergedChannels)
        self.joiner.finished.connect(self.joined)
        self.joiner.errorOccurred.connect(
            lambda error: error == QProcess.FailedToStart and self.finish("Couldn't run FFmpeg to join the clips."))
        self.joiner.start(self.job["ffmpeg"], ["-v", "error", "-y", "-f", "concat", "-safe", "0",
                                               "-i", os.path.join(folder, "parts.txt"), "-c", "copy",
                                               "-movflags", "+faststart", os.path.join(folder, "joined.mp4")])

    def joined(self, code=0, status=None):
        if self.phase != "joining":
            return
        video = os.path.join(os.path.dirname(self.parts[0]), "joined.mp4")
        if code or not os.path.isfile(video):
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
        self.on_done(self.job, error.strip(), output if video else None)

    def cancel(self):
        if self.phase != "done":
            self.phase = "done"
            self.timer.stop()
            if self.joiner:
                self.joiner.kill()
                self.joiner.waitForFinished(1000)
            self.game.stop()
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
        self.round_marks = self.kill_marks = self.multi_marks = []
        self.library = Library(self.library_changed, self)
        self.demo_items = {}  # name: item
        self.wanted_player = None  # to choose once the demo plays

        self.setWindowTitle("MoH Demo Replay")
        self.resize(1000, 640)
        style = self.style()

        # demos
        self.filter = QLineEdit(placeholderText="Filter demos by name, map or player")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.filter_demos)
        self.demos = self.make_list(["Date", "Length", "Player", "Map", "Demo"], fit=False)
        self.demos.setSortingEnabled(True)
        self.demos.sortByColumn(0, Qt.DescendingOrder)
        self.demos.itemActivated.connect(lambda item: self.play_demo(item.text(4)))
        self.add_menu(self.demos, "Record the demos selected…", self.record_demos)
        left = QWidget()
        box = QVBoxLayout(left)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.filter)
        box.addWidget(self.demos)

        # what's in the demo
        self.info = QLabel("No demo")
        self.info.setWordWrap(True)
        self.player = QComboBox()
        self.player.currentIndexChanged.connect(self.fill_kills)
        self.only_kills_button = only_kills = QPushButton("Only these kills")
        only_kills.setToolTip("Play only the kills listed, from the one selected or the first")
        only_kills.clicked.connect(self.only_kills)
        follow = QPushButton("Only while watched")
        follow.setToolTip("Play only while this player is shown, from the first time")
        follow.clicked.connect(self.only_watched)
        everything = QPushButton("Play everything")
        everything.clicked.connect(lambda: self.game.send("demoonly"))
        players = QHBoxLayout()
        players.addWidget(QLabel("Player"))
        players.addWidget(self.player, 1)
        players.addWidget(only_kills)
        players.addWidget(follow)
        players.addWidget(everything)

        self.kills = self.make_list(["Time", "Killer", "Victim", "How"])
        self.multikills = self.make_list(["Time", "Player", "Kills", "Victims"])
        self.rounds = self.make_list(["Time", "Round", "Result"])
        # a click jumps, so does Enter
        for tree in (self.kills, self.multikills, self.rounds):
            tree.itemClicked.connect(self.jump_to_item)
            tree.itemActivated.connect(self.jump_to_item)
        for tree in (self.kills, self.multikills):
            self.add_menu(tree, "Record the ones selected…", lambda _=False, t=tree: self.record_selected(t))
        self.tabs = tabs = QTabWidget()
        tabs.addTab(self.kills, "Kills")
        tabs.addTab(self.multikills, "Multi-kills")
        tabs.addTab(self.rounds, "Rounds")
        tabs.currentChanged.connect(self.tab_changed)
        right = QWidget()
        box = QVBoxLayout(right)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.info)
        box.addLayout(players)
        box.addWidget(tabs)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 700])

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
        for icon, tip, command in ((QStyle.SP_MediaSkipBackward, "Previous round (PgDn)", "demoprevround"),
                                   (QStyle.SP_MediaSeekBackward, "Previous kill (Down)", "demoprevkill"),
                                   (None, "Back 10 seconds (Left: 5)", "demoskip -10")):
            controls.addWidget(self.make_button(icon, tip, lambda _=False, c=command: self.game.send(c), "-10 s"))
        controls.addWidget(self.play)
        for icon, tip, command in ((None, "Forward 10 seconds (Right: 5)", "demoskip 10"),
                                   (QStyle.SP_MediaSeekForward, "Next kill (Up)", "demonextkill"),
                                   (QStyle.SP_MediaSkipForward, "Next round (PgUp)", "demonextround")):
            controls.addWidget(self.make_button(icon, tip, lambda _=False, c=command: self.game.send(c), "+10 s"))
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time)
        controls.addWidget(self.speed)
        record = QPushButton("Record…")
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

        settings_action = self.menuBar().addAction("Settings…")
        settings_action.triggered.connect(self.edit_settings)
        self.restart_action = self.menuBar().addAction("Restart game")
        self.restart_action.triggered.connect(self.start_game)
        self.menuBar().addAction(self.queue_dock.toggleViewAction())

        self.indexing = QLabel()
        self.statusBar().addPermanentWidget(self.indexing)

        self.poller = QTimer(interval=100, timeout=self.poll)
        self.poller.start()
        self.fill_demos()
        # once the window is up: the indexes kept take a moment to load
        QTimer.singleShot(0, lambda: self.library.open(settings.value("demos", ""), settings.value("exe", "")))

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

    def edit_settings(self):
        if SettingsDialog(self.settings, self).exec():
            self.fill_demos()
            self.library.open(self.settings.value("demos", ""), self.settings.value("exe", ""))
            if self.ready():
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
        self.game.start(self.settings.value("exe"), self.settings.value("game"), self.settings.value("demos"), width, height)
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
        """Lists the demos of the folder, with what the library knows of
        them, keeping the items there."""
        demos = list_demos(self.settings.value("demos", ""))
        for name in [n for n in self.demo_items if n not in demos]:
            item = self.demo_items.pop(name)
            self.demos.takeTopLevelItem(self.demos.indexOfTopLevelItem(item))
        self.demos.setSortingEnabled(False)
        for name, path in demos.items():
            item = self.demo_items.get(name)
            if item is None:
                try:
                    mtime = int(os.path.getmtime(path))
                except OSError:
                    mtime = 0
                date = QDateTime.fromSecsSinceEpoch(mtime).toString("yyyy-MM-dd hh:mm")
                item = self.demo_items[name] = SortItem([date, "", "", "", name])
                self.demos.addTopLevelItem(item)
            summary = self.library.demos.get(name)
            if summary and item.data(1, Qt.UserRole) != summary["duration"]:
                item.setText(1, clock(summary["duration"]))
                item.setData(1, Qt.UserRole, summary["duration"])
                item.setText(3, ", ".join(m.rsplit("/", 1)[-1] for m in summary["maps"]))
                players = sorted(summary["players"].values(), key=lambda p: (-p[1], p[0].lower()))
                tip = (f"<b>{html.escape(', '.join(summary['maps']))}</b> — {clock(summary['duration'])}<br>"
                       + html.escape(", ".join(player_kills(p) for p in players)))
                for column in range(5):
                    item.setToolTip(column, tip)
        self.demos.setSortingEnabled(True)
        self.filter_demos()

    def filter_demos(self):
        """Shows the demos where each word is in the name, a map or a
        player's name, and the players found."""
        words = [w for w in (clean_name(w) for w in self.filter.text().split()) if w]
        found_any = False
        for name, item in self.demo_items.items():
            summary = self.library.demos.get(name) or {}
            players = summary.get("players", {})
            maps = " ".join(summary.get("maps", [])).lower()
            found, shown = {}, True
            for word in words:
                if word in name.lower() or word in maps:
                    continue
                hits = [key for key in players if word in key]
                if not hits:
                    shown = False
                    break
                found.update((key, players[key]) for key in hits)
            item.setHidden(not shown)
            best = sorted(found.values(), key=lambda p: (-p[1], p[0].lower()))
            item.setText(2, ", ".join(player_kills(p) for p in best))
            item.setData(2, Qt.UserRole, best[0][0] if best else None)
            found_any |= shown and bool(best)
        self.demos.setColumnHidden(2, not found_any)
        self.fit_columns(self.demos)

    def library_changed(self):
        self.fill_demos()
        progress = self.library.progress()
        self.indexing.setText(f"Indexing demos: {progress[0]} of {progress[1]}" if progress else "")

    def play_demo(self, name):
        self.pending_demo = name
        # found by a player's name: that player's kills
        item = self.demo_items.get(name)
        self.wanted_player = item.data(2, Qt.UserRole) if item and not self.demos.isColumnHidden(2) else None
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
        elif only == "watched":
            mode = "only while " + self.state.get("player", "") + " is watched"
        else:
            mode = ""
        if not demo:
            self.info.setText("No demo")
            if not self.pending_demo:
                self.index = None
                self.fill_index()
        elif self.index:
            maps = ", ".join(m["map"] for m in self.index["maps"])
            text = f"<b>{html.escape(demo)}</b> — {html.escape(maps)}, {clock(duration)}"
            recorder = self.index["recorder"]["name"]
            text += f", recorded by {html.escape(recorder)}" if recorder else ""
            text += " (truncated)" if self.index.get("truncated") else ""
            text += f"<br>Playing {html.escape(mode)}" if mode else ""
            text += " — paused" if paused else ""
            self.info.setText(text)

    def fill_index(self):
        index = self.index or {}
        names = set()
        for kill in index.get("kills", []):
            names.update(n for n in (kill["killerName"], kill["victimName"]) if n)
        current = self.player.currentData()
        self.player.blockSignals(True)
        self.player.clear()
        self.player.addItem("All players", "")
        for name in sorted(names, key=str.lower):
            self.player.addItem(name, name)
        found = self.player.findData(current)
        if self.wanted_player:
            found = next((i for i in range(1, self.player.count())
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

    def fill_kills(self):
        player = self.player.currentData() or ""
        kills = (self.index or {}).get("kills", [])
        self.kills.clear()
        self.kill_marks = []
        for kill in kills:
            if player and not same_player(player, kill["killerName"]):
                continue
            killer, victim, how = kill["killerName"], kill["victimName"], kill["text"]
            item = QTreeWidgetItem([clock(kill["time"]), killer, victim, how])
            item.setData(0, Qt.UserRole, kill["time"] - KILL_BEFORE)
            item.setData(1, Qt.UserRole, (kill["time"] - KILL_BEFORE, kill["time"] + KILL_AFTER))
            self.kills.addTopLevelItem(item)
            self.kill_marks.append((kill["time"], how))
        self.multikills.clear()
        self.multi_marks = []
        for chain in multi_kills(kills):
            if player and not same_player(player, chain[0]["killerName"]):
                continue
            victims = ", ".join(kill["victimName"] or "?" for kill in chain)
            item = QTreeWidgetItem([clock(chain[0]["time"]), chain[0]["killerName"], str(len(chain)), victims])
            item.setData(0, Qt.UserRole, chain[0]["time"] - KILL_BEFORE)
            item.setData(1, Qt.UserRole, (chain[0]["time"] - KILL_BEFORE, chain[-1]["time"] + KILL_AFTER))
            self.multikills.addTopLevelItem(item)
            self.multi_marks += [(kill["time"], kill["text"]) for kill in chain]
        self.tab_changed()

    def tab_changed(self):
        """The slider marks the kills of the tab, and the button plays them."""
        multi = self.tabs.currentWidget() is self.multikills
        self.slider.set_marks(self.multi_marks if multi else self.kill_marks, self.round_marks)
        self.only_kills_button.setText("Only these multi-kills" if multi else "Only these kills")

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
        """Plays the kills or multi-kills listed, from the one selected or the
        first."""
        multi = self.tabs.currentWidget() is self.multikills
        tree = self.multikills if multi else self.kills
        item = tree.currentItem() or tree.topLevelItem(0)
        if not item:
            QMessageBox.information(self, self.only_kills_button.text(),
                                    f"There are no {'multi-kills' if multi else 'kills'} to play.")
            return
        player = self.player.currentData() or ""
        self.game.send("demoseek " + seconds(item.data(0, Qt.UserRole)))
        self.game.send(f"demoonly {'multikills' if multi else 'kills'}" + (" " + quoted(player) if player else ""))

    def only_watched(self):
        """Plays while the player is watched, from the first time."""
        player = self.player.currentData()
        if not player:
            QMessageBox.information(self, "Only while watched", "Choose a player first.")
            return
        watched = (self.index or {}).get("watched", [])
        start = next((w["time"] for w in watched if same_player(w["name"], player)), None)
        if start is None:
            QMessageBox.information(self, "Only while watched", player + " isn't watched in this demo.")
            return
        self.game.send("demoseek " + seconds(start))
        self.game.send("demoonly watched " + quoted(player))

    # recording

    def record(self):
        """Record…: a stretch of the demo playing, its kills or multi-kills
        (by the player chosen), or while a player is watched."""
        demo = self.state.get("demo")
        if not demo:
            QMessageBox.information(self, "Record a video", "Play a demo first.")
            return
        player = self.player.currentData() or ""
        index = self.index or {}
        kills = sum(1 for k in index.get("kills", []) if not player or same_player(k["killerName"], player))
        multi = sum(1 for c in multi_kills(index.get("kills", [])) if not player or same_player(c[0]["killerName"], player))
        watched = any(same_player(w["name"], player) for w in index.get("watched", [])) if player else False
        by = " by " + player if player else ""
        dialog = RecordDialog(self.settings, [
            ("kills", f"The {kills} kills{by}", kills > 0),
            ("multikills", f"The {multi} multi-kills{by}", multi > 0),
            ("watched", f"While {player} is watched" if player else "While a player is watched", watched),
        ], self.state.get("time", 0), parent=self)
        if not dialog.exec():
            return
        what = dialog.what()
        if what == "range":
            start, end = dialog.times()
            label = f"{demo}, {clock(start)} to {clock(end)}"
            clip = range_clip(demo, start, end)
        else:
            label = demo + ", " + dialog.choices[what].text()[0].lower() + dialog.choices[what].text()[1:]
            clip = only_clip(demo, what, player)
        self.add_job(dialog.job(label, [clip], demo, player))

    def record_selected(self, tree):
        """The kills or multi-kills selected, in one video or one each."""
        demo = self.state.get("demo")
        items = tree.selectedItems()
        if not demo or not items:
            QMessageBox.information(self, "Record a video", "Select kills of the demo playing first.")
            return
        kind = "multi-kills" if tree is self.multikills else "kills"
        spans = stretches(item.data(1, Qt.UserRole) for item in items)
        dialog = RecordDialog(self.settings, [("selected", f"The {len(items)} {kind} selected", True)],
                              join=len(spans) > 1, parent=self)
        if not dialog.exec():
            return
        player = self.player.currentData() or ""
        clips = [range_clip(demo, start, end) for start, end in spans]
        if dialog.join.isChecked() or len(clips) == 1:
            self.add_job(dialog.job(f"{demo}, {len(items)} {kind}", clips, demo, player))
        else:
            for clip in clips:
                self.add_job(dialog.job(f"{demo}, {clock(clip['start'])} to {clock(clip['end'])}", [clip], demo, player))

    def record_demos(self):
        """The demos selected, all of them or the kills or multi-kills of the
        player found by the filter, in one video or one each."""
        names = [item.text(4) for item in self.demos.selectedItems() if not item.isHidden()]
        if not names:
            QMessageBox.information(self, "Record a video", "Select demos first.")
            return
        # the player found in each demo, the one with the most kills
        found = {}
        if not self.demos.isColumnHidden(2):
            found = {name: self.demo_items[name].data(2, Qt.UserRole) for name in names}
        player = next((p for p in found.values() if p), "")

        def demos_with(column):
            """The demos where the player found has kills (1) or multi-kills
            (2), all players' if none."""
            with_some = []
            for name in names:
                players = (self.library.demos.get(name) or {}).get("players", {})
                if found.get(name):
                    counts = players.get(clean_name(found[name]), [0, 0, 0])
                    if counts[column]:
                        with_some.append(name)
                elif any(p[column] for p in players.values()):
                    with_some.append(name)
            return with_some
        with_kills, with_multi = demos_with(1), demos_with(2)
        whose = player + "'s" if player else "All the"

        def where(count):
            return "" if len(names) == 1 else f", in {count} of the {len(names)} demos"
        dialog = RecordDialog(self.settings, [
            ("kills", f"{whose} kills{where(len(with_kills))}", bool(with_kills)),
            ("multikills", f"{whose} multi-kills{where(len(with_multi))}", bool(with_multi)),
            ("everything", "All of the demo" if len(names) == 1 else f"All of the {len(names)} demos", True),
        ], join=len(names) > 1, parent=self)
        if not dialog.exec():
            return
        what = dialog.what()
        chosen = {"kills": with_kills, "multikills": with_multi, "everything": names}[what]
        clips = [only_clip(name, "" if what == "everything" else what, found.get(name) or "") for name in chosen]
        kind = {"kills": "kills", "multikills": "multi-kills", "everything": ""}[what]
        if dialog.join.isChecked() or len(clips) == 1:
            demo = chosen[0] if len(chosen) == 1 else f"{len(chosen)} demos"
            self.add_job(dialog.job(demo + (f", {whose.lower() if not player else whose} {kind}" if kind else ""),
                                    clips, demo, player))
        else:
            for name, clip in zip(chosen, clips):
                label = name + (f", {whose.lower() if not player else whose} {kind}" if kind else "")
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
    parser.add_argument("demo", nargs="?", help="a demo to play, without .dm3")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("mohdemo-replay")
    settings = QSettings("mohdemo-replay", "mohdemo-replay")
    for key in ("exe", "game", "demos"):
        if getattr(args, key):
            settings.setValue(key, os.path.abspath(getattr(args, key)))
    if not settings.value("exe"):
        built = os.path.join(REPO, ".cmake", "RelWithDebInfo", "openmohaa")
        settings.setValue("exe", built if os.path.isfile(built) else shutil.which("openmohaa") or "")

    window = Window(settings)
    window.show()
    if not window.ready():
        window.edit_settings()
    elif args.demo:
        window.play_demo(args.demo)
    else:
        window.start_game()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
