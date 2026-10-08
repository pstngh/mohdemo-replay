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
import html
import json
import os
import shutil
import sys
import tempfile
from time import monotonic

from PySide6.QtCore import (
    QDateTime, QElapsedTimer, QEvent, QObject, QProcess, QProcessEnvironment, QSettings, Qt, QTimer,
)
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressDialog, QProxyStyle, QPushButton, QRadioButton, QSlider, QSplitter, QStyle,
    QStyleOptionSlider, QTabWidget, QToolButton, QToolTip, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

PIPE = "replay_pipe"
KILL_BEFORE = 4000  # msec of a kill shown before it, as cl_demoKillBefore
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


def same_player(a, b):
    """The engine's rule for names: without ^ and a letter or digit, only
    printable ASCII, any case (Q_CleanStr)."""
    def clean(name):
        out, i = [], 0
        while i < len(name):
            if name[i] == "^" and i + 1 < len(name) and name[i + 1].isascii() and name[i + 1].isalnum():
                i += 2
                continue
            if " " <= name[i] <= "~":
                out.append(name[i])
            i += 1
        return "".join(out).lower()
    return clean(a) == clean(b)


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
                QToolTip.showText(event.globalPos(), text, self)
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


class RecordDialog(QDialog):
    """What to record and how, kept in the settings."""

    def __init__(self, settings, time, player, kills, watched, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Record a video")
        self.settings = settings
        self.player = player
        form = QFormLayout(self)

        self.range = QRadioButton("From")
        self.start = QLineEdit(clock(time))
        self.end = QLineEdit(clock(time + 30000))
        row = QHBoxLayout()
        for widget in (self.range, self.start, QLabel("to"), self.end):
            row.addWidget(widget)
        self.kills = QRadioButton(f"The {kills} kills" + (" by " + player if player else ""))
        self.kills.setEnabled(kills > 0)
        self.watched = QRadioButton("While " + player + " is watched" if player else "While a player is watched")
        self.watched.setEnabled(bool(player) and watched)
        group = QButtonGroup(self)
        for button in (self.range, self.kills, self.watched):
            group.addButton(button)
        self.range.setChecked(True)
        what = QVBoxLayout()
        what.addLayout(row)
        what.addWidget(self.kills)
        what.addWidget(self.watched)
        form.addRow("Record", what)

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

    def accept(self):
        if self.range.isChecked():
            start, end = parse_clock(self.start.text()), parse_clock(self.end.text())
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
        super().accept()

    def job(self, demo):
        """What the recorder does, from the settings."""
        crf = self.quality.value()
        options = self.advanced.text().strip() or " ".join([
            CODECS[self.codec.currentText()].format(crf=crf, quality=max(1, min(100, 120 - 3 * crf))),
            f"-c:a aac -b:a {self.bitrate.currentText()}" if self.sound.isChecked() else "-an",
            "-movflags +faststart"])
        width, height = (int(v) for v in self.size.currentText().lower().split("x"))
        if self.range.isChecked():
            start, end = parse_clock(self.start.text()), parse_clock(self.end.text())
            commands = [f"demoseek {seconds(start)}", f"demovideo replay {seconds(end)}"]
        else:
            start, end = 0, None
            only = f"kills {quoted(self.player)}" if self.kills.isChecked() and self.player else (
                "kills" if self.kills.isChecked() else f"watched {quoted(self.player)}")
            commands = ["demoseek 0", "demoonly " + only, "demovideo replay"]
        try:
            name = self.pattern.text().format_map({
                "demo": demo, "start": clock(start).replace(":", "-"), "player": self.player or "",
                "date": QDateTime.currentDateTime().toString("yyyy-MM-dd hh-mm")})
        except (KeyError, ValueError, IndexError):
            name = demo
        name = "".join(c for c in name if c not in '/\\:*?"<>|').strip() or demo
        return {"options": options, "width": width, "height": height, "fps": self.fps.currentText(),
                "sound": self.sound.isChecked(), "ffmpeg": self.ffmpeg.text().strip() or "ffmpeg",
                "commands": commands, "start": start, "end": end,
                "output": os.path.join(self.folder.text(), name + ".mp4")}


class Recording(QObject):
    """A video recorded by a second game, offscreen, rendering the sound."""

    def __init__(self, window, demo, job):
        super().__init__(window)
        self.window = window
        self.demo = demo
        self.job = job
        self.phase = "loading"
        self.error = ""
        self.game = Game(self.output, self.exited)
        self.clock = QElapsedTimer()
        self.timer = QTimer(self, interval=200, timeout=self.poll)
        self.progress = QProgressDialog("Starting the recorder…", "Cancel", 0, 0, window)
        self.progress.setWindowTitle("Recording")
        self.progress.setMinimumDuration(0)
        self.progress.canceled.connect(self.cancel)

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
        self.game.send("demo " + quoted(self.demo))
        self.clock.start()
        self.timer.start()

    def output(self, line):
        if line.startswith("ERROR:") or "Couldn't write" in line or "Couldn't run" in line:
            self.error = line.replace("^1", "")

    def poll(self):
        # looked at first: the game says it records before FFmpeg makes the
        # file, and that it's done once FFmpeg has quit
        made = os.path.isfile(self.made())
        state = self.game.read_json("demostate.json") or {}
        if self.phase == "loading":
            if state.get("demo") and state.get("time", 0) > 0 and not state.get("seeking"):
                for command in self.job["commands"]:
                    self.game.send(command)
                self.phase = "starting"
            elif self.clock.elapsed() > 120000:
                self.finish("The recorder didn't load the demo. " + self.error)
        elif self.phase == "starting":
            if state.get("recording"):
                self.phase = "recording"
            elif made:
                self.finish()  # over between two polls
            elif self.clock.elapsed() > 180000 or self.error:
                self.finish("The recording didn't start. " + self.error)
        elif self.phase == "recording":
            start, end, time = self.job["start"], self.job["end"], state.get("time", 0)
            if end:
                self.progress.setMaximum(100)
                self.progress.setValue(max(0, min(99, (time - start) * 100 // max(1, end - start))))
            self.progress.setLabelText(f"Recording {clock(time)}…")
            if not state.get("recording"):
                self.finish()

    def made(self):
        """The video the recorder writes."""
        return os.path.join(self.game.home or "", "main", "videos", "replay.mp4")

    def finish(self, error=""):
        self.timer.stop()
        made = self.made()
        log = made + ".log"
        if not error and self.error:
            # FFmpeg quit midway: what it wrote isn't the whole video
            error = "The recording stopped. " + self.error
        if not error and not os.path.isfile(made):
            error = "FFmpeg didn't write the video."
        if error and os.path.isfile(log):
            with open(log, errors="replace") as f:
                error += "\n" + f.read()[-1500:]
        output = self.job["output"]
        if not error:
            os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
            base, number = output[:-4], 2
            while os.path.exists(output):
                output = f"{base} ({number}).mp4"
                number += 1
            shutil.move(made, output)
        self.phase = "done"
        self.progress.close()
        self.game.stop()
        if error:
            QMessageBox.warning(self.window, "Recording", error.strip())
        else:
            self.window.statusBar().showMessage("Recorded " + output)
        self.window.recording = None

    def cancel(self):
        if self.phase != "done":
            self.phase = "done"
            self.timer.stop()
            self.game.stop()
            self.window.statusBar().showMessage("Recording canceled")
            self.window.recording = None

    def exited(self):
        if self.phase not in ("done",):
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
        self.recording = None
        self.last_jump = (None, 0)
        self.round_marks = []

        self.setWindowTitle("MoH Demo Replay")
        self.resize(1000, 640)
        style = self.style()

        # demos
        self.filter = QLineEdit(placeholderText="Filter demos")
        self.filter.textChanged.connect(self.fill_demos)
        self.demos = self.make_list(["Date", "Demo"])
        self.demos.setSortingEnabled(True)
        self.demos.sortByColumn(0, Qt.DescendingOrder)
        self.demos.itemActivated.connect(lambda item: self.play_demo(item.text(1)))
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
        only_kills = QPushButton("Only these kills")
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
        self.rounds = self.make_list(["Time", "Round", "Result"])
        # a click jumps, so does Enter
        for tree in (self.kills, self.rounds):
            tree.itemClicked.connect(self.jump_to_item)
            tree.itemActivated.connect(self.jump_to_item)
        tabs = QTabWidget()
        tabs.addTab(self.kills, "Kills")
        tabs.addTab(self.rounds, "Rounds")
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
        splitter.setSizes([300, 700])

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

        settings_action = self.menuBar().addAction("Settings…")
        settings_action.triggered.connect(self.edit_settings)
        self.restart_action = self.menuBar().addAction("Restart game")
        self.restart_action.triggered.connect(self.start_game)

        self.poller = QTimer(interval=100, timeout=self.poll)
        self.poller.start()
        self.fill_demos()

    def make_list(self, columns):
        tree = QTreeWidget()
        tree.setHeaderLabels(columns)
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        tree.header().setStretchLastSection(True)
        return tree

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
        folder = self.settings.value("demos", "")
        try:
            names = [n for n in os.listdir(folder) if n.lower().endswith(".dm3")]
        except OSError:
            names = []
        words = self.filter.text().lower().split()
        self.demos.clear()
        for name in names:
            if all(w in name.lower() for w in words):
                date = QDateTime.fromSecsSinceEpoch(int(os.path.getmtime(os.path.join(folder, name))))
                self.demos.addTopLevelItem(QTreeWidgetItem([date.toString("yyyy-MM-dd hh:mm"), name[:-4]]))

    def play_demo(self, name):
        self.pending_demo = name
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
        if only == "kills":
            mode = "only the kills" + (" by " + self.state["player"] if self.state.get("player") else "")
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
        self.kills.clear()
        marks = []
        for kill in (self.index or {}).get("kills", []):
            if player and not same_player(player, kill["killerName"]):
                continue
            killer, victim, how = kill["killerName"], kill["victimName"], kill["text"]
            item = QTreeWidgetItem([clock(kill["time"]), killer, victim, how])
            item.setData(0, Qt.UserRole, kill["time"] - KILL_BEFORE)
            self.kills.addTopLevelItem(item)
            marks.append((kill["time"], how))
        self.slider.set_marks(marks, self.round_marks)

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
        """Plays the kills listed, from the one selected or the first."""
        item = self.kills.currentItem() or self.kills.topLevelItem(0)
        if not item:
            QMessageBox.information(self, "Only these kills", "There are no kills to play.")
            return
        player = self.player.currentData() or ""
        self.game.send("demoseek " + seconds(item.data(0, Qt.UserRole)))
        self.game.send("demoonly kills" + (" " + quoted(player) if player else ""))

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

    def record(self):
        demo = self.state.get("demo")
        if not demo:
            QMessageBox.information(self, "Record a video", "Play a demo first.")
            return
        if self.recording:
            QMessageBox.information(self, "Record a video", "A video is already being recorded.")
            return
        player = self.player.currentData() or ""
        index = self.index or {}
        kills = sum(1 for k in index.get("kills", []) if not player or same_player(k["killerName"], player))
        watched = any(same_player(w["name"], player) for w in index.get("watched", [])) if player else False
        dialog = RecordDialog(self.settings, self.state.get("time", 0), player, kills, watched, self)
        if dialog.exec():
            self.recording = Recording(self, demo, dialog.job(demo))
            self.recording.start()

    def closeEvent(self, event):
        self.poller.stop()
        if self.recording:
            self.recording.cancel()
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
