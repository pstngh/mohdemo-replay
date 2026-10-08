#!/usr/bin/env python3
"""Tests for mohreplay.py. A fake game (fakegame.py) stands in for openmohaa,
so they run anywhere, without game files or a screen:

    python3 replay/tests/test_mohreplay.py [-v] [name of a test]

With MOHREPLAY_TEST_GAME (the folder with main/Pak0.pk3) and
MOHREPLAY_TEST_DEMOS (a folder of real demos) set, the RealGame tests also
play and record a real demo with the game built in .cmake/RelWithDebInfo (or
MOHREPLAY_TEST_EXE), without a window; MOHREPLAY_TEST_DEMO picks the demo.
Without PySide6, it exits with 77, which ctest takes as skipped.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    from PySide6.QtCore import QEvent, QPointF, QSettings, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:
    if __name__ == "__main__":
        print("Skipped: PySide6 isn't installed")
        sys.exit(77)
    raise unittest.SkipTest("PySide6 isn't installed")

import mohreplay  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])
FAKEGAME = os.path.join(HERE, "fakegame.py")

DEMO = {
    "duration": 600000,
    "truncated": False,
    "recorder": {"client": 0, "name": "t-"},
    "maps": [{"time": 0, "map": "obj/obj_team1"}],
    "watched": [{"time": 0, "client": 0, "name": "t-"}, {"time": 300000, "client": 1, "name": "^1Phil"}],
    "kills": [
        {"time": 10000, "killer": 0, "killerName": "t-", "victim": 1, "victimName": "^1Phil",
         "text": "Phil was rifled by t-"},
        {"time": 12000, "killer": 0, "killerName": "t-", "victim": 2, "victimName": "Bob",
         "text": "Bob was rifled by t-"},
        {"time": 14500, "killer": 0, "killerName": "t-", "victim": 3, "victimName": "Al",
         "text": "Al was shot by t-"},
        {"time": 200000, "killer": 1, "killerName": "^1Phil", "victim": 0, "victimName": "t-",
         "text": "t- was machine-gunned by Phil"},
        {"time": 250000, "killer": -1, "killerName": "", "victim": 2, "victimName": "Bob",
         "text": "Bob took himself out of commision"},
        {"time": 400000, "killer": 0, "killerName": "t-", "victim": 1, "victimName": "^1Phil",
         "text": "Phil was rifled by t-"},
    ],
    "rounds": [{"time": 195000}, {"time": 420000}],
    "roundEnds": [{"time": 191000, "text": "Axis win!"}, {"time": 416000, "text": "Allies win!"}],
}


def wait_until(condition, timeout=10.0):
    """Runs Qt's events until condition() is true, False after the timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(condition())


class Messages:
    """Takes the place of QMessageBox's static functions, which would wait."""

    def __init__(self):
        self.shown = []

    def __call__(self, parent, title, text, *args, **kwargs):
        self.shown.append((title, text))
        return QMessageBox.Ok


class FakeGameCase(unittest.TestCase):
    """A game folder with openmohaa and mohdemoindex being the fake game, a
    folder of demos and settings of their own."""

    env = {"FAKEGAME_SPEED": "20"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mohreplay-test-")
        self.bin = os.path.join(self.tmp, "bin")
        self.game_dir = os.path.join(self.tmp, "game")
        self.demos = os.path.join(self.tmp, "demos")
        self.videos = os.path.join(self.tmp, "videos")
        for folder in (self.bin, self.game_dir, self.demos):
            os.makedirs(folder)
        self.exe = os.path.join(self.bin, "openmohaa")
        os.symlink(FAKEGAME, self.exe)
        os.symlink(FAKEGAME, os.path.join(self.bin, "mohdemoindex"))
        self.add_demo("first", DEMO)
        self.settings = QSettings(os.path.join(self.tmp, "settings.ini"), QSettings.IniFormat)
        for key, value in (("exe", self.exe), ("game", self.game_dir), ("demos", self.demos),
                           ("rec/folder", self.videos)):
            self.settings.setValue(key, value)
        self.messages = Messages()
        self.patches = [mock.patch.object(QMessageBox, name, self.messages) for name in ("warning", "information")]
        # quitting while videos are recorded
        self.patches.append(mock.patch.object(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes))
        self.patches.append(mock.patch.dict(os.environ, {**self.env, "SDL_VIDEODRIVER": "offscreen"}))
        self.patches.append(mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.tmp, "cache")}))
        for patch in self.patches:
            patch.start()
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
        # what's left to do still runs in the test's folders and settings
        app.processEvents()
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_demo(self, name, index):
        with open(os.path.join(self.demos, name + ".dm3"), "w") as f:
            json.dump(index, f)

    def window(self):
        window = mohreplay.Window(self.settings)
        self.windows.append(window)
        return window

    def commands(self, game):
        try:
            with open(os.path.join(game.home, "main", "commands.log")) as f:
                return f.read().splitlines()
        except (OSError, TypeError):
            return []

    def playing(self, window, name="first"):
        window.play_demo(name)
        self.assertTrue(wait_until(lambda: window.state.get("demo") == name and window.index), "the demo didn't load")


class TestFunctions(unittest.TestCase):
    def test_clock(self):
        self.assertEqual(mohreplay.clock(0), "0:00")
        self.assertEqual(mohreplay.clock(61999), "1:01")
        self.assertEqual(mohreplay.clock(-5), "0:00")
        self.assertEqual(mohreplay.clock(3600000), "60:00")

    def test_seconds(self):
        self.assertEqual(mohreplay.seconds(12345), "12.345")
        self.assertEqual(mohreplay.seconds(-1), "0.000")

    def test_parse_clock(self):
        self.assertEqual(mohreplay.parse_clock("90"), 90000)
        self.assertEqual(mohreplay.parse_clock("1:30"), 90000)
        self.assertEqual(mohreplay.parse_clock(" 1:30.5 "), 90500)
        self.assertEqual(mohreplay.parse_clock("1:00:00"), 3600000)
        for bad in ("", "x", "1:-2", "1:2:3:4", "1::2"):
            self.assertIsNone(mohreplay.parse_clock(bad), bad)

    def test_same_player(self):
        self.assertTrue(mohreplay.same_player("^1Phil", "phil"))
        self.assertTrue(mohreplay.same_player("T-", "t-"))
        self.assertTrue(mohreplay.same_player("a^", "A^"))
        self.assertFalse(mohreplay.same_player("phil", "phill"))

    def test_multi_kills(self):
        def kill(time, killer, victim="x"):
            return {"time": time, "killerName": killer, "victimName": victim}
        kills = [kill(0, "a"), kill(1000, "^1B"), kill(3000, "A"), kill(5000, "b"), kill(6000, "a"),
                 kill(7000, ""), kill(9000, "a"), kill(20000, "c"), kill(23001, "c")]
        chains = mohreplay.multi_kills(kills)
        self.assertEqual([[k["time"] for k in chain] for chain in chains], [[0, 3000, 6000, 9000]])
        kills.insert(3, kill(3900, "b"))
        chains = mohreplay.multi_kills(kills)
        self.assertEqual([[k["time"] for k in chain] for chain in chains], [[0, 3000, 6000, 9000], [1000, 3900, 5000]])

    def test_macos_bundle(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        app = os.path.join(tmp, "openmohaa.app")
        os.makedirs(os.path.join(app, "Contents", "MacOS"))
        exe = os.path.join(app, "Contents", "MacOS", "openmohaa")
        open(exe, "w").close()
        self.assertEqual(mohreplay.game_program(app), exe)
        self.assertEqual(mohreplay.game_program(app + "/"), exe)
        self.assertEqual(mohreplay.game_program(exe), exe)
        # mohdemoindex is built next to the bundle
        self.assertEqual(mohreplay.find_indexer(exe), os.path.join(app, "Contents", "MacOS", "mohdemoindex"))
        open(os.path.join(tmp, "mohdemoindex"), "w").close()
        self.assertEqual(mohreplay.find_indexer(exe), os.path.join(tmp, "mohdemoindex"))
        plain = os.path.join(tmp, "openmohaa")
        self.assertEqual(mohreplay.find_indexer(plain), os.path.join(tmp, "mohdemoindex"))

    def test_quoted(self):
        self.assertEqual(mohreplay.quoted('say "hi"'), "\"say 'hi'\"")


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.slider = mohreplay.Timeline()
        self.slider.resize(600, self.slider.minimumHeight())
        self.slider.setMaximum(600000)
        self.slider.show()
        app.processEvents()

    def tearDown(self):
        self.slider.close()

    def test_positions(self):
        slider = self.slider
        self.assertLess(slider.x_of(0), slider.x_of(300000))
        self.assertLess(slider.x_of(300000), slider.x_of(600000))
        # one pixel is about 1 second here
        self.assertAlmostEqual(slider.value_at(slider.x_of(300000)), 300000, delta=1100)

    def test_marks(self):
        slider = self.slider
        slider.set_marks([(10000, "a"), (11000, "b"), (400000, "c")], [(195000, "Round 2")])
        bottom, top = slider.height() - 2, 1
        self.assertEqual(slider.mark_at(slider.x_of(400000), bottom), "6:40  c")
        self.assertEqual(slider.mark_at(slider.x_of(195000), top), "3:15  Round 2")
        self.assertIsNone(slider.mark_at(slider.x_of(195000), bottom))
        self.assertIsNone(slider.mark_at(slider.x_of(300000), bottom))
        # kills a second apart: both
        self.assertEqual(slider.mark_at(slider.x_of(10500), bottom), "0:10  a\n0:11  b")
        slider.grab()  # paints them

    def click(self, x):
        y = self.slider.height() // 2
        for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            event = QMouseEvent(kind, QPointF(x, y), self.slider.mapToGlobal(QPointF(x, y)),
                                Qt.LeftButton, Qt.LeftButton if kind == QEvent.MouseButtonPress else Qt.NoButton,
                                Qt.NoModifier)
            QApplication.sendEvent(self.slider, event)

    def test_click_jumps_there(self):
        released = []
        self.slider.sliderReleased.connect(lambda: released.append(self.slider.value()))
        self.click(self.slider.x_of(450000))
        self.assertEqual(len(released), 1)
        self.assertAlmostEqual(released[0], 450000, delta=1100)


class TestGame(FakeGameCase):
    def start(self, driver=None):
        self.output, self.exits = [], []
        game = mohreplay.Game(self.output.append, lambda: self.exits.append(True))
        self.addCleanup(game.stop)
        game.start(self.exe, self.game_dir, self.demos, 640, 480, driver=driver)
        return game

    def test_commands_arrive_in_order(self):
        game = self.start()
        # sent before the game opens its pipe: they wait in the queue
        for number in range(50):
            game.send(f"echo {number}")
        self.assertTrue(wait_until(lambda: len(self.commands(game)) == 50))
        self.assertEqual(self.commands(game), [f"echo {n}" for n in range(50)])

    def test_home_folder(self):
        game = self.start()
        home = game.home
        self.assertTrue(os.path.islink(os.path.join(home, "main", "demos")))
        with open(os.path.join(home, "main", "replay.cfg")) as f:
            self.assertIn('bind PAUSE "demopause"', f.read())
        game.stop()
        self.assertFalse(os.path.exists(home))
        # the demos are only linked, never removed
        self.assertTrue(os.path.isfile(os.path.join(self.demos, "first.dm3")))

    def test_xwayland_when_wayland_fails(self):
        with mock.patch.dict(os.environ, {"FAKEGAME_FAIL_DRIVER": "wayland"}):
            os.environ.pop("SDL_VIDEODRIVER")
            game = self.start(driver="wayland")
            self.assertTrue(wait_until(lambda: game.driver == "x11" and game.running()))
            self.assertIn("The game didn't start on Wayland, trying XWayland", self.output)
            game.send("echo hi")
            self.assertTrue(wait_until(lambda: self.commands(game) == ["echo hi"]))
            self.assertFalse(self.exits)

    def test_exit_is_reported(self):
        game = self.start()
        game.send("quit")
        self.assertTrue(wait_until(lambda: self.exits))
        self.assertFalse(game.running())

    def test_stop_kills_a_hung_game(self):
        with mock.patch.dict(os.environ, {"FAKEGAME_HANG": "1"}), \
                mock.patch.multiple(mohreplay.Game, QUIT_WAIT=300, TERM_WAIT=300):
            game = self.start()
            self.assertTrue(wait_until(lambda: os.path.exists(os.path.join(game.home, "main", mohreplay.PIPE))))
            started = time.monotonic()
            game.stop()
            self.assertFalse(game.running())
            self.assertLess(time.monotonic() - started, 3)

    def test_missing_program(self):
        self.output, self.exits = [], []
        game = mohreplay.Game(self.output.append, lambda: self.exits.append(True))
        game.start(os.path.join(self.tmp, "nothing"), self.game_dir, self.demos, 640, 480)
        self.assertTrue(wait_until(lambda: self.exits))
        self.assertTrue(any(line.startswith("ERROR: couldn't start") for line in self.output))
        game.stop()


class TestWindow(FakeGameCase):
    def test_play_fills_the_lists(self):
        window = self.window()
        self.playing(window)
        self.assertEqual(window.kills.topLevelItemCount(), 6)
        self.assertEqual(window.rounds.topLevelItemCount(), 3)
        self.assertEqual(window.rounds.topLevelItem(0).text(2), "Axis win!")
        self.assertEqual(window.slider.maximum(), 600000)
        self.assertIn("obj/obj_team1", window.info.text())
        self.assertIn("recorded by t-", window.info.text())
        players = [window.player.itemText(i) for i in range(window.player.count())]
        self.assertEqual(players, ["All players", "^1Phil", "Al", "Bob", "t-"])

    def test_player_filters_kills(self):
        window = self.window()
        self.playing(window)
        self.assertEqual(len(window.slider.kills), 6)
        self.assertEqual(window.slider.rounds, [(195000, "Round 2"), (420000, "Round 3")])
        window.player.setCurrentIndex(window.player.findData("^1Phil"))
        self.assertEqual(window.kills.topLevelItemCount(), 1)
        self.assertEqual(window.kills.topLevelItem(0).text(2), "t-")
        self.assertEqual(window.slider.kills, [(200000, "t- was machine-gunned by Phil")])

    def test_slider_click_seeks(self):
        window = self.window()
        self.playing(window)
        window.show()
        slider = window.slider
        y = slider.height() // 2
        x = slider.x_of(300000)
        for kind, buttons in ((QEvent.MouseButtonPress, Qt.LeftButton), (QEvent.MouseButtonRelease, Qt.NoButton)):
            QApplication.sendEvent(slider, QMouseEvent(kind, QPointF(x, y), slider.mapToGlobal(QPointF(x, y)),
                                                       Qt.LeftButton, buttons, Qt.NoModifier))
        self.assertTrue(wait_until(lambda: any(c.startswith(("demoseek 29", "demoseek 30"))
                                               for c in self.commands(window.game))))

    def test_kill_click_seeks_before_it(self):
        window = self.window()
        self.playing(window)
        window.jump_to_item(window.kills.topLevelItem(1))
        window.jump_to_item(window.kills.topLevelItem(1))  # a double-click: one seek
        self.assertTrue(wait_until(lambda: "demoseek 8.000" in self.commands(window.game)))
        self.assertEqual(self.commands(window.game).count("demoseek 8.000"), 1)

    def test_only_kills_of_a_player(self):
        window = self.window()
        self.playing(window)
        window.player.setCurrentIndex(window.player.findData("t-"))
        window.only_kills()
        self.assertTrue(wait_until(lambda: window.state.get("only") == "kills"))
        self.assertEqual(self.commands(window.game)[-2:], ["demoseek 6.000", 'demoonly kills "t-"'])
        self.assertIn("only the kills by t-", window.info.text())

    def test_multi_kills_tab(self):
        window = self.window()
        self.playing(window)
        self.assertEqual(window.multikills.topLevelItemCount(), 1)
        item = window.multikills.topLevelItem(0)
        self.assertEqual([item.text(i) for i in range(4)], ["0:10", "t-", "3", "^1Phil, Bob, Al"])
        window.tabs.setCurrentWidget(window.multikills)
        self.assertEqual(window.only_kills_button.text(), "Only these multi-kills")
        self.assertEqual([m[0] for m in window.slider.kills], [10000, 12000, 14500])
        window.only_kills()
        self.assertTrue(wait_until(lambda: window.state.get("only") == "multikills"))
        self.assertEqual(self.commands(window.game)[-2:], ["demoseek 6.000", "demoonly multikills"])
        self.assertIn("Playing only the multi-kills", window.info.text())
        window.player.setCurrentIndex(window.player.findData("^1Phil"))
        self.assertEqual(window.multikills.topLevelItemCount(), 0)
        window.only_kills()
        self.assertEqual(self.messages.shown[-1], ("Only these multi-kills", "There are no multi-kills to play."))
        window.tabs.setCurrentWidget(window.kills)
        self.assertEqual(window.only_kills_button.text(), "Only these kills")
        self.assertEqual(len(window.slider.kills), 1)

    def test_only_watched(self):
        window = self.window()
        self.playing(window)
        window.only_watched()
        self.assertEqual(self.messages.shown[-1][0], "Only while watched")
        window.player.setCurrentIndex(window.player.findData("^1Phil"))
        window.only_watched()
        self.assertTrue(wait_until(lambda: self.commands(window.game)[-1:] == ['demoonly watched "^1Phil"']))
        self.assertIn("demoseek 300.000", self.commands(window.game))

    def test_buttons_send_commands(self):
        window = self.window()
        self.playing(window)
        window.play.click()
        self.assertTrue(wait_until(lambda: window.state.get("paused")))
        window.speed.setCurrentText("2×")
        self.assertTrue(wait_until(lambda: "timescale 2" in self.commands(window.game)))

    def shown(self, window):
        return sorted(window.demos.topLevelItem(i).text(mohreplay.COL_DEMO) for i in range(window.demos.topLevelItemCount())
                      if not window.demos.topLevelItem(i).isHidden())

    def test_filter_demos(self):
        self.add_demo("second-obj_team2", DEMO)
        window = self.window()
        self.assertEqual(self.shown(window), ["first", "second-obj_team2"])
        window.filter.setText("TEAM2")
        self.assertEqual(self.shown(window), ["second-obj_team2"])

    def test_missing_demo(self):
        window = self.window()
        window.start_game()
        window.game.send('demo "nothing"')
        self.assertTrue(wait_until(lambda: "Couldn't load" in window.statusBar().currentMessage()))

    def test_game_quitting(self):
        window = self.window()
        self.playing(window)
        window.game.send("quit")
        self.assertTrue(wait_until(lambda: "The game quit" in window.statusBar().currentMessage()))
        self.assertEqual(window.time.text(), "0:00 / 0:00")
        # and it starts again with a demo
        self.playing(window)


class TestLibrary(FakeGameCase):
    def setUp(self):
        super().setUp()
        other = json.loads(json.dumps(DEMO))
        other["maps"] = [{"time": 0, "map": "dm/mohdm6", "rules": "realism", "realismTicks": 900, "defaultTicks": 0}]
        other["duration"] = 61000
        for kill in other["kills"]:
            kill["killerName"] = kill["killerName"].replace("t-", "<KoS>Bob")
        self.add_demo("other", other)
        with open(os.path.join(self.demos, "broken.dm3"), "w") as f:
            f.write("not a demo")

    def indexed(self, window, names=("first", "other")):
        self.assertTrue(wait_until(lambda: set(names) <= set(window.library.demos)
                                   and not window.library.progress(), 20), "not indexed")
        wait_until(lambda: False, 0.4)  # the list is updated after them

    def shown(self, window):
        return TestWindow.shown(self, window)

    def test_closed_before_opening(self):
        window = self.window()
        window.close()
        app.processEvents()
        self.assertIsNone(window.library.folder)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cache")))

    def test_index_and_cache(self):
        window = self.window()
        self.indexed(window)
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_LENGTH), "10:00")
        self.assertEqual(window.demo_items["other"].text(mohreplay.COL_LENGTH), "1:01")
        self.assertEqual(window.demo_items["broken"].text(mohreplay.COL_LENGTH), "")
        self.assertIn("broken", window.library.failed)
        summary = window.library.demos["first"]
        self.assertEqual(summary["maps"], ["obj/obj_team1"])
        self.assertEqual(summary["players"]["t-"], ["t-", 4, 1])
        self.assertEqual(summary["players"]["phil"], ["^1Phil", 1, 0])
        self.assertIn("&lt;KoS&gt;Bob (4, 1 multi)", window.demo_items["other"].toolTip(mohreplay.COL_DEMO))
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_MAP), "obj_team1")
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_RULES), "")
        self.assertEqual(window.demo_items["other"].text(mohreplay.COL_RULES), "Realism")
        self.assertIn("1:01, realism", window.demo_items["other"].toolTip(mohreplay.COL_DEMO))
        self.assertEqual(window.indexing.text(), "")
        cache = window.library.cache
        self.assertTrue(cache.startswith(os.path.join(self.tmp, "cache")))
        self.assertTrue(os.path.isfile(os.path.join(cache, "first.json")))

        # kept: nothing is indexed again
        again = self.window()
        self.assertTrue(wait_until(lambda: again.library.demos))
        self.assertEqual(set(again.library.demos), {"first", "other"})
        self.assertEqual(again.library.waiting, [])
        self.assertEqual(again.library.busy, {"broken"})  # it's tried once more

    def test_redone_when_the_demo_changes(self):
        window = self.window()
        self.indexed(window)
        changed = dict(DEMO, duration=125000)
        self.add_demo("first", changed)
        future = time.time() + 5
        os.utime(os.path.join(self.demos, "first.dm3"), (future, future))
        window.library.rescan()
        self.assertTrue(wait_until(lambda: window.library.demos["first"]["duration"] == 125000, 20))
        self.assertTrue(wait_until(lambda: window.demo_items["first"].text(mohreplay.COL_LENGTH) == "2:05"))

    def test_new_and_removed_demos(self):
        window = self.window()
        self.indexed(window)
        self.add_demo("new", DEMO)
        os.remove(os.path.join(self.demos, "other.dm3"))
        # the folder is watched
        self.assertTrue(wait_until(lambda: "new" in window.demo_items and window.demo_items["new"].text(mohreplay.COL_LENGTH), 20))
        self.assertNotIn("other", window.demo_items)
        self.assertNotIn("other", window.library.demos)
        self.assertFalse(os.path.exists(os.path.join(window.library.cache, "other.json")))

    def test_without_mohdemoindex(self):
        os.remove(os.path.join(self.bin, "mohdemoindex"))
        window = self.window()
        wait_until(lambda: False, 0.5)
        self.assertEqual(window.library.demos, {})
        self.assertIsNone(window.library.progress())
        self.assertEqual(self.shown(window), ["broken", "first", "other"])

    def test_filter_by_player(self):
        window = self.window()
        self.indexed(window)
        self.assertTrue(window.demos.isColumnHidden(mohreplay.COL_PLAYER))
        window.filter.setText("PHIL")
        self.assertEqual(self.shown(window), ["first", "other"])
        self.assertFalse(window.demos.isColumnHidden(mohreplay.COL_PLAYER))
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_PLAYER), "^1Phil (1)")
        window.filter.setText("kos")
        self.assertEqual(self.shown(window), ["other"])
        self.assertEqual(window.demo_items["other"].text(mohreplay.COL_PLAYER), "<KoS>Bob (4, 1 multi)")
        window.filter.setText("mohdm6 bob")
        self.assertEqual(self.shown(window), ["other"])
        window.filter.setText("obj_team1")
        self.assertEqual(self.shown(window), ["first"])
        self.assertTrue(window.demos.isColumnHidden(mohreplay.COL_PLAYER))
        window.filter.setText("nobody")
        self.assertEqual(self.shown(window), [])

    def test_filter_by_rules(self):
        mixed = json.loads(json.dumps(DEMO))
        mixed["maps"] = [{"time": 0, "map": "obj/obj_team1", "rules": "default"},
                         {"time": 300000, "map": "obj/obj_team2", "rules": "realism"},
                         {"time": 590000, "map": "obj/obj_team4", "rules": ""}]
        self.add_demo("mixed", mixed)
        window = self.window()
        self.indexed(window, ("first", "other", "mixed"))
        self.assertEqual(window.demo_items["mixed"].text(mohreplay.COL_RULES), "Both")
        self.assertEqual(self.shown(window), ["broken", "first", "mixed", "other"])
        window.rules.setCurrentIndex(window.rules.findData("realism"))
        self.assertEqual(self.shown(window), ["mixed", "other"])
        window.filter.setText("kos")
        self.assertEqual(self.shown(window), ["other"])
        window.filter.setText("")
        window.rules.setCurrentIndex(window.rules.findData("default"))
        self.assertEqual(self.shown(window), ["mixed"])
        # kept for next time
        self.assertEqual(self.settings.value("rules"), "default")
        again = self.window()
        self.assertEqual(again.rules.currentData(), "default")

    def test_rules_of_the_demo_playing(self):
        realism = json.loads(json.dumps(DEMO))
        realism["maps"] = [{"time": 0, "map": "obj/obj_team1", "rules": "realism"}]
        self.add_demo("first", realism)
        window = self.window()
        self.playing(window)
        self.assertIn("obj/obj_team1 (realism)", window.info.text())

    def test_old_indexes(self):
        # indexes made before the rules were: no rules, as unknown
        self.assertEqual(mohreplay.summarize(DEMO)["rules"], "")

    def test_play_from_a_player_search(self):
        window = self.window()
        self.indexed(window)
        window.filter.setText("phil")
        self.playing(window)
        self.assertEqual(window.player.currentData(), "^1Phil")
        self.assertEqual(window.kills.topLevelItemCount(), 1)

    def test_sort_by_length(self):
        window = self.window()
        self.indexed(window)
        window.demos.sortByColumn(mohreplay.COL_LENGTH, Qt.AscendingOrder)
        order = [window.demos.topLevelItem(i).text(mohreplay.COL_DEMO) for i in range(3)]
        self.assertEqual(order, ["broken", "other", "first"])


class TestRecording(FakeGameCase):
    def setUp(self):
        super().setUp()
        os.symlink(FAKEGAME, os.path.join(self.bin, "ffmpeg"))
        self.settings.setValue("rec/ffmpeg", os.path.join(self.bin, "ffmpeg"))

    def record(self, window, setup=None, action=None):
        """Records with the Record dialog, as if its button were pressed;
        action opens it, Record… by default."""
        dialogs = []

        def exec_dialog(dialog):
            dialogs.append(dialog)
            if setup:
                setup(dialog)
            dialog.accept()
            return dialog.result()
        with mock.patch.object(mohreplay.RecordDialog, "exec", exec_dialog):
            (action or window.record)()
        return dialogs[0] if dialogs else None

    def video(self, name):
        with open(os.path.join(self.videos, name)) as f:
            return json.load(f)

    def done(self, window, count=1, timeout=20):
        """Waits for the jobs to be over, done or not."""
        self.assertTrue(wait_until(lambda: len(window.jobs) >= count and all(
            j["status"] in ("done", "failed", "canceled") for j in window.jobs), timeout), "not recorded")
        return window.jobs

    def test_record_a_stretch(self):
        window = self.window()
        self.playing(window)

        def setup(dialog):
            dialog.start.setText("1:00")
            dialog.end.setText("1:10")
            dialog.pattern.setText("{demo} {start}")
        self.record(window, setup)
        self.assertFalse(window.queue_dock.isHidden())
        job, = self.done(window)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["item"].text(2), "Done")
        self.assertEqual(job["item"].text(1), "first, 1:00 to 1:10")
        video = self.video("first 1-00.mp4")
        self.assertEqual((video["start"], video["end"]), (60000, 70000))
        self.assertGreaterEqual(video["stop"], 70000)
        cvars = video["cvars"]
        self.assertEqual(cvars["s_loopback"], "1")
        self.assertEqual(cvars["cl_aviFrameRate"], "60")
        self.assertEqual((cvars["r_customwidth"], cvars["r_customheight"]), ("1920", "1080"))
        self.assertIn("-crf 20", cvars["cl_aviPipeFormat"])
        self.assertIn("Recorded", window.statusBar().currentMessage())
        self.assertIsNone(window.recorder)

    def test_record_kills_of_a_player_without_sound(self):
        window = self.window()
        self.playing(window)
        window.player.setCurrentIndex(window.player.findData("t-"))

        def setup(dialog):
            dialog.choices["kills"].setChecked(True)
            dialog.sound.setChecked(False)
            dialog.pattern.setText("{player} frags")
        dialog = self.record(window, setup)
        self.assertEqual(dialog.choices["kills"].text(), "The 4 kills by t-")
        self.assertFalse(dialog.join.isVisible())
        self.done(window)
        video = self.video("t- frags.mp4")
        self.assertEqual((video["only"], video["player"]), ("kills", "t-"))
        self.assertEqual(video["cvars"]["s_loopback"], "0")
        self.assertIn("-an", video["cvars"]["cl_aviPipeFormat"])

    def test_record_multi_kills(self):
        window = self.window()
        self.playing(window)

        def setup(dialog):
            dialog.choices["multikills"].setChecked(True)
            dialog.pattern.setText("multi")
        dialog = self.record(window, setup)
        self.assertEqual(dialog.choices["multikills"].text(), "The 1 multi-kills")
        self.assertFalse(dialog.choices["watched"].isEnabled())
        self.done(window)
        self.assertEqual(self.video("multi.mp4")["only"], "multikills")

    def test_no_overwrite(self):
        window = self.window()
        self.playing(window)
        os.makedirs(self.videos)
        with open(os.path.join(self.videos, "clip.mp4"), "w") as f:
            f.write("mine")

        def setup(dialog):
            dialog.pattern.setText("clip")
        self.record(window, setup)
        job, = self.done(window)
        self.assertEqual(job["output"], os.path.join(self.videos, "clip (2).mp4"))
        self.assertEqual(job["item"].text(0), "clip (2).mp4")
        with open(os.path.join(self.videos, "clip.mp4")) as f:
            self.assertEqual(f.read(), "mine")

    def test_ffmpeg_failing(self):
        window = self.window()
        self.playing(window)
        with mock.patch.dict(os.environ, {"FAKEGAME_FFMPEG_FAIL": "1"}):
            self.record(window)
            job, = self.done(window)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["item"].text(2).startswith("Failed: "))
        self.assertIn("no FFmpeg", job["item"].toolTip(2))
        self.assertIn("Couldn't record", window.statusBar().currentMessage())
        self.assertIsNone(window.recorder)

    def test_ffmpeg_quitting_midway(self):
        window = self.window()
        self.playing(window)
        with mock.patch.dict(os.environ, {"FAKEGAME_FFMPEG_FAIL": "mid", "FAKEGAME_SPEED": "1"}):
            self.record(window)
            job, = self.done(window)
        text = job["item"].toolTip(2)
        self.assertTrue(text.startswith("The recording stopped. Couldn't write"), text)
        self.assertIn("FFmpeg quit", text)
        # half a video isn't kept
        self.assertFalse(os.path.exists(self.videos) and os.listdir(self.videos))

    def test_bad_times(self):
        window = self.window()
        self.playing(window)

        def setup(dialog):
            dialog.start.setText("2:00")
            dialog.end.setText("1:00")
        self.record(window, setup)
        self.assertIn("aren't right", self.messages.shown[-1][1])
        self.assertEqual(window.jobs, [])

    def test_queue(self):
        window = self.window()
        self.playing(window)
        for start in ("1:00", "2:00", "3:00"):
            def setup(dialog, start=start):
                dialog.start.setText(start)
                dialog.end.setText(start.replace(":00", ":05"))
            self.record(window, setup)
        statuses = [j["status"] for j in window.jobs]
        self.assertEqual(statuses, ["recording", "waiting", "waiting"])
        self.assertEqual(window.jobs[1]["item"].text(2), "Waiting")
        # the second is canceled before its turn
        window.jobs[1]["item"].setSelected(True)
        window.cancel_jobs()
        self.done(window, 3)
        self.assertEqual([j["status"] for j in window.jobs], ["done", "canceled", "done"])
        self.assertEqual(sorted(os.listdir(self.videos)), ["first 1-00.mp4", "first 3-00.mp4"])
        window.remove_finished()
        self.assertEqual(window.jobs, [])
        self.assertEqual(window.queue.topLevelItemCount(), 0)

    def test_cancel(self):
        window = self.window()
        self.playing(window)
        with mock.patch.dict(os.environ, {"FAKEGAME_SPEED": "0.01"}):
            self.record(window)
            recorder = window.recorder
            self.assertTrue(wait_until(lambda: window.jobs[0]["item"].text(2).startswith("Recording "), 20))
            window.jobs[0]["item"].setSelected(True)
            window.cancel_jobs()
        self.assertIsNone(window.recorder)
        self.assertEqual(window.jobs[0]["status"], "canceled")
        self.assertFalse(recorder.game.running())
        self.assertFalse(os.path.exists(self.videos) and os.listdir(self.videos))

    def test_selected_kills_in_one_video(self):
        window = self.window()
        self.playing(window)
        # two close together, and one later
        for row in (0, 1, 5):
            window.kills.topLevelItem(row).setSelected(True)

        def setup(dialog):
            dialog.join.setChecked(True)
            dialog.pattern.setText("selected")
        dialog = self.record(window, setup, lambda: window.record_selected(window.kills))
        self.assertEqual(dialog.choices["selected"].text(), "The 3 kills selected")
        self.assertTrue(dialog.join.isVisibleTo(dialog))
        job, = self.done(window)
        self.assertEqual(job["status"], "done", job["item"].toolTip(2))
        parts = self.video("selected.mp4")["joined"]
        self.assertEqual([(p["start"], p["end"]) for p in parts], [(6000, 14000), (396000, 402000)])

    def test_selected_kills_one_video_each(self):
        window = self.window()
        self.playing(window)
        for row in (0, 5):
            window.kills.topLevelItem(row).setSelected(True)

        def setup(dialog):
            dialog.join.setChecked(False)
            dialog.pattern.setText("kill {start}")
        self.record(window, setup, lambda: window.record_selected(window.kills))
        self.done(window, 2)
        self.assertEqual(sorted(os.listdir(self.videos)), ["kill 0-06.mp4", "kill 6-36.mp4"])
        self.assertEqual(self.settings.value("rec/join"), "false")

    def test_selected_multi_kill(self):
        window = self.window()
        self.playing(window)
        window.multikills.topLevelItem(0).setSelected(True)
        dialog = self.record(window, lambda d: d.pattern.setText("multi"),
                             lambda: window.record_selected(window.multikills))
        self.assertFalse(dialog.join.isVisibleTo(dialog))
        self.done(window)
        video = self.video("multi.mp4")
        self.assertEqual((video["start"], video["end"]), (6000, 16500))

    def test_join_failing(self):
        window = self.window()
        self.playing(window)
        for row in (0, 5):
            window.kills.topLevelItem(row).setSelected(True)
        with mock.patch.dict(os.environ, {"FAKEGAME_FFMPEG_FAIL": "join"}):
            self.record(window, lambda d: d.join.setChecked(True), lambda: window.record_selected(window.kills))
            job, = self.done(window)
        self.assertEqual(job["status"], "failed")
        self.assertIn("can't join", job["item"].toolTip(2))
        self.assertFalse(os.path.exists(self.videos) and os.listdir(self.videos))

    def test_player_in_several_demos(self):
        other = json.loads(json.dumps(DEMO))
        other["recorder"]["name"] = other["watched"][0]["name"] = "^2T-"
        for kill in other["kills"]:
            kill["killerName"] = kill["killerName"].replace("t-", "^2T-")
            kill["victimName"] = kill["victimName"].replace("t-", "^2T-")
        self.add_demo("other", other)
        nokills = dict(DEMO, kills=[k for k in DEMO["kills"] if k["killerName"] != "t-"])
        self.add_demo("none", nokills)
        window = self.window()
        TestLibrary.indexed(self, window, ("first", "other", "none"))
        window.filter.setText("t-")
        for name in ("first", "other", "none"):
            window.demo_items[name].setSelected(True)

        def setup(dialog):
            dialog.choices["kills"].setChecked(True)
            dialog.join.setChecked(True)
            dialog.pattern.setText("{player} in {demo}")
        dialog = self.record(window, setup, window.record_demos)
        self.assertEqual(dialog.choices["kills"].text(), "t-'s kills, in 2 of the 3 demos")
        self.assertEqual(dialog.choices["multikills"].text(), "t-'s multi-kills, in 2 of the 3 demos")
        job, = self.done(window)
        self.assertEqual(job["status"], "done", job["item"].toolTip(2))
        self.assertEqual(job["item"].text(1), "2 demos, t-'s kills")
        parts = self.video("t- in 2 demos.mp4")["joined"]
        # each demo with its own spelling of the name
        self.assertEqual(sorted((p["only"], p["player"]) for p in parts), [("kills", "^2T-"), ("kills", "t-")])

    def test_whole_demos_one_video_each(self):
        self.add_demo("second", dict(DEMO, duration=30000))
        window = self.window()
        TestLibrary.indexed(self, window, ("first", "second"))
        window.demo_items["second"].setSelected(True)

        def setup(dialog):
            dialog.choices["everything"].setChecked(True)
            dialog.pattern.setText("{demo}")
        dialog = self.record(window, setup, window.record_demos)
        self.assertEqual(dialog.choices["everything"].text(), "All of the demo")
        self.assertEqual(dialog.choices["kills"].text(), "All the kills")
        job, = self.done(window)
        self.assertEqual(job["item"].text(1), "second")
        video = self.video("second.mp4")
        self.assertEqual((video["start"], video["only"]), (0, ""))
        self.assertEqual(video["stop"], 30000)

    def test_quit_while_recording(self):
        window = self.window()
        self.playing(window)
        with mock.patch.dict(os.environ, {"FAKEGAME_SPEED": "0.01"}):
            self.record(window)
            self.record(window)
            asked = []
            with mock.patch.object(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.No):
                window.close()
            self.assertEqual(asked, ["2 videos are still to record. Quit anyway?"])
            self.assertTrue(window.recorder.game.running())
            with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes):
                recorder = window.recorder
                window.close()
            self.assertFalse(recorder.game.running())
            self.assertEqual([j["status"] for j in window.jobs], ["canceled", "canceled"])


REAL_GAME = os.environ.get("MOHREPLAY_TEST_GAME")
REAL_DEMOS = os.environ.get("MOHREPLAY_TEST_DEMOS")


@unittest.skipUnless(REAL_GAME and REAL_DEMOS, "set MOHREPLAY_TEST_GAME and MOHREPLAY_TEST_DEMOS")
class TestRealGame(FakeGameCase):
    """Plays and records a real demo with the real game, without a window."""

    env = {}

    def setUp(self):
        super().setUp()
        exe = os.environ.get("MOHREPLAY_TEST_EXE") or os.path.join(mohreplay.REPO, ".cmake", "RelWithDebInfo", "openmohaa")
        self.settings.setValue("exe", exe)
        self.settings.setValue("game", REAL_GAME)
        self.settings.setValue("demos", REAL_DEMOS)
        self.settings.setValue("size", "640x360")
        self.demo = os.environ.get("MOHREPLAY_TEST_DEMO") or "d482684ec556d1c3-obj-obj_team1"
        # quiet: the recorder's own volume comes after
        start = mohreplay.Game.start

        def quiet(game, exe, game_dir, demos, width, height, extra=(), driver=None):
            return start(game, exe, game_dir, demos, width, height, ("+set", "s_volume", "0", *extra), driver)
        patch = mock.patch.object(mohreplay.Game, "start", quiet)
        patch.start()
        self.addCleanup(patch.stop)

    def test_play_seek_and_record(self):
        window = self.window()
        window.play_demo(self.demo)
        self.assertTrue(wait_until(lambda: window.state.get("demo") == self.demo and window.index, 60))
        self.assertGreater(window.kills.topLevelItemCount(), 0)
        window.game.send("demoseek 2:00")
        self.assertTrue(wait_until(lambda: 120000 <= window.state.get("time", 0) < 125000
                                   and not window.state.get("seeking"), 30))

        def setup(dialog):
            dialog.start.setText("2:00")
            dialog.end.setText("2:03")
            dialog.size.setCurrentText("640x360")
            dialog.pattern.setText("real")
        with mock.patch.object(mohreplay.RecordDialog, "exec", lambda d: (setup(d), d.accept(), d.result())[-1]):
            window.record()
        output = os.path.join(self.videos, "real.mp4")
        self.assertTrue(wait_until(lambda: window.jobs[0]["status"] not in ("waiting", "recording"), 120), "no video")
        self.assertEqual(window.jobs[0]["status"], "done", window.jobs[0]["item"].toolTip(2))
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,duration",
                                "-of", "json", output], capture_output=True, text=True, check=True)
        streams = json.loads(probe.stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (640, 360))
        self.assertAlmostEqual(float(video["duration"]), 3, delta=0.1)
        self.assertTrue(any(s["codec_type"] == "audio" for s in streams))


if __name__ == "__main__":
    unittest.main()
