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
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
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
         "text": "Phil was rifled by t-", "weapon": "rifle"},
        {"time": 12000, "killer": 0, "killerName": "t-", "victim": 2, "victimName": "Bob",
         "text": "Bob was rifled by t-", "weapon": "rifle"},
        {"time": 14500, "killer": 0, "killerName": "t-", "victim": 3, "victimName": "Al",
         "text": "Al was shot by t-", "weapon": "other"},
        {"time": 200000, "killer": 1, "killerName": "^1Phil", "victim": 0, "victimName": "t-",
         "text": "t- was machine-gunned by Phil", "weapon": "mg"},
        {"time": 250000, "killer": -1, "killerName": "", "victim": 2, "victimName": "Bob",
         "text": "Bob took himself out of commision", "weapon": "other"},
        {"time": 400000, "killer": 0, "killerName": "t-", "victim": 1, "victimName": "^1Phil",
         "text": "Phil was rifled by t-", "weapon": "rifle"},
    ],
    "rounds": [{"time": 195000}, {"time": 420000}],
    "roundEnds": [{"time": 191000, "text": "Axis win!"}, {"time": 416000, "text": "Allies win!"}],
}


def followed_demo():
    """DEMO where Phil, followed from 5:00, kills twice in 2.5 s, a second
    and 3.5 seconds after he's shown."""
    demo = json.loads(json.dumps(DEMO))
    demo["kills"][5:5] = [
        {"time": 301000, "killer": 1, "killerName": "^1Phil", "victim": 0, "victimName": "t-",
         "text": "t- was rifled by Phil", "weapon": "rifle"},
        {"time": 303500, "killer": 1, "killerName": "^1Phil", "victim": 3, "victimName": "Al",
         "text": "Al was sniped by Phil", "weapon": "sniper"},
    ]
    return demo


def team_demo():
    """DEMO with three team kills: t-'s 1.5 s after their third kill and
    alone at 1:40, and Phil's 1.5 s after their kill at 3:20."""
    demo = json.loads(json.dumps(DEMO))

    def team_kill(time, killer, name):
        return {"time": time, "killer": killer, "killerName": name, "victim": 4, "victimName": "Joe",
                "text": "Joe was rifled by " + name, "teamKill": True, "weapon": "rifle"}
    demo["kills"] += [team_kill(16000, 0, "t-"), team_kill(100000, 0, "t-"), team_kill(201500, 1, "^1Phil")]
    demo["kills"].sort(key=lambda kill: kill["time"])
    return demo


def grenade_demo():
    """DEMO with two grenade kills by t-: 2.5 s after their third kill and
    alone at 0:50, the second as an index made before kills were marked."""
    demo = json.loads(json.dumps(DEMO))
    demo["kills"] += [
        {"time": 17000, "killer": 0, "killerName": "t-", "victim": 4, "victimName": "Joe",
         "text": "Joe tripped on t-'s grenade", "grenade": True, "weapon": "grenade"},
        {"time": 50000, "killer": 0, "killerName": "t-", "victim": 4, "victimName": "Joe",
         "text": "Joe is picking t-'s shrapnel out of his teeth"},
    ]
    demo["kills"].sort(key=lambda kill: kill["time"])
    return demo


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
        self.patches.append(mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
                                                         "XDG_CONFIG_HOME": os.path.join(self.tmp, "config")}))
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

    def choose(self, window, killer=None, victim=None, weapons=None, shown=None):
        """The kill filter, as typed and ticked, applied at once."""
        if killer is not None:
            window.killer.setText(killer)
        if shown is not None:
            window.killer.set_shown(shown)
        if victim is not None:
            window.victim.setText(victim)
        if weapons is not None:
            for key, action in window.weapon_actions.items():
                action.setChecked(key in weapons)
        window.apply_kill_filter()

    def listed(self, window):
        """The times of the kills listed."""
        return [window.kills.topLevelItem(i).text(0) for i in range(window.kills.topLevelItemCount())]


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
        # as a key bind passes it
        self.assertTrue(mohreplay.same_player("|Fatal-Finns|  Granu ", "|fatal-finns| granu"))
        self.assertFalse(mohreplay.same_player("phil", "phill"))

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

    def test_demoonly_plan(self):
        plan = mohreplay.demoonly_plan
        self.assertEqual(plan(DEMO), (0, [(0, 600000)]))
        # anyone's kills, wherever the recorder looks, but the deaths of the
        # player shown: t-'s at 3:20, Phil's at 6:40
        self.assertEqual(plan(DEMO, "kills"), (4, [(6000, 16500), (246000, 252000)]))
        # a player's while shown, whoever's is shown
        self.assertEqual(plan(DEMO, "kills", "T-"), (3, [(6000, 16500)]))
        self.assertEqual(plan(followed_demo(), "shownkills"), (5, [(6000, 16500), (300000, 305500)]))
        self.assertEqual(plan(DEMO, "watched", "^1Phil"), (0, [(300000, 600000)]))

    def test_demoonly_plan_with_a_kill_filter(self):
        plan, kf = mohreplay.demoonly_plan, mohreplay.KillFilter
        # anyone's, whoever is shown
        self.assertEqual(plan(DEMO, "kills", kill_filter=kf(weapons=["rifle"])), (2, [(6000, 14000)]))
        self.assertEqual(plan(DEMO, "kills", kill_filter=kf(victims=["BOB"])), (2, [(8000, 14000), (246000, 252000)]))
        # killers by a part of their names, while they're shown
        self.assertEqual(plan(DEMO, "kills", kill_filter=kf(["t"])), (3, [(6000, 16500)]))
        self.assertEqual(plan(DEMO, "kills", kill_filter=kf(["phil", "T-"], weapons=["other"])), (1, [(10500, 16500)]))
        self.assertEqual(plan(followed_demo(), "kills", kill_filter=kf(["t-", "phil"], weapons=["sniper"])),
                         (1, [(300000, 305500)]))
        self.assertEqual(plan(DEMO, "kills", kill_filter=kf(shown=True)), plan(DEMO, "shownkills"))
        # a player given, or whoever is shown, instead of the killers
        self.assertEqual(plan(DEMO, "kills", "t-", kill_filter=kf(["phil"], weapons=["rifle"])), (2, [(6000, 14000)]))
        self.assertEqual(plan(DEMO, "shownkills", kill_filter=kf(["phil"], victims=["al"])), (1, [(10500, 16500)]))
        # while a killer is watched
        self.assertEqual(plan(DEMO, "watched", kill_filter=kf(["phil"])), (0, [(300000, 600000)]))
        self.assertEqual(plan(DEMO, "watched", kill_filter=kf()), (0, []))

    def test_kill_filter(self):
        kill_filter = mohreplay.KillFilter(["^1Phil ", " ", "T-"], ["bob"], ["mg", "sniper", "grenade"])
        self.assertEqual((kill_filter.names, kill_filter.killers), (["^1Phil", "T-"], ["phil", "t-"]))
        # as Weapon lists them; grenade kills aren't kills
        self.assertEqual(kill_filter.weapons, ["sniper", "mg"])
        self.assertTrue(kill_filter and kill_filter.by_shown())
        self.assertEqual(kill_filter.commands(), ["demofilter clear", 'demofilter killer "^1Phil"', 'demofilter killer "T-"',
                                                  'demofilter victim "bob"', "demofilter weapon sniper mg"])
        self.assertEqual(kill_filter.words(), " by ^1Phil or T- of bob with sniper or StG / BAR")
        self.assertEqual(kill_filter.words(killers=False), " of bob with sniper or StG / BAR")
        self.assertTrue(kill_filter.keeps({"killerName": "^4phil", "victimName": "Bob!", "weapon": "mg"}))
        # of t-, with a rifle
        self.assertFalse(kill_filter.keeps(DEMO["kills"][3]) or kill_filter.keeps(DEMO["kills"][1]))
        self.assertTrue(kill_filter.keeps(dict(DEMO["kills"][1], weapon="sniper"), killer=False))
        # whoever is shown, or names
        shown = mohreplay.KillFilter(["x"], shown=True)
        self.assertEqual((shown.names, shown.commands(), shown.words()), ([], ["demofilter clear", "demofilter shown"], ""))
        self.assertTrue(shown.by_shown() and shown.has_killer("anyone"))
        nothing = mohreplay.KillFilter()
        self.assertFalse(nothing or nothing.by_shown())
        self.assertEqual(nothing.commands(), ["demofilter clear"])
        self.assertEqual(mohreplay.or_list(["a", "b", "c"]), "a, b or c")

    def test_weapons(self):
        self.assertEqual([mohreplay.weapon_of(kill) for kill in DEMO["kills"]],
                         ["rifle", "rifle", "other", "mg", "other", "rifle"])
        # indexes made before weapons were, messages not known
        self.assertEqual([mohreplay.weapon_of(kill) for kill in ({}, {"weapon": ""}, {"weapon": "laser"})],
                         ["other"] * 3)
        # the index tells grenade kills, whatever the names
        self.assertFalse(mohreplay.is_grenade({"text": "Bob was rifled by Grenadier", "grenade": False, "weapon": "rifle"}))
        self.assertTrue(mohreplay.is_grenade({"text": "Bob is picking t-'s shrapnel out of his teeth"}))

    def test_filter_kills(self):
        """The kills of a demo the filter keeps, from its summary, as the demo
        list counts them."""
        summary, kf = mohreplay.summarize(DEMO), mohreplay.KillFilter
        self.assertEqual(mohreplay.filter_kills(summary, kf(weapons=["other"])), (2, {"t-": ["t-", 1]}))
        self.assertEqual(mohreplay.filter_kills(summary, kf(["T-"])), (3, {"t-": ["t-", 3]}))
        self.assertEqual(mohreplay.filter_kills(summary, kf(["phil"])), (0, {}))
        self.assertEqual(mohreplay.filter_kills(summary, kf(victims=["bob"])), (2, {"t-": ["t-", 1]}))
        self.assertEqual(mohreplay.filter_kills(summary, kf(shown=True, weapons=["rifle"])), (2, {"t-": ["t-", 2]}))

    def test_deaths_shown(self):
        # t- killed at 3:20 while shown, Phil at 6:40 while followed
        deaths = mohreplay.deaths_shown(DEMO)
        self.assertEqual([kill["time"] for kill in DEMO["kills"] if id(kill) in deaths], [200000, 400000])
        # the client tells, whatever the name
        demo = json.loads(json.dumps(DEMO))
        demo["kills"][3]["victimName"] = "t- (renamed)"
        self.assertEqual(len(mohreplay.deaths_shown(demo)), 2)
        demo["kills"][3]["victim"] = -1
        self.assertEqual(len(mohreplay.deaths_shown(demo)), 1)
        # a player killing themselves while shown made no frag
        demo = json.loads(json.dumps(DEMO))
        demo["kills"][1].update(victim=0, victimName="t-", text="t- blew himself up")
        self.assertEqual(mohreplay.demoonly_plan(demo, "kills", "t-")[0], 2)
        self.assertEqual(mohreplay.summarize(demo)["players"]["t-"], ["t-", 2])

    def test_team_kills(self):
        plan, demo = mohreplay.demoonly_plan, team_demo()
        # not counted, and only played with a kill of their killer at most 2 s
        # away: not Phil's 1.5 s after killing t-, who's shown, at 3:20
        self.assertEqual(plan(demo, "kills"), (4, [(6000, 18000), (246000, 252000)]))
        self.assertEqual(plan(demo, "kills", "T-"), (3, [(6000, 18000)]))
        self.assertEqual(mohreplay.summarize(demo)["players"]["t-"], ["t-", 3])

    def test_grenade_kills(self):
        plan, demo = mohreplay.demoonly_plan, grenade_demo()
        self.assertEqual([mohreplay.is_grenade(kill) for kill in demo["kills"]][2:5], [False, True, True])
        # not counted, not played
        self.assertEqual(plan(demo, "kills", "T-"), (3, [(6000, 16500)]))
        self.assertEqual(mohreplay.summarize(demo)["players"]["t-"], ["t-", 3])

    def test_levels(self):
        """A kill's clip stays inside its level, and who is shown is looked
        up on its level, which starts with a watch event."""
        demo = json.loads(json.dumps(DEMO))
        demo["maps"].append({"time": 256000, "map": "obj/obj_team2"})
        demo["watched"][1:1] = [{"time": 256000, "client": 0, "name": "t-"}]
        demo["kills"].append({"time": 257000, "killer": 0, "killerName": "t-", "victim": 2, "victimName": "Bob",
                              "text": "Bob was rifled by t-"})
        demo["kills"].sort(key=lambda kill: kill["time"])
        self.assertEqual(mohreplay.demoonly_plan(demo, "kills", "T-"), (4, [(6000, 16500), (256000, 259000)]))
        kill = {"time": 255000}
        self.assertEqual(mohreplay.kill_span(demo, kill, 0, 600000), (251000, 256000))
        self.assertEqual(mohreplay.kill_span(demo, dict(kill, time=257000), 0, 600000), (256000, 259000))
        # and the demo's end
        self.assertEqual(mohreplay.kill_span(demo, dict(kill, time=599000), 0, 602000), (595000, 600000))

    def test_lead_in_and_out(self):
        plan = mohreplay.demoonly_plan
        self.assertEqual(plan(DEMO, "kills", "T-", 1000, 400), (3, [(9000, 12400), (13500, 14900)]))
        self.assertEqual(plan(DEMO, "kills", "T-", 10000, 5000), (3, [(0, 19500)]))

    def test_wording(self):
        self.assertEqual([mohreplay.the(n, "kill", "kills") for n in (0, 1, 3)], ["No kills", "The kill", "The 3 kills"])
        self.assertEqual([mohreplay.about(msec) for msec in (20000, 291000, 3900000)],
                         ["under a minute", "about 5 min", "about 1 h 5 min"])

    def test_stretches(self):
        spans = [(6000, 12000), (-500, 1500), (8000, 14000), (14800, 20000), (25000, 30000), (30600, 32000)]
        # overlapping or less than a second apart
        self.assertEqual(mohreplay.stretches(spans), [(0, 1500), (6000, 20000), (25000, 32000)])
        # but not when the player shown changes in between, even right at the end
        self.assertEqual(mohreplay.stretches(spans, [10000, 30000]),
                         [(0, 1500), (6000, 20000), (25000, 30000), (30600, 32000)])
        self.assertEqual(mohreplay.stretches(spans, [14500]), [(0, 1500), (6000, 14000), (14800, 20000), (25000, 32000)])


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

    def test_settings_kept(self):
        game = self.start()
        self.assertFalse(os.path.exists(os.path.join(game.home, mohreplay.GAME_CONFIG)))
        # what the game writes when a setting changes
        with open(os.path.join(game.home, mohreplay.GAME_CONFIG), "w") as f:
            f.write('seta cg_fov "90"\n')
        game.stop()
        with open(mohreplay.kept_config()) as f:
            self.assertEqual(f.read(), 'seta cg_fov "90"\n')
        # the next game starts with them
        game.start(self.exe, self.game_dir, self.demos, 640, 480)
        with open(os.path.join(game.home, mohreplay.GAME_CONFIG)) as f:
            self.assertEqual(f.read(), 'seta cg_fov "90"\n')
        # and when it quits by itself, they're kept already
        with open(os.path.join(game.home, mohreplay.GAME_CONFIG), "w") as f:
            f.write('seta cg_fov "100"\n')
        game.send("quit")
        self.assertTrue(wait_until(lambda: self.exits))
        with open(mohreplay.kept_config()) as f:
            self.assertEqual(f.read(), 'seta cg_fov "100"\n')

    def test_app_settings_not_kept(self):
        game = self.start()
        with open(os.path.join(game.home, mohreplay.GAME_CONFIG), "w") as f:
            f.write('unbindall\nbind w "+forward"\nbind UPARROW "demonextkill t-"\nbind PAUSE "demopause"\n'
                    'seta com_pipefile "replay_pipe"\nseta cg_fov "90"\n')
        game.stop()
        # nor in the game folder, where other games would get them
        for path in (mohreplay.kept_config(), os.path.join(self.game_dir, mohreplay.GAME_CONFIG),
                     os.path.join(self.game_dir, "main", "omconfig.cfg")):
            with open(path) as f:
                self.assertEqual(f.read(), 'unbindall\nbind w "+forward"\nseta cg_fov "90"\n')

    def test_recorder_settings_not_kept(self):
        with open(os.path.join(self.tmp, "kept.cfg"), "w") as f:
            f.write('seta cg_fov "90"\n')
        os.makedirs(os.path.dirname(mohreplay.kept_config()))
        shutil.copyfile(os.path.join(self.tmp, "kept.cfg"), mohreplay.kept_config())
        game = mohreplay.Game(lambda line: None, lambda: None, keep_config=False)
        self.addCleanup(game.stop)
        game.start(self.exe, self.game_dir, self.demos, 640, 480)
        with open(os.path.join(game.home, mohreplay.GAME_CONFIG), "r+") as f:
            self.assertEqual(f.read(), 'seta cg_fov "90"\n')
            f.write('seta cg_fov "120"\n')
        game.stop()
        with open(mohreplay.kept_config()) as f:
            self.assertEqual(f.read(), 'seta cg_fov "90"\n')

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
    def test_viewmodel_and_forcemodel_toggles(self):
        window = self.window()
        self.assertTrue(window.viewmodel_action.isChecked())
        self.assertTrue(window.forcemodel_action.isChecked())
        window.viewmodel_action.trigger()
        self.assertFalse(window.viewmodel_action.isChecked())
        self.assertEqual(self.settings.value("viewmodel"), "0")
        window.forcemodel_action.trigger()
        self.assertFalse(window.forcemodel_action.isChecked())
        self.assertEqual(self.settings.value("forcemodel"), "0")
        window.viewmodel_action.trigger()
        self.assertTrue(window.viewmodel_action.isChecked())
        self.assertEqual(self.settings.value("viewmodel"), "1")
        # kept
        self.assertFalse(self.window().forcemodel_action.isChecked())

    def test_choose_folder(self):
        window = self.window()
        self.assertEqual(window.folder_action.text(), "demos")
        other = os.path.join(self.tmp, "more demos")
        os.makedirs(other)
        shutil.copyfile(os.path.join(self.demos, "first.dm3"), os.path.join(other, "again.dm3"))
        with mock.patch.object(mohreplay.QFileDialog, "getExistingDirectory", lambda *args: other):
            window.choose_folder()
        self.assertEqual(self.settings.value("demos"), other)
        self.assertEqual(window.folder_action.text(), "more demos")
        self.assertTrue(wait_until(lambda: list(window.demo_items) == ["again"]))

    def test_play_fills_the_lists(self):
        window = self.window()
        self.playing(window)
        # not t-'s death at 3:20 while shown, nor Phil's at 6:40 while followed
        self.assertEqual(self.listed(window), ["0:10", "0:12", "0:14", "4:10"])
        self.assertEqual(window.rounds.topLevelItemCount(), 3)
        self.assertEqual(window.rounds.topLevelItem(0).text(2), "Axis win!")
        self.assertEqual(window.slider.maximum(), 600000)
        self.assertEqual(window.title.text(), "first")
        self.assertIn("obj/obj_team1", window.info.text())
        self.assertIn("recorded by t-", window.info.text())
        self.assertEqual([window.tabs.tabText(i) for i in range(window.tabs.count())], ["Kills (4)", "Rounds (3)"])
        # with what, and how in the tooltip, as the game told it
        self.assertEqual(window.kills.columnCount(), 4)
        self.assertEqual([window.kills.topLevelItem(i).text(3) for i in range(4)], ["Rifle", "Rifle", "Other", "Other"])
        self.assertIn("was", window.kills.topLevelItem(0).toolTip(2))
        # the players to choose from
        self.assertEqual(window.killer.players, ["^1Phil", "Al", "Bob", "t-"])
        self.assertEqual(window.victim.players, window.killer.players)
        # the game is told the filter
        self.assertTrue(wait_until(lambda: "demofilter clear" in self.commands(window.game)))

    def test_killers(self):
        window = self.window()
        self.playing(window)
        self.assertEqual(len(window.slider.kills), 4)
        self.assertEqual(window.slider.rounds, [(195000, "Round 2"), (420000, "Round 3")])
        # only the kills made while the player is shown: Phil's at 3:20 is
        # while t- is, t-'s at 6:40 while Phil is
        self.choose(window, killer="phil")
        self.assertEqual(self.listed(window), [])
        self.assertEqual(window.slider.kills, [])
        self.assertEqual(window.tabs.tabText(0), "Kills (0 of 4)")
        self.choose(window, killer="T")
        self.assertEqual(self.listed(window), ["0:10", "0:12", "0:14"])
        self.assertEqual(len(window.slider.kills), 3)
        # several
        self.choose(window, killer="phil, t-")
        self.assertEqual(self.listed(window), ["0:10", "0:12", "0:14"])
        self.assertEqual(window.tabs.tabText(0), "Kills (3 of 4)")
        self.assertTrue(wait_until(lambda: self.commands(window.game)[-3:] == [
            "demofilter clear", 'demofilter killer "phil"', 'demofilter killer "t-"']))

    def test_victims_and_weapons(self):
        window = self.window()
        self.playing(window)
        # anyone's, whoever is shown: Bob's suicide too
        self.choose(window, victim="bob")
        self.assertEqual(self.listed(window), ["0:12", "4:10"])
        self.choose(window, victim="", weapons=["rifle"])
        self.assertEqual(self.listed(window), ["0:10", "0:12"])
        self.assertEqual(window.weapon.text(), "Rifle")
        self.choose(window, weapons=["rifle", "other"])
        self.assertEqual(self.listed(window), ["0:10", "0:12", "0:14", "4:10"])
        self.assertEqual(window.weapon.text(), "Rifle, Other")
        # all together
        self.choose(window, killer="t-", victim="al, phil", weapons=["other"])
        self.assertEqual(self.listed(window), ["0:14"])
        self.assertTrue(wait_until(lambda: self.commands(window.game)[-5:] == [
            "demofilter clear", 'demofilter killer "t-"', 'demofilter victim "al"', 'demofilter victim "phil"',
            "demofilter weapon other"]))
        self.choose(window, killer="", victim="", weapons=[])
        self.assertEqual(window.weapon.text(), "Any weapon")
        self.assertEqual(window.tabs.tabText(0), "Kills (4)")

    def test_filter_fields(self):
        window = self.window()
        self.playing(window)
        # applied once typing stops
        window.killer.setText("ph")
        window.killer.setText("phil")
        self.assertEqual(self.listed(window), ["0:10", "0:12", "0:14", "4:10"])
        self.assertTrue(wait_until(lambda: self.listed(window) == [], 2))
        # the menu adds and takes off the players of the demo playing
        window.killer.open_menu()
        actions = {action.text(): action for action in window.killer.menu.actions()}
        self.assertTrue(actions["^1Phil"].isChecked())
        actions["t-"].trigger()
        self.assertEqual(window.killer.text(), "phil, t-")
        actions["^1Phil"].trigger()
        self.assertEqual(window.killer.text(), "t-")
        window.killer.menu.close()
        # whoever is shown instead of names, and names instead of it
        actions["Whoever is shown"].trigger()
        self.assertTrue(window.killer.shown())
        self.assertEqual((window.killer.text(), window.killer.placeholderText()), ("", "Whoever is shown"))
        self.assertTrue(window.kill_filter().shown)
        window.killer.setText("bob")
        self.assertFalse(window.killer.shown())
        self.assertEqual(window.killer.placeholderText(), "Anyone")
        # Victim's has no whoever is shown
        window.victim.open_menu()
        self.assertEqual([a.text() for a in window.victim.menu.actions()], ["^1Phil", "Al", "Bob", "t-"])
        window.victim.menu.close()
        # Weapon's menu takes several, and any
        menu = window.weapon.menu()
        self.assertEqual([a.text() for a in menu.actions() if a.text()], list(mohreplay.WEAPONS.values()) + ["Any weapon"])
        window.weapon_actions["sniper"].trigger()
        window.weapon_actions["mg"].trigger()
        self.assertEqual(window.weapon.text(), "Sniper, StG / BAR")
        next(a for a in menu.actions() if a.text() == "Any weapon").trigger()
        self.assertEqual(window.weapon.text(), "Any weapon")

    def test_kills_while_shown(self):
        demo = json.loads(json.dumps(DEMO))
        # Phil is shown from a second before his kill to a second after it,
        # then t-, who kills under another name at 6:40
        demo["watched"] = [{"time": 0, "client": 0, "name": "t-"}, {"time": 199000, "client": 1, "name": "^1Phil"},
                           {"time": 201000, "client": 0, "name": "t-"}]
        demo["kills"][5]["killerName"] = "t-.fi"
        self.add_demo("shown", demo)
        window = self.window()
        self.playing(window, "shown")
        self.choose(window, killer="phil")
        item = window.kills.topLevelItem(0)
        self.assertEqual(window.kills.topLevelItemCount(), 1)
        # only while he's shown
        self.assertEqual(item.data(1, Qt.UserRole), [(199000, 201000)])
        window.only_kills()
        self.assertTrue(wait_until(lambda: self.commands(window.game)[-4:] == [
            "demofilter clear", 'demofilter killer "phil"', "demoseek 199.000", "demoonly kills"]))
        # the same client under another name
        self.choose(window, killer="t-.fi")
        self.assertEqual(self.listed(window), ["6:40"])
        # all the kills, whoever is shown
        self.choose(window, killer="")
        self.assertEqual(window.kills.topLevelItemCount(), 6)

    def test_whoever_is_shown(self):
        self.add_demo("followed", followed_demo())
        window = self.window()
        self.playing(window, "followed")
        self.choose(window, shown=True)
        # t-'s while he plays and Phil's while he's followed, not Phil's at
        # 3:20 (t- is shown) or t-'s at 6:40 (Phil is)
        rows = [window.kills.topLevelItem(i) for i in range(window.kills.topLevelItemCount())]
        self.assertEqual([(row.text(0), row.text(1)) for row in rows],
                         [("0:10", "t-"), ("0:12", "t-"), ("0:14", "t-"), ("5:01", "^1Phil"), ("5:03", "^1Phil")])
        self.assertEqual(rows[3].data(1, Qt.UserRole), [(300000, 303000)])
        self.assertEqual([m[0] for m in window.slider.kills], [10000, 12000, 14500, 301000, 303500])
        window.only_kills()
        self.assertTrue(wait_until(lambda: window.state.get("only") == "kills"))
        self.assertEqual(self.commands(window.game)[-4:], ["demofilter clear", "demofilter shown", "demoseek 6.000",
                                                          "demoonly kills"])
        self.assertTrue(wait_until(lambda: "Playing only the kills of whoever is shown" in window.info.text()))

        # from the one selected
        window.kills.setCurrentItem(rows[3])
        window.only_kills()
        self.assertTrue(wait_until(lambda: self.commands(window.game)[-2:] == ["demoseek 300.000", "demoonly kills"]))

        # with a weapon
        self.choose(window, weapons=["sniper"])
        self.assertEqual(self.listed(window), ["5:03"])
        self.assertTrue(wait_until(lambda: "Playing only the kills of whoever is shown with sniper" in window.info.text()))

        window.only_watched()
        self.assertEqual(self.messages.shown[-1], ("Only while watched", "Type a killer first."))
        # still chosen in the next demo
        self.playing(window)
        self.assertTrue(window.killer.shown())
        self.assertEqual(window.kills.topLevelItemCount(), 0)
        self.choose(window, weapons=[])
        self.assertEqual(window.kills.topLevelItemCount(), 3)

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
        self.choose(window, killer="t-")
        self.assertTrue(window.everything_button.isChecked())
        window.only_kills_button.click()
        self.assertTrue(window.only_kills_button.isChecked())
        self.assertTrue(wait_until(lambda: window.state.get("only") == "kills"))
        self.assertEqual(self.commands(window.game)[-4:], ["demofilter clear", 'demofilter killer "t-"', "demoseek 6.000",
                                                          "demoonly kills"])
        self.assertIn("only the kills by t-", window.info.text())
        self.assertTrue(wait_until(lambda: window.only_kills_button.isChecked(), 2))
        window.everything_button.click()
        self.assertTrue(wait_until(lambda: window.state.get("only") == ""))
        self.assertTrue(wait_until(lambda: window.everything_button.isChecked(), 2))

    def test_only_watched(self):
        window = self.window()
        self.playing(window)
        window.watched_button.click()
        self.assertEqual(self.messages.shown[-1], ("Only while watched", "Type a killer first."))
        # nothing plays otherwise
        self.assertTrue(window.everything_button.isChecked())
        self.choose(window, killer="bob")
        window.only_watched()
        self.assertEqual(self.messages.shown[-1], ("Only while watched", "bob isn't watched in this demo."))
        self.choose(window, killer="bob, PHIL")
        window.only_watched()
        self.assertTrue(wait_until(lambda: self.commands(window.game)[-2:] == ["demoseek 300.000", "demoonly watched"]))
        self.assertTrue(wait_until(lambda: "only while bob or PHIL is watched" in window.info.text()))

    def test_buttons_send_commands(self):
        window = self.window()
        self.playing(window)
        window.play.click()
        self.assertTrue(wait_until(lambda: window.state.get("paused")))
        window.speed.setCurrentText("2×")
        self.assertTrue(wait_until(lambda: "timescale 2" in self.commands(window.game)))

    def test_kill_buttons_and_keys_go_to_the_kills_listed(self):
        window = self.window()
        self.playing(window)
        buttons = {button.toolTip(): button for button in window.findChildren(mohreplay.QToolButton)}
        # the keys run demonextkill and demoprevkill, which go to the
        # filter's kills; the buttons tell it first, in case
        self.assertEqual((mohreplay.BINDS["UPARROW"], mohreplay.BINDS["DOWNARROW"]), ("demonextkill", "demoprevkill"))
        for killer, told in (("t-", ["demofilter clear", 'demofilter killer "t-"']), ("", ["demofilter clear"])):
            self.choose(window, killer=killer)
            buttons["Next kill listed (Up)"].click()
            buttons["Previous kill listed (Down)"].click()
            self.assertTrue(wait_until(lambda: self.commands(window.game)[-2 * len(told) - 2:] == [
                *told, "demonextkill", *told, "demoprevkill"]), killer)

    def shown(self, window):
        """The names of the demos shown, in folders shown."""
        def shown(item):
            return not item.isHidden() and (not item.parent() or shown(item.parent()))
        return sorted(name for name, item in window.demo_items.items() if shown(item))

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


class TestSettings(FakeGameCase):
    def test_tabs(self):
        # on the window, as the app opens it: left alone, PySide may delete
        # it twice when Python quits
        window = self.window()
        dialog = mohreplay.SettingsDialog(self.settings, window, "videos")
        self.assertEqual(dialog.tabs.tabText(dialog.tabs.currentIndex()), "Videos")
        dialog.fields["demos"].setText(os.path.join(self.tmp, "other demos"))
        dialog.kill_before.setValue(6)
        dialog.videos.size.setCurrentText("big")
        dialog.videos.blur.setChecked(True)
        dialog.accept()
        # a size that isn't one: nothing kept
        self.assertEqual(self.messages.shown, [("Settings", "The video size should be like 1920x1080.")])
        self.assertIsNone(self.settings.value("rec/size"))
        self.assertEqual(self.settings.value("demos"), self.demos)
        dialog.videos.size.setCurrentText("1280×720")
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertEqual(self.settings.value("demos"), os.path.join(self.tmp, "other demos"))
        self.assertEqual(self.settings.value("kill_before"), "6.0")
        self.assertEqual((self.settings.value("rec/size"), self.settings.value("rec/blur")), ("1280x720", "true"))
        self.assertEqual(mohreplay.video_summary(self.settings),
                         "1280x720, 60 fps with motion blur, H.264 (CRF 20), sound 192k, models forced")
        again = mohreplay.SettingsDialog(self.settings, window)
        self.assertEqual(again.tabs.currentIndex(), 0)
        self.assertTrue(again.videos.blur.isChecked())

    def test_video_settings(self):
        video = mohreplay.video_settings(self.settings)
        self.assertEqual((video["width"], video["height"], video["fps"], video["sound"]), (1920, 1080, "60", True))
        self.assertEqual(video["options"], "-c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p "
                                           "-c:a aac -b:a 192k -movflags +faststart")
        # watching without the gun: videos too, unless they're told otherwise
        self.settings.setValue("viewmodel", "0")
        self.assertEqual(mohreplay.video_settings(self.settings)["viewmodel"], "0")
        self.assertIn("no viewmodel", mohreplay.video_summary(self.settings))
        self.settings.setValue("rec/viewmodel", "1")
        self.assertEqual(mohreplay.video_settings(self.settings)["viewmodel"], "1")


class TestLibrary(FakeGameCase):
    def setUp(self):
        super().setUp()
        other = json.loads(json.dumps(DEMO))
        other["maps"] = [{"time": 0, "map": "dm/mohdm6", "rules": "realism", "realismTicks": 900, "defaultTicks": 0}]
        other["duration"] = 361000
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
        self.assertEqual(window.demo_items["other"].text(mohreplay.COL_LENGTH), "6:01")
        self.assertEqual(window.demo_items["broken"].text(mohreplay.COL_LENGTH), "")
        self.assertIn("broken", window.library.failed)
        summary = window.library.demos["first"]
        self.assertEqual(summary["maps"], ["obj/obj_team1"])
        # the kills made while they're shown
        self.assertEqual(summary["players"]["t-"], ["t-", 3])
        self.assertEqual(summary["players"]["phil"], ["^1Phil", 0])
        self.assertIn("&lt;KoS&gt;Bob (3)", window.demo_items["other"].toolTip(mohreplay.COL_DEMO))
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_MAP), "obj_team1")
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_RULES), "")
        self.assertEqual(window.demo_items["other"].text(mohreplay.COL_RULES), "Realism")
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_RECORDER), "t-")
        self.assertEqual(window.demo_items["broken"].text(mohreplay.COL_RECORDER), "")
        self.assertIn("6:01, realism", window.demo_items["other"].toolTip(mohreplay.COL_DEMO))
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

    def test_demos_in_folders(self):
        # named by their path, so the same file name in two folders is two
        # demos; hidden folders are left out
        for name in ("clan/first", "clan/war 2/first", ".trash/first"):
            os.makedirs(os.path.dirname(os.path.join(self.demos, name)), exist_ok=True)
            self.add_demo(name, DEMO)
        window = self.window()
        self.indexed(window, ("first", "other", "clan/first", "clan/war 2/first"))
        self.assertEqual(self.shown(window), ["broken", "clan/first", "clan/war 2/first", "first", "other"])
        self.assertEqual(window.demo_items["clan/war 2/first"].text(mohreplay.COL_LENGTH), "10:00")
        self.assertTrue(os.path.isfile(os.path.join(window.library.cache, "clan", "war 2", "first.json")))
        # in their folders, by their file name; folders first
        clan = window.folder_items["clan"]
        self.assertEqual(clan.text(mohreplay.COL_DEMO), "clan (2)")
        self.assertEqual([clan.child(i).text(mohreplay.COL_DEMO) for i in range(clan.childCount())], ["war 2 (1)", "first"])
        self.assertIs(window.demo_items["clan/war 2/first"].parent(), window.folder_items["clan/war 2"])
        self.assertEqual(window.demos.topLevelItem(0), clan)
        self.assertTrue(window.demos.rootIsDecorated())
        window.filter.setText("WAR")
        self.assertEqual(self.shown(window), ["clan/war 2/first"])
        self.assertEqual(clan.text(mohreplay.COL_DEMO), "clan (1)")
        window.filter.setText("first")
        self.assertEqual(window.demos_of([clan]), ["clan/war 2/first", "clan/first"])
        window.filter.setText("")
        # a double-click on a folder plays nothing
        window.demos.itemActivated.emit(clan, 0)
        self.assertIsNone(window.pending_demo)
        window.demos.itemActivated.emit(window.demo_items["clan/war 2/first"], 0)
        self.assertEqual(window.pending_demo, "clan/war 2/first")
        self.playing(window, "clan/war 2/first")
        # the folders are watched, new ones too
        os.makedirs(os.path.join(self.demos, "clan", "new"))
        self.assertTrue(wait_until(lambda: os.path.join(self.demos, "clan", "new") in window.library.watcher.directories(), 20))
        self.add_demo("clan/new/late", DEMO)
        self.assertTrue(wait_until(lambda: "clan/new/late" in window.demo_items
                                   and window.demo_items["clan/new/late"].text(mohreplay.COL_LENGTH), 20))
        os.remove(os.path.join(self.demos, "clan", "first.dm3"))
        self.assertTrue(wait_until(lambda: "clan/first" not in window.demo_items, 20))
        self.assertFalse(os.path.exists(os.path.join(window.library.cache, "clan", "first.json")))
        # a folder left empty goes
        shutil.rmtree(os.path.join(self.demos, "clan", "new"))
        self.assertTrue(wait_until(lambda: "clan/new" not in window.folder_items, 20))
        self.assertIn("clan", window.folder_items)

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
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_PLAYER), "^1Phil (0)")
        window.filter.setText("kos")
        self.assertEqual(self.shown(window), ["other"])
        self.assertEqual(window.demo_items["other"].text(mohreplay.COL_PLAYER), "<KoS>Bob (3)")
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

    def test_kill_filter(self):
        """The demos with kills the filter keeps, with their killers and how
        many each."""
        window = self.window()
        self.indexed(window)
        self.choose(window, killer="kos")
        self.assertEqual(self.shown(window), ["other"])
        self.assertFalse(window.demos.isColumnHidden(mohreplay.COL_PLAYER))
        self.assertEqual(window.demo_items["other"].text(mohreplay.COL_PLAYER), "<KoS>Bob (3)")
        # Phil's kill at 3:20 is while t- is shown: not his
        self.choose(window, killer="phil")
        self.assertEqual(self.shown(window), [])
        self.choose(window, killer="t-, bob")
        self.assertEqual(self.shown(window), ["first", "other"])
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_PLAYER), "t- (3)")
        self.choose(window, killer="", victim="phil")
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_PLAYER), "t- (1)")
        self.assertEqual(window.demo_items["other"].text(mohreplay.COL_PLAYER), "<KoS>Bob (1)")
        self.choose(window, victim="", weapons=["rifle", "other"])
        # Bob's suicide counts, but has no killer to tell
        self.assertEqual(window.demo_items["first"].text(mohreplay.COL_PLAYER), "t- (3)")
        self.choose(window, weapons=["mg"])
        self.assertEqual(self.shown(window), [])
        # whoever is shown
        self.choose(window, weapons=[], shown=True)
        self.assertEqual(self.shown(window), ["first", "other"])
        # with the search's words
        window.filter.setText("mohdm6")
        self.assertEqual(self.shown(window), ["other"])
        window.filter.setText("")
        self.choose(window, shown=False)
        self.assertEqual(self.shown(window), ["broken", "first", "other"])
        self.assertTrue(window.demos.isColumnHidden(mohreplay.COL_PLAYER))

    def test_short_demos_hidden(self):
        self.add_demo("short", dict(DEMO, duration=mohreplay.MIN_LENGTH - 1))
        self.add_demo("long enough", dict(DEMO, duration=mohreplay.MIN_LENGTH))
        window = self.window()
        # shown until their length is known
        self.assertIn("short", self.shown(window))
        self.indexed(window, ("first", "other", "short", "long enough"))
        self.assertEqual(window.demo_items["short"].text(mohreplay.COL_LENGTH), "4:59")
        self.assertEqual(self.shown(window), ["broken", "first", "long enough", "other"])
        # whatever the filter
        window.filter.setText("t-")
        self.assertNotIn("short", self.shown(window))

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
        self.assertNotIn("r_motionBlur", cvars)
        self.assertIn("Recorded", window.statusBar().currentMessage())
        self.assertIsNone(window.recorder)

    def test_record_a_demo_in_a_folder(self):
        os.makedirs(os.path.join(self.demos, "clan", "war 2"))
        self.add_demo("clan/war 2/first", DEMO)
        window = self.window()
        self.playing(window, "clan/war 2/first")

        def setup(dialog):
            dialog.start.setText("1:00")
            dialog.end.setText("1:10")
            dialog.pattern.setText("{demo} {start}")
        self.record(window, setup)
        job, = self.done(window)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["item"].text(1), "clan/war 2/first, 1:00 to 1:10")
        # named after the demo's file
        video = self.video("first 1-00.mp4")
        self.assertEqual((video["start"], video["end"]), (60000, 70000))

    def change_video(self, dialog, change):
        """Changes the video settings with the Record dialog's Change…"""
        def exec_dialog(videos):
            change(videos.videos)
            videos.accept()
            return videos.result()
        with mock.patch.object(mohreplay.VideoSettingsDialog, "exec", exec_dialog):
            dialog.change_video()

    def test_motion_blur(self):
        window = self.window()
        self.playing(window)

        def setup(dialog):
            self.assertEqual(dialog.summary.text(), "1920x1080, 60 fps, H.264 (CRF 20), sound 192k, models forced")
            self.change_video(dialog, lambda videos: (self.assertFalse(videos.blur.isChecked()),
                                                      videos.blur.setChecked(True)))
            self.assertEqual(dialog.summary.text(),
                             "1920x1080, 60 fps with motion blur, H.264 (CRF 20), sound 192k, models forced")
            dialog.pattern.setText("blur")
        self.record(window, setup)
        self.done(window)
        cvars = self.video("blur.mp4")["cvars"]
        # the renderer's, over all of each frame's time, the colors unchanged
        self.assertEqual([cvars[name] for name in ("r_temporal", "r_motionBlur", "r_motionBlurShutter",
                                                   "r_motionBlurFps", "r_saturation")], ["1", "1", "1", "0", "1"])
        self.assertEqual(cvars["cl_aviFrameRate"], "60")
        self.assertTrue(cvars["cl_aviPipeFormat"].startswith("-c:v libx264 "))

        # kept, and the same in Settings
        self.settings.setValue("rec/fps", "30")
        self.settings.setValue("rec/advanced", "-vf scale=640:-2 -c:v libx264")

        def advanced(dialog):
            self.assertIn("with motion blur, FFmpeg options of your own", dialog.summary.text())
            dialog.pattern.setText("advanced")
        self.record(window, advanced)
        self.done(window, 2)
        cvars = self.video("advanced.mp4")["cvars"]
        self.assertEqual((cvars["r_motionBlur"], cvars["cl_aviFrameRate"]), ("1", "30"))
        self.assertEqual(cvars["cl_aviPipeFormat"], "-vf scale=640:-2 -c:v libx264")

    def test_record_kills_of_a_player_without_sound(self):
        window = self.window()
        self.playing(window)
        self.choose(window, killer="t-")

        self.settings.setValue("rec/sound", "false")

        def setup(dialog):
            dialog.choices["kills"].setChecked(True)
            dialog.pattern.setText("{player} frags")
        dialog = self.record(window, setup)
        self.assertEqual(dialog.choices["kills"].text(), "The 3 kills by t-, under a minute")
        self.assertFalse(dialog.join.isVisible())
        self.done(window)
        video = self.video("t- frags.mp4")
        self.assertEqual((video["only"], video["player"]), ("kills", ""))
        self.assertEqual(video["filter"], {"shown": False, "killers": ["t-"], "victims": [], "weapons": []})
        self.assertEqual(video["cvars"]["s_loopback"], "0")
        self.assertIn("-an", video["cvars"]["cl_aviPipeFormat"])

    def test_lengths_follow_the_lead_in_and_out(self):
        window = self.window()
        self.playing(window)
        self.choose(window, killer="t-")

        def setup(dialog):
            self.assertEqual(dialog.choices["kills"].text(), "The 3 kills by t-, under a minute")
            dialog.kill_before.setValue(30)
            dialog.kill_after.setValue(30)
            # 0:00 to 0:44.5, while t- is shown
            self.assertTrue(wait_until(lambda: dialog.choices["kills"].text() == "The 3 kills by t-, about 1 min", 2),
                            dialog.choices["kills"].text())
            dialog.choices["kills"].setChecked(True)
            dialog.pattern.setText("long")
        self.record(window, setup)
        self.done(window)
        video = self.video("long.mp4")
        self.assertEqual((video["start"], video["cvars"]["cl_demoKillBefore"], video["cvars"]["cl_demoKillAfter"]),
                         (0, "30.0", "30.0"))

    def test_record_recorder_frags(self):
        window = self.window()
        self.playing(window)

        def setup(dialog):
            dialog.choices["recorderkills"].setChecked(True)
            dialog.pattern.setText("{demo} {player} recfrags")
        dialog = self.record(window, setup)
        self.assertEqual(dialog.choices["recorderkills"].text(), "The 3 recorder frags, under a minute")
        # what can't be recorded isn't offered: while who is watched?
        self.assertFalse(dialog.choices["watched"].isEnabled())
        self.assertTrue(dialog.choices["watched"].isHidden())
        self.assertFalse(dialog.choices["recorderkills"].isHidden())
        self.assertTrue(dialog.nothing.isHidden())
        job, = self.done(window)
        self.assertEqual(job["item"].text(1), "first, the 3 recorder frags")
        video = self.video("first t- recfrags.mp4")
        # from just before the first kill
        self.assertEqual((video["start"], video["only"], video["player"]), (6000, "kills", "t-"))

    def test_record_kills_with_a_weapon(self):
        window = self.window()
        self.playing(window)
        self.choose(window, victim="bob, phil", weapons=["rifle"])

        def setup(dialog):
            dialog.choices["recorderkills"].setChecked(True)
            dialog.pattern.setText("rifle")
        dialog = self.record(window, setup)
        self.assertEqual(list(dialog.choices), ["kills", "recorderkills", "shownkills", "watched"])
        self.assertEqual(dialog.choices["kills"].text(), "The 2 kills of bob or phil with rifle, under a minute")
        self.assertEqual(dialog.choices["recorderkills"].text(), "The 2 recorder frags of bob or phil with rifle, under a minute")
        self.assertEqual(dialog.choices["shownkills"].text(), "The 2 frags shown of bob or phil with rifle: the recorder's "
                         "and the players they follow, under a minute")
        job, = self.done(window)
        self.assertEqual(job["item"].text(1), "first, the 2 recorder frags of bob or phil with rifle")
        video = self.video("rifle.mp4")
        self.assertEqual((video["start"], video["only"], video["player"]), (6000, "kills", "t-"))
        self.assertEqual(video["filter"], {"shown": False, "killers": [], "victims": ["bob", "phil"], "weapons": ["rifle"]})

        # while a killer is watched
        self.choose(window, killer="phil", victim="", weapons=[])

        def watched(dialog):
            dialog.choices["watched"].setChecked(True)
            dialog.pattern.setText("{player} watched")
        dialog = self.record(window, watched)
        self.assertEqual(dialog.choices["watched"].text(), "While phil is watched, about 5 min")
        self.assertEqual(dialog.choices["kills"].text(), "No kills by phil")
        self.assertTrue(dialog.choices["kills"].isHidden())
        self.done(window, 2)
        video = self.video("phil watched.mp4")
        self.assertEqual((video["start"], video["only"], video["player"]), (300000, "watched", ""))
        self.assertEqual(video["filter"]["killers"], ["phil"])

    def test_record_frags_shown(self):
        window = self.window()
        self.playing(window)
        self.choose(window, killer="t-")

        def setup(dialog):
            dialog.choices["shownkills"].setChecked(True)
            dialog.pattern.setText("{demo} frags {player}")
        dialog = self.record(window, setup)
        # the recorder's are the kills by t-
        self.assertEqual(list(dialog.choices), ["kills", "shownkills", "watched"])
        self.assertEqual(dialog.choices["kills"].text(), "The 3 kills by t-, under a minute")
        self.assertEqual(dialog.choices["shownkills"].text(),
                         "The 3 frags shown: the recorder's and the players they follow, under a minute")
        job, = self.done(window)
        self.assertEqual(job["item"].text(1), "first, the 3 frags shown: the recorder's and the players they follow")
        video = self.video("first frags.mp4")
        # from just before the first kill
        self.assertEqual((video["start"], video["only"], video["player"]), (6000, "shownkills", ""))

        # whoever is shown in Killer: its kills are the frags shown
        self.choose(window, killer="", shown=True)

        def shown(dialog):
            dialog.choices["kills"].setChecked(True)
            dialog.pattern.setText("shown")
        dialog = self.record(window, shown)
        self.assertEqual(list(dialog.choices), ["kills", "recorderkills", "watched"])
        self.assertEqual(dialog.choices["kills"].text(),
                         "The 3 frags shown: the recorder's and the players they follow, under a minute")
        self.assertEqual(dialog.choices["watched"].text(), "While a player is watched")
        self.assertFalse(dialog.choices["watched"].isEnabled())
        self.done(window, 2)
        video = self.video("shown.mp4")
        self.assertEqual((video["only"], video["filter"]["shown"]), ("kills", True))

    def test_last_choice_remembered(self):
        window = self.window()
        self.playing(window)
        dialog = self.record(window, lambda d: (d.choices["shownkills"].setChecked(True), d.pattern.setText("a")))
        self.done(window)
        # offered: chosen again, else the first
        dialog = self.record(window, lambda d: d.pattern.setText("b"))
        self.assertTrue(dialog.choices["shownkills"].isChecked())
        self.done(window, 2)
        self.assertEqual(self.video("b.mp4")["only"], "shownkills")
        self.choose(window, killer="phil")
        with mock.patch.object(self.settings, "value", lambda key, default=None, s=self.settings.value: (
                "watched" if key == "rec/what" else s(key, default))):
            dialog = mohreplay.RecordDialog(self.settings, [("watched", "While Phil is watched", False)], 0)
        self.assertTrue(dialog.range.isChecked())

    def test_progress(self):
        window = self.window()
        job = {"clips": [{"spans": [(0, 10000)]}, {"spans": [(5000, 15000), (20000, 30000)]}]}
        recorder = mohreplay.Recorder(window, job, None, None)
        recorder.clip = 1
        with mock.patch.object(recorder.elapsed, "elapsed", lambda: 30000):
            # 10 s of the first clip and 7 of the second, in 30 s
            self.assertEqual(recorder.how_far(12000), "56%, under a minute left")
            self.assertEqual(recorder.how_far(5000), "33%, about 1 min left")
        job["clips"][0]["spans"] = None
        self.assertEqual(recorder.how_far(12000), "0:12")

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
        for row in (0, 1, 3):
            window.kills.topLevelItem(row).setSelected(True)

        def setup(dialog):
            dialog.join.setChecked(True)
            dialog.pattern.setText("selected")
        dialog = self.record(window, setup, lambda: window.record_selected(window.kills))
        self.assertEqual(dialog.choices["selected"].text(), "The 3 kills selected, under a minute")
        self.assertTrue(dialog.join.isVisibleTo(dialog))
        job, = self.done(window)
        self.assertEqual(job["status"], "done", job["item"].toolTip(2))
        parts = self.video("selected.mp4")["joined"]
        self.assertEqual([(p["start"], p["end"]) for p in parts], [(6000, 14000), (246000, 252000)])

    def test_team_kills_arent_kills(self):
        self.add_demo("first", team_demo())
        window = self.window()
        self.playing(window)
        # neither listed nor marked
        rows = [window.kills.topLevelItem(i) for i in range(window.kills.topLevelItemCount())]
        self.assertEqual([row.text(0) for row in rows], ["0:10", "0:12", "0:14", "4:10"])
        self.assertEqual([mark[0] for mark in window.slider.kills], [10000, 12000, 14500, 250000])
        # but the one 1.5 s after t-'s kill at 0:14 is in its video

        def setup(dialog):
            dialog.pattern.setText("selected")
        rows[2].setSelected(True)
        dialog = self.record(window, setup, lambda: window.record_selected(window.kills))
        self.assertEqual(dialog.choices["selected"].text(), "The kill selected, under a minute")
        self.done(window)
        video = self.video("selected.mp4")
        self.assertEqual((video["start"], video["end"]), (10500, 18000))

    def test_selected_kills_one_video_each(self):
        window = self.window()
        self.playing(window)
        for row in (0, 3):
            window.kills.topLevelItem(row).setSelected(True)

        def setup(dialog):
            dialog.join.setChecked(False)
            dialog.pattern.setText("kill {start}")
        self.record(window, setup, lambda: window.record_selected(window.kills))
        self.done(window, 2)
        self.assertEqual(sorted(os.listdir(self.videos)), ["kill 0-06.mp4", "kill 4-06.mp4"])
        self.assertEqual(self.settings.value("rec/join"), "false")

    def test_selected_kills_cut_where_the_player_shown_changes(self):
        demo = json.loads(json.dumps(DEMO))
        # t- kills 0.8 s apart, then 0.5 s on each side of half a second of Phil
        demo["watched"] = [{"time": 0, "client": 0, "name": "t-"}, {"time": 40000, "client": 1, "name": "^1Phil"},
                           {"time": 40500, "client": 0, "name": "t-"}]
        demo["kills"] = [{"time": time, "killer": 0, "killerName": "t-", "victim": 1, "victimName": "^1Phil",
                          "text": "Phil was rifled by t-"} for time in (20000, 26800, 38000, 44000)]
        self.add_demo("cut", demo)
        window = self.window()
        self.playing(window, "cut")
        for name, killer, shown, clips in (("shown", "", True, [(16000, 28800), (34000, 40000), (40500, 46000)]),
                                           ("t-", "t-", False, [(16000, 28800), (34000, 40000), (40500, 46000)]),
                                           ("all", "", False, [(16000, 28800), (34000, 46000)])):
            self.choose(window, killer=killer, shown=shown)
            for row in range(window.kills.topLevelItemCount()):
                window.kills.topLevelItem(row).setSelected(True)

            def setup(dialog, name=name):
                dialog.join.setChecked(True)
                dialog.pattern.setText(name)
            self.record(window, setup, lambda: window.record_selected(window.kills))
            self.done(window, len(window.jobs))
            parts = self.video(name + ".mp4")["joined"]
            self.assertEqual([(p["start"], p["end"]) for p in parts], clips, name)

    def test_join_failing(self):
        window = self.window()
        self.playing(window)
        for row in (0, 3):
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
        for name in ("first", "other", "none"):
            window.demo_items[name].setSelected(True)
        # the search finds t- in the three
        window.filter.setText("t-")
        self.assertEqual(sorted(window.demos_of(window.demos.selectedItems())), ["first", "none", "other"])
        # t-'s kills, in two
        self.choose(window, killer="t-")
        self.assertEqual(TestWindow.shown(self, window), ["first", "other"])
        self.assertEqual(window.demo_items["other"].text(mohreplay.COL_PLAYER), "^2T- (3)")

        def setup(dialog):
            dialog.choices["kills"].setChecked(True)
            dialog.join.setChecked(True)
            dialog.pattern.setText("{player} in {demo}")
        dialog = self.record(window, setup, window.record_demos)
        self.assertEqual(dialog.choices["kills"].text(), "The 6 kills by t-, under a minute")
        job, = self.done(window)
        self.assertEqual(job["status"], "done", job["item"].toolTip(2))
        self.assertEqual(job["item"].text(1), "2 demos, kills by t-")
        parts = self.video("t- in 2 demos.mp4")["joined"]
        # a part of the name, whatever each demo's spelling
        self.assertEqual([(p["only"], p["player"], p["filter"]["killers"]) for p in parts],
                         [("kills", "", ["t-"]), ("kills", "", ["t-"])])

    def test_frags_shown_in_several_demos(self):
        self.add_demo("followed", followed_demo())
        # t-, shown till 5:00, kills only after
        self.add_demo("unseen", dict(DEMO, kills=[k for k in DEMO["kills"] if k["time"] > 100000]))
        window = self.window()
        TestLibrary.indexed(self, window, ("first", "followed", "unseen"))
        window.filter.setText("t-")
        for name in ("first", "followed", "unseen"):
            window.demo_items[name].setSelected(True)

        def setup(dialog):
            dialog.choices["shownkills"].setChecked(True)
            dialog.join.setChecked(True)
            dialog.pattern.setText("{demo} {player}")
        dialog = self.record(window, setup, window.record_demos)
        self.assertEqual(dialog.choices["kills"].text(), "The 11 kills, about 1 min")
        self.assertEqual(dialog.choices["shownkills"].text(), "The 8 frags shown: the recorder's and the players "
                         "they follow, in 2 of the 3 demos, under a minute")
        job, = self.done(window)
        self.assertEqual(job["status"], "done", job["item"].toolTip(2))
        self.assertEqual(job["item"].text(1), "2 demos, frags shown")
        # whoever is shown, not a killer
        parts = self.video("2 demos.mp4")["joined"]
        self.assertEqual([(p["only"], p["player"]) for p in parts], [("shownkills", ""), ("shownkills", "")])

        def each(dialog):
            dialog.choices["shownkills"].setChecked(True)
            dialog.join.setChecked(False)
            dialog.pattern.setText("{demo} each")
        self.record(window, each, window.record_demos)
        jobs = self.done(window, 3)
        self.assertEqual(sorted(job["item"].text(1) for job in jobs[1:]),
                         ["first, frags shown", "followed, frags shown"])
        self.assertEqual(sorted(n for n in os.listdir(self.videos) if "each" in n),
                         ["first each.mp4", "followed each.mp4"])
        self.assertEqual(self.video("followed each.mp4")["only"], "shownkills")

        # whoever is shown in Killer, with a weapon: those of the demos listed
        self.choose(window, shown=True, weapons=["sniper"])
        self.assertEqual(TestWindow.shown(self, window), ["followed"])
        self.assertEqual(window.demo_items["followed"].text(mohreplay.COL_PLAYER), "^1Phil (1)")
        window.demos.clearSelection()
        window.demo_items["followed"].setSelected(True)

        def sniper(dialog):
            dialog.pattern.setText("{demo} sniper")
        dialog = self.record(window, sniper, window.record_demos)
        self.assertEqual(list(dialog.choices), ["kills", "recorderkills", "everything"])
        self.assertEqual(dialog.choices["kills"].text(), "The frag shown with sniper: the recorder's and the players "
                         "they follow, under a minute")
        self.assertTrue(dialog.choices["kills"].isChecked())
        self.assertFalse(dialog.choices["recorderkills"].isEnabled())
        self.done(window, 4)
        video = self.video("followed sniper.mp4")
        self.assertEqual((video["only"], video["filter"]["shown"], video["filter"]["weapons"]), ("kills", True, ["sniper"]))

    def test_record_demos_recorder_frags(self):
        self.add_demo("followed", followed_demo())
        self.add_demo("unseen", dict(DEMO, kills=[k for k in DEMO["kills"] if k["time"] > 100000]))
        window = self.window()
        TestLibrary.indexed(self, window, ("first", "followed", "unseen"))
        window.filter.setText("t-")
        for name in ("first", "followed", "unseen"):
            window.demo_items[name].setSelected(True)

        def setup(dialog):
            dialog.choices["recorderkills"].setChecked(True)
            dialog.join.setChecked(True)
            dialog.pattern.setText("{demo} {player}")
        dialog = self.record(window, setup, window.record_demos)
        self.assertEqual(dialog.choices["kills"].text(), "The 11 kills, about 1 min")
        self.assertEqual(dialog.choices["recorderkills"].text(),
                         "The 6 recorder frags, in 2 of the 3 demos, under a minute")
        job, = self.done(window)
        self.assertEqual(job["status"], "done", job["item"].toolTip(2))
        self.assertEqual(job["item"].text(1), "2 demos, recorder frags")
        # only the recorder's kills, excluding followed players
        parts = self.video("2 demos.mp4")["joined"]
        self.assertEqual([(p["only"], p["player"]) for p in parts], [("kills", "t-"), ("kills", "t-")])

        def each(dialog):
            dialog.choices["recorderkills"].setChecked(True)
            dialog.join.setChecked(False)
            dialog.pattern.setText("{demo} each")
        self.record(window, each, window.record_demos)
        jobs = self.done(window, 3)
        self.assertEqual(sorted(job["item"].text(1) for job in jobs[1:]),
                         ["first, recorder frags", "followed, recorder frags"])
        self.assertEqual(sorted(n for n in os.listdir(self.videos) if "each" in n),
                         ["first each.mp4", "followed each.mp4"])
        self.assertEqual(self.video("followed each.mp4")["only"], "kills")
        self.assertEqual(self.video("followed each.mp4")["player"], "t-")

    def test_whole_demos_one_video_each(self):
        # short, to record quickly, so listed anyway
        self.patches.append(mock.patch.object(mohreplay, "MIN_LENGTH", 0))
        self.patches[-1].start()
        self.add_demo("second", dict(DEMO, duration=30000))
        window = self.window()
        TestLibrary.indexed(self, window, ("first", "second"))
        window.demo_items["second"].setSelected(True)

        def setup(dialog):
            dialog.choices["everything"].setChecked(True)
            dialog.pattern.setText("{demo}")
        dialog = self.record(window, setup, window.record_demos)
        self.assertEqual(dialog.choices["everything"].text(), "All of the demo, under a minute")
        self.assertEqual(dialog.choices["kills"].text(), "The 4 kills, under a minute")
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


def game_folder(real, folder):
    """A game folder of its own in folder, with links to the files of real
    but none to its settings, which the game watched writes there."""
    os.makedirs(os.path.join(folder, "main"))
    for name in os.listdir(real):
        if name != "main":
            os.symlink(os.path.join(real, name), os.path.join(folder, name))
    for name in os.listdir(os.path.join(real, "main")):
        if name not in ("configs", "omconfig.cfg"):
            os.symlink(os.path.join(real, "main", name), os.path.join(folder, "main", name))
    return folder


@unittest.skipUnless(REAL_GAME and REAL_DEMOS, "set MOHREPLAY_TEST_GAME and MOHREPLAY_TEST_DEMOS")
class TestRealGame(FakeGameCase):
    """Plays and records a real demo with the real game, without a window."""

    env = {}

    def setUp(self):
        super().setUp()
        exe = os.environ.get("MOHREPLAY_TEST_EXE") or os.path.join(mohreplay.REPO, ".cmake", "RelWithDebInfo", "openmohaa")
        self.settings.setValue("exe", exe)
        self.settings.setValue("game", game_folder(REAL_GAME, os.path.join(self.tmp, "real")))
        self.settings.setValue("demos", REAL_DEMOS)
        self.settings.setValue("size", "640x360")
        self.demo = os.environ.get("MOHREPLAY_TEST_DEMO") or "d482684ec556d1c3-obj-obj_team1"
        # quiet: the recorder's own volume comes after
        start = mohreplay.Game.start

        def quiet(game, exe, game_dir, demos, width, height, extra=(), driver=None, **kwargs):
            return start(game, exe, game_dir, demos, width, height, ("+set", "s_volume", "0", *extra), driver,
                         **kwargs)
        patch = mock.patch.object(mohreplay.Game, "start", quiet)
        patch.start()
        self.addCleanup(patch.stop)

    def test_frags_shown(self):
        """Only these kills of whoever is shown plays inside their clips."""
        window = self.window()
        window.play_demo(self.demo)
        self.assertTrue(wait_until(lambda: window.state.get("demo") == self.demo and window.index, 60))
        self.choose(window, shown=True)
        self.assertEqual(window.kills.topLevelItemCount(),
                         len([k for k, _, _ in mohreplay.shown_kills(window.index)
                              if not k.get("teamKill") and not mohreplay.is_grenade(k)]))
        rows = [window.kills.topLevelItem(i) for i in range(window.kills.topLevelItemCount())]
        clips = mohreplay.stretches([span for row in rows for span in row.data(1, Qt.UserRole)],
                                    [w["time"] for w in window.index["watched"]])
        # from the kill before the first one by a player the recorder follows
        recorder = window.index["recorder"]["name"]
        followed = next(i for i, row in enumerate(rows) if not mohreplay.same_player(row.text(1), recorder))
        window.kills.setCurrentItem(rows[max(0, followed - 1)])
        window.speed.setCurrentText("4×")
        window.only_kills()
        samples, end = [], time.monotonic() + 20
        while time.monotonic() < end:
            app.processEvents()
            state = window.state
            if state.get("only") == "kills" and not state.get("seeking") and not state.get("paused"):
                samples.append(state["time"])
            time.sleep(0.02)
        self.assertGreater(len(set(samples)), 20)
        # a frame may be drawn past a clip before demoonly jumps
        self.assertEqual([t for t in samples if not any(s - 150 <= t <= e + 150 for s, e in clips)], [])
        start, stop = rows[followed].data(1, Qt.UserRole)[0]
        self.assertTrue(any(start <= t <= stop for t in samples), "the followed player's kill wasn't played")

    def test_kill_filter(self):
        """demonextkill goes where each kill listed starts, with the filter's
        killers, victims and weapons."""
        window = self.window()
        window.play_demo(self.demo)
        self.assertTrue(wait_until(lambda: window.state.get("demo") == self.demo and window.index, 60))
        window.game.send("demopause 1")
        index = window.index
        frags = [k for k in index["kills"] if not k.get("teamKill") and not k.get("grenade")]
        self.assertTrue(all(k.get("weapon") for k in frags), "kills without a weapon")

        def most(names):
            return max(set(names), key=names.count)
        recorder = index["recorder"]["name"]
        weapon = most([mohreplay.weapon_of(k) for k in frags if mohreplay.same_player(k["killerName"], recorder)])
        # killed by whoever is shown
        victim = most([k["victimName"] for k, _, _ in mohreplay.shown_kills(index) if k in frags and k["victimName"]
                       and not mohreplay.same_player(k["victimName"], k["killerName"])])
        cases = ((dict(killer=recorder[:3], weapons=[weapon]), ), (dict(shown=True, victim=victim), ),
                 (dict(weapons=["mg", "sniper"]), ))
        for case, in cases:
            self.choose(window, **{"killer": "", "victim": "", "weapons": [], "shown": False, **case})
            starts = [window.kills.topLevelItem(i).data(0, Qt.UserRole) for i in range(window.kills.topLevelItemCount())]
            self.assertTrue(starts, case)
            # as demonextkill goes, from the start: the next more than 0.1 s on
            wanted, now = [], 0
            for start in starts:
                if start > now + 100:
                    wanted.append(start)
                    now = start
            wanted = wanted[:8]
            window.game.send("demoseek 0")
            self.assertTrue(wait_until(lambda: window.state.get("time", 1) < 500 and not window.state.get("seeking"), 30))
            landed = []
            for _ in wanted:
                last = window.state["time"]
                window.game.send("demonextkill")
                # each more than 0.1 s on
                self.assertTrue(wait_until(lambda: not window.state.get("seeking") and window.state["time"] > last + 100, 30),
                                (case, landed))
                landed.append(window.state["time"])
            # the game's time comes in snapshots
            self.assertEqual(len(landed), len(wanted))
            self.assertTrue(all(abs(a - b) <= 100 for a, b in zip(landed, wanted)), (case, landed, wanted))

    def test_play_seek_and_record(self):
        window = self.window()
        window.play_demo(self.demo)
        self.assertTrue(wait_until(lambda: window.state.get("demo") == self.demo and window.index, 60))
        self.assertGreater(window.kills.topLevelItemCount(), 0)
        window.game.send("demoseek 2:00")
        self.assertTrue(wait_until(lambda: 120000 <= window.state.get("time", 0) < 125000
                                   and not window.state.get("seeking"), 30))

        self.settings.setValue("rec/size", "640x360")

        def setup(dialog):
            dialog.start.setText("2:00")
            dialog.end.setText("2:03")
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

    def test_record_with_motion_blur(self):
        """The renderer blurs, the game drawing the video's 60 frames a
        second."""
        window = self.window()
        window.play_demo(self.demo)
        self.assertTrue(wait_until(lambda: window.state.get("demo") == self.demo and window.index, 60))

        for key, value in (("size", "640x360"), ("fps", "60"), ("blur", "true")):
            self.settings.setValue("rec/" + key, value)

        def setup(dialog):
            dialog.start.setText("2:00")
            dialog.end.setText("2:03")
            dialog.pattern.setText("blur")
        with mock.patch.object(mohreplay.RecordDialog, "exec", lambda d: (setup(d), d.accept(), d.result())[-1]):
            window.record()
        output = os.path.join(self.videos, "blur.mp4")
        self.assertTrue(wait_until(lambda: window.jobs[0]["status"] not in ("waiting", "recording"), 120), "no video")
        self.assertEqual(window.jobs[0]["status"], "done", window.jobs[0]["item"].toolTip(2))
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-count_frames", "-show_entries",
                                "stream=r_frame_rate,nb_read_frames,duration", "-of", "json", output],
                               capture_output=True, text=True, check=True)
        video, = json.loads(probe.stdout)["streams"]
        self.assertEqual(video["r_frame_rate"], "60/1")
        self.assertAlmostEqual(int(video["nb_read_frames"]), 180, delta=2)
        self.assertAlmostEqual(float(video["duration"]), 3, delta=0.1)


if __name__ == "__main__":
    unittest.main()
