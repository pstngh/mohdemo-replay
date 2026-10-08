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

from PySide6.QtCore import QDateTime, QElapsedTimer, QProcess, QProcessEnvironment, QSettings, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QSlider, QSplitter, QStyle, QTabWidget, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
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


def clock(msec):
    seconds = max(0, msec) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def seconds(msec):
    return f"{max(0, msec) / 1000:.3f}"


def quoted(name):
    return '"' + name.replace('"', "'") + '"'


def plain(name):
    """A name without its ^ color codes, as the game shows it."""
    out, i = [], 0
    while i < len(name):
        if name[i] == "^" and i + 1 < len(name) and name[i + 1] != "^":
            i += 2
            continue
        out.append(name[i])
        i += 1
    return "".join(out)


class Game:
    """The game process, its throwaway home folder and its command pipe."""

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

    def start(self, exe, game, demos, width, height):
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
                     "+set", "cl_skipintro", "1", "+exec", "replay.cfg"]
        # Wayland first, then XWayland if it doesn't start
        self.driver = os.environ.get("SDL_VIDEODRIVER") or ("wayland" if sys.platform.startswith("linux") else "")
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
        if quick and self.driver == "wayland" and not os.environ.get("SDL_VIDEODRIVER"):
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
            if not self.process.waitForFinished(3000):
                self.process.terminate()
                if not self.process.waitForFinished(2000):
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


class Window(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.game = Game(self.game_output, self.game_exited)
        self.state = {}
        self.index = None
        self.pending_demo = None
        self.seeking_slider = False

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
        only_kills.setToolTip("Play only the kills listed")
        only_kills.clicked.connect(self.only_kills)
        follow = QPushButton("Only while watched")
        follow.setToolTip("Play only while this player is shown")
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
        self.kills.itemActivated.connect(self.jump_to_item)
        self.rounds.itemActivated.connect(self.jump_to_item)
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
        self.slider = QSlider(Qt.Horizontal)
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
            mode = "only the kills" + (" by " + plain(self.state["player"]) if self.state.get("player") else "")
        elif only == "watched":
            mode = "only while " + plain(self.state.get("player", "")) + " is watched"
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
            recorder = plain(self.index["recorder"]["name"])
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
        for name in sorted(names, key=lambda n: plain(n).lower()):
            self.player.addItem(plain(name), name)
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
        self.fill_kills()

    def fill_kills(self):
        player = self.player.currentData() or ""
        self.kills.clear()
        for kill in (self.index or {}).get("kills", []):
            if player and plain(player).lower() != plain(kill["killerName"]).lower():
                continue
            killer = plain(kill["killerName"]) if kill["killerName"] else ""
            victim = plain(kill["victimName"])
            how = plain(kill["text"])
            item = QTreeWidgetItem([clock(kill["time"]), killer, victim, how])
            item.setData(0, Qt.UserRole, kill["time"] - KILL_BEFORE)
            self.kills.addTopLevelItem(item)

    # actions

    def jump_to_item(self, item):
        self.game.send("demoseek " + seconds(item.data(0, Qt.UserRole)))

    def slider_released(self):
        self.seeking_slider = False
        self.game.send("demoseek " + seconds(self.slider.value()))

    def only_kills(self):
        player = self.player.currentData() or ""
        self.game.send("demoonly kills" + (" " + quoted(player) if player else ""))

    def only_watched(self):
        player = self.player.currentData()
        if not player:
            QMessageBox.information(self, "Only while watched", "Choose a player first.")
            return
        self.game.send("demoonly watched " + quoted(player))

    def closeEvent(self, event):
        self.poller.stop()
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
