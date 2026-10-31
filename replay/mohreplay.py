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
import csv
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
    QSettings, QStandardPaths, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDockWidget, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProxyStyle, QPushButton, QRadioButton, QSizePolicy, QSlider, QSplitter, QStyle,
    QStyleOptionSlider, QTabWidget, QToolBar, QToolButton, QToolTip, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QWidgetAction,
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
# what Record… offers for whoever is shown, as demoonly shownkills: one and
# several
SHOWN_KILLS = ("frag shown", "frags shown")
SHOWN_WHO = ": the recorder's and the players they follow"
# what Record… offers for the recorder's own frags
RECORDER_KILLS = ("recorder frag", "recorder frags")
# what demoonly plays for each of Record…'s choices of kills
ONLY = {"kills": "kills", "recorderkills": "kills", "shownkills": "shownkills", "watched": "watched"}
# the weapons kills are filtered by, as the demo index names them, and in
# words, but grenades, whose kills aren't kills; other is anything else,
# and the death messages not known
WEAPONS = {"sniper": "Sniper", "rifle": "Rifle", "smg": "SMG", "mg": "StG / BAR", "shotgun": "Shotgun",
           "pistol": "Pistol", "rocket": "Bazooka", "melee": "Melee", "other": "Other"}
WEAPON_WORDS = {"sniper": "sniper", "rifle": "rifle", "smg": "SMG", "mg": "StG / BAR", "shotgun": "shotgun",
                "pistol": "pistol", "rocket": "bazooka", "melee": "melee", "other": "other weapons"}
WEAPON_TIPS = {"sniper": "Springfield and KAR98 sniper: \"was sniped by\"",
               "rifle": "M1 Garand and KAR98: \"was rifled by\"",
               "smg": "Thompson and MP40: \"was perforated by\"",
               "mg": "StG 44 and BAR, the game's machine guns: \"was machine-gunned by\"",
               "shotgun": "\"was hunted down by\", \"was pumped full of buckshot by\"",
               "pistol": "Colt 45, P38 and the silenced pistol: \"was gunned down by\"",
               "rocket": "Bazooka and panzerschreck: \"took …'s rocket\"",
               "melee": "Hit with a gun's butt: \"was bashed by\", \"was clubbed by\"",
               "other": "Explosions, falls, being crushed…, and death messages not known"}
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
# with motion blur, the renderer's: what moves blurs over all of a frame's
# time, at any frame rate, and the colors are as without it (r_temporal
# would make them stronger)
BLUR_CVARS = ["+set", "r_temporal", "1", "+set", "r_motionBlur", "1", "+set", "r_motionBlurShutter", "1",
              "+set", "r_motionBlurFps", "0", "+set", "r_saturation", "1"]


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


def without_colors(name):
    """name without its colour codes, ^ and a letter or digit."""
    out, i = [], 0
    while i < len(name):
        if name[i] == "^" and i + 1 < len(name) and name[i + 1].isascii() and name[i + 1].isalnum():
            i += 2
            continue
        out.append(name[i])
        i += 1
    return "".join(out)


@functools.lru_cache(maxsize=4096)
def clean_name(name):
    """The engine's rule for names: without ^ and a letter or digit, only
    printable ASCII, any case (Q_CleanStr), spaces as one."""
    printable = "".join(c for c in without_colors(name) if " " <= c <= "~")
    return " ".join(word for word in printable.lower().split(" ") if word)


# what the names of the objective maps' levels have, any case (obj/obj_team4,
# OBJ/obj_Team2, obj/harbor...), and not those of the deathmatch maps (dm/)
OBJ_LEVELS = "obj/"
# what Great frags leaves out: sniper frags seen from their fragger's
# view with the scope on for longer than this, in msec, and the fragger at
# this speed or slower, the slow scoped shots of someone standing still,
# but not those of a streak: with another frag of the fragger at most
# GREAT_STREAK msec before or after, as one clip shows them; and the frags
# of victims who had given no input for over GREAT_IDLE msec, AFK
GREAT_SCOPE, GREAT_SPEED, GREAT_STREAK, GREAT_IDLE = 1000, 30, 5000, 5000


def kill_levels(index):
    """Gives each kill of index the name of its level, "level", lower case:
    the last one loaded before it, "" before the first, as the engine looks
    it up. Returns index."""
    maps = index.get("maps", [])
    times = [m["time"] for m in maps]
    for kill in index.get("kills", []):
        i = bisect.bisect_right(times, kill["time"]) - 1
        kill["level"] = maps[i]["map"].lower() if i >= 0 else ""
    return index


def kill_streaks(index):
    """Tells each kill of index whether it's in a streak, "streak": with
    another kill of its killer at most GREAT_STREAK msec before or after it,
    as the engine looks them up (CL_DemoInMultiKill); team kills, grenade
    kills, the deaths of the player shown and the kills of idle victims
    don't count. Returns index."""
    deaths = deaths_shown(index)

    def counts(kill):
        return (kill["killerName"] and not kill.get("teamKill") and not is_grenade(kill) and id(kill) not in deaths
                and not idle_victim(kill))
    times = {}
    for kill in index.get("kills", []):
        if counts(kill):
            times.setdefault(clean_name(kill["killerName"]), []).append(kill["time"])
    for when in times.values():
        when.sort()
    for kill in index.get("kills", []):
        when = times.get(clean_name(kill["killerName"]), []) if kill["killerName"] and not is_grenade(kill) else []
        t = kill["time"]
        near = bisect.bisect_right(when, t + GREAT_STREAK) - bisect.bisect_left(when, t - GREAT_STREAK)
        # other than itself
        kill["streak"] = near > (1 if counts(kill) else 0)
    return index


def slow_snipe(kill):
    """Whether kill is one Great frags leaves out: a sniper frag its
    index saw from the fragger's view, made with the scope on for over
    GREAT_SCOPE msec at GREAT_SPEED or slower, not in a streak (given by
    kill_streaks). Not one when not seen, as in an index made before scopes
    were."""
    return (weapon_of(kill) == "sniper" and kill.get("scope", -1) > GREAT_SCOPE and kill.get("speed", 0) <= GREAT_SPEED
            and not kill.get("streak"))


def idle_victim(kill):
    """Whether kill's victim had given no input for over GREAT_IDLE msec
    before it, AFK, as its index saw them. Not when not seen."""
    return kill.get("idle", -1) > GREAT_IDLE


def poor_frag(kill):
    """Whether Great frags leaves kill out: a slow snipe or the frag of an
    idle victim."""
    return slow_snipe(kill) or idle_victim(kill)


def same_player(a, b):
    return clean_name(a) == clean_name(b)


def is_grenade(kill):
    """Whether kill was made with a grenade or shrapnel: as its index tells,
    or its message for an index made before weapons were."""
    if "weapon" in kill:
        return bool(kill.get("grenade"))
    return bool(kill.get("grenade")) or "grenade" in kill.get("text", "").lower() or "shrapnel" in kill.get("text", "").lower()


def weapon_of(kill):
    """What kill was made with, a key of WEAPONS: other for what isn't
    known, as in indexes made before weapons were."""
    weapon = kill.get("weapon", "")
    return weapon if weapon in WEAPONS else "other"


def or_list(words):
    """"a, b or c"."""
    words = list(words)
    return ", ".join(words[:-1]) + " or " + words[-1] if len(words) > 1 else "".join(words)


class KillFilter:
    """The kills listed, played and recorded: by players whose names have one
    of killers in them, while they're shown, as a player's kills are, or by
    whoever is shown (shown), of players whose names have one of victims,
    made with one of weapons (keys of WEAPONS), on the objective maps only if
    obj, great ones only if great (not slow snipes nor frags of AFK
    players). Names are parts of
    names as the search takes its words; none is anyone, no weapons any
    weapon. The game is told it with demofilter."""

    def __init__(self, killers=(), victims=(), weapons=(), shown=False, obj=False, great=False):
        self.shown = bool(shown)
        self.obj = bool(obj)
        self.great = bool(great)
        # as given, for the game and the labels; whoever is shown, or names
        self.names = [] if shown else [n.strip() for n in killers if clean_name(n)]
        self.victim_names = [n.strip() for n in victims if clean_name(n)]
        self.killers = [clean_name(n) for n in self.names]
        self.victims = [clean_name(n) for n in self.victim_names]
        self.weapons = [w for w in WEAPONS if w in weapons]

    def __bool__(self):
        return bool(self.shown or self.killers or self.victims or self.weapons or self.obj or self.great)

    def by_shown(self):
        """Whether its kills are only those made while their killer is shown."""
        return self.shown or bool(self.killers)

    def has_killer(self, killer):
        """Whether a killer's clean name is one of its, anyone if whoever is
        shown."""
        return not self.killers or any(part in killer for part in self.killers)

    def has(self, victim, weapon, level="", poor=False):
        """Whether a kill of a victim (clean name) made with weapon in a level
        (lower case), poor (poor_frag) or not, is one of its, whoever the
        killer."""
        return ((not self.weapons or weapon in self.weapons)
                and (not self.victims or any(part in victim for part in self.victims))
                and (not self.obj or OBJ_LEVELS in level) and not (self.great and poor))

    def keeps(self, kill, killer=True):
        """Whether kill, given its level (kill_levels), is one of its, whoever
        the killer unless killer."""
        return ((not killer or self.has_killer(clean_name(kill["killerName"])))
                and self.has(clean_name(kill.get("victimName", "")), weapon_of(kill), kill.get("level", ""),
                             poor_frag(kill)))

    def commands(self):
        """The demofilter commands telling it to the game."""
        return (["demofilter clear"] + (["demofilter shown"] if self.shown else [])
                + ["demofilter killer " + quoted(name) for name in self.names]
                + ["demofilter victim " + quoted(name) for name in self.victim_names]
                + (["demofilter weapon " + " ".join(self.weapons)] if self.weapons else [])
                + (["demofilter level " + OBJ_LEVELS] if self.obj else [])
                + ([f"demofilter great {GREAT_SCOPE} {GREAT_SPEED} {GREAT_STREAK} {GREAT_IDLE}"] if self.great else []))

    def words(self, killers=True):
        """What it keeps after "kills", the killers too if killers: " by
        pstn or Phil of Bob with sniper", "" for all."""
        text = " by " + or_list(self.names) if killers and self.names else ""
        text += " of " + or_list(self.victim_names) if self.victim_names else ""
        text += " with " + or_list(WEAPON_WORDS[w] for w in self.weapons) if self.weapons else ""
        return text + (" on obj maps" if self.obj else "") + (", great ones" if self.great else "")


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
    they're shown, and its kills for the kill filter: [(killer, clean
    killer, clean victim, weapon, whether made while the killer is shown,
    level, whether poor (poor_frag))]."""
    kill_streaks(kill_levels(index))
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
    shown = {id(kill) for kill, _, _ in shown_kills(index)}
    kills = [kill for kill in index.get("kills", [])
             if not kill.get("teamKill") and not is_grenade(kill) and id(kill) not in deaths]
    for kill in kills:
        if id(kill) in shown:
            add(kill["killerName"], 1)
    # "realism" or "default" when its levels tell one, "both", or ""
    kinds = {m.get("rules") for m in index.get("maps", [])} & {"realism", "default"}
    rules = "both" if len(kinds) == 2 else kinds.pop() if kinds else ""
    return {"maps": [m["map"] for m in index.get("maps", [])], "duration": index.get("duration", 0),
            "recorder": index.get("recorder", {}).get("name", ""), "players": players, "rules": rules,
            "kills": [(kill["killerName"], clean_name(kill["killerName"]), clean_name(kill.get("victimName", "")),
                       weapon_of(kill), id(kill) in shown, kill["level"], poor_frag(kill)) for kill in kills]}


def filter_kills(summary, kill_filter):
    """The kills of a demo the filter keeps, from its summary: how many, and
    their killers, {clean name: [name, kills]}."""
    count, killers = 0, {}
    by_shown = kill_filter.by_shown()
    for name, killer, victim, weapon, shown, level, poor in summary.get("kills", []):
        if (shown or not by_shown) and kill_filter.has_killer(killer) and kill_filter.has(victim, weapon, level, poor):
            count += 1
            if name:
                killers.setdefault(killer, [name, 0])[1] += 1
    return count, killers


def search_parts(text):
    """The searches of the demo filter: the words of each part separated by
    commas, cleaned like names. A demo is found by any search with all of its
    words."""
    parts = ([w for w in (clean_name(w) for w in part.split()) if w] for part in text.split(","))
    return [words for words in parts if words]


def search_demo(name, summary, searches):
    """The players of a demo the searches find, {clean name: [name, kills]},
    or None when no search has all its words in the demo's name, a level or a
    player's name."""
    players = summary.get("players", {})
    maps = " ".join(summary.get("maps", [])).lower()
    found, any_found = {}, False
    for words in searches:
        hits = {}
        for word in words:
            if word in name.lower() or word in maps:
                continue
            keys = [key for key in players if word in key]
            if not keys:
                break
            hits.update((key, players[key]) for key in keys)
        else:
            any_found = True
            found.update(hits)
    return found if any_found else None


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


class StayOpenMenu(QMenu):
    """A menu whose checkable actions are ticked without closing it."""

    def mouseReleaseEvent(self, event):
        action = self.activeAction()
        if action and action.isCheckable() and action.isEnabled():
            action.trigger()
            return
        super().mouseReleaseEvent(event)


class NamesField(QLineEdit):
    """Parts of players' names, separated by commas, anyone if none. Its menu
    adds or takes off players (those of the demo playing by default, with
    labels if given), in a list that scrolls, and whoever is shown instead
    of names, if offered."""

    edited = Signal()
    ROWS = 15  # players the menu shows at once: a long list fits the screen

    def __init__(self, tip, offer_shown=False, menu_tip="The players of the demo playing",
                 empty="Play a demo to choose its players"):
        super().__init__(placeholderText="Anyone")
        self.setClearButtonEnabled(True)
        self.setToolTip(tip)
        self.players = []
        self.labels = {}  # player: what the menu shows
        self.empty = empty
        self.menu = StayOpenMenu(self)
        self.shown_action = None
        if offer_shown:
            self.shown_action = QAction("Whoever is shown", self, checkable=True)
            self.shown_action.setToolTip("The recorder's frags, and those of the players they follow while they do")
            self.shown_action.toggled.connect(self.shown_toggled)
        menu_action = self.addAction(theme_icon("go-down", QStyle.SP_ArrowDown), QLineEdit.TrailingPosition)
        menu_action.setToolTip(menu_tip)
        menu_action.triggered.connect(self.open_menu)
        self.textChanged.connect(self.text_changed)

    def names(self):
        return [part.strip() for part in self.text().split(",") if clean_name(part)]

    def shown(self):
        return bool(self.shown_action and self.shown_action.isChecked())

    def set_shown(self, shown):
        if self.shown_action:
            self.shown_action.setChecked(shown)

    def text_changed(self, text):
        # names instead of whoever is shown
        if text.strip() and self.shown():
            self.shown_action.setChecked(False)
        self.edited.emit()

    def shown_toggled(self, checked):
        self.setPlaceholderText("Whoever is shown" if checked else "Anyone")
        if checked and self.text():
            self.blockSignals(True)
            self.clear()
            self.blockSignals(False)
        self.edited.emit()

    def open_menu(self):
        self.menu.clear()
        if self.shown_action:
            self.menu.addAction(self.shown_action)
            self.menu.addSeparator()
        if self.players:
            self.menu.addAction(self.player_list())
        else:
            self.menu.addAction(self.empty).setEnabled(False)
        self.menu.popup(self.mapToGlobal(self.rect().bottomLeft()))

    def player_list(self):
        """The players, ticked if named, as a menu's action."""
        chosen = {clean_name(name) for name in self.names()}
        self.list = QListWidget()
        self.list.setFrameShape(QListWidget.NoFrame)
        for player in self.players:
            item = QListWidgetItem(self.labels.get(player, player))
            item.setData(Qt.UserRole, player)
            # ticked by a click anywhere on it, not only on its box
            item.setFlags(Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked if clean_name(player) in chosen else Qt.Unchecked)
            self.list.addItem(item)
        self.list.itemClicked.connect(self.list_clicked)
        self.list.setFixedHeight(min(len(self.players), self.ROWS) * self.list.sizeHintForRow(0))
        self.list.setMinimumWidth(self.list.sizeHintForColumn(0) + self.list.verticalScrollBar().sizeHint().width())
        action = QWidgetAction(self.menu)
        action.setDefaultWidget(self.list)
        return action

    def list_clicked(self, item):
        checked = item.checkState() != Qt.Checked
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self.toggle(item.data(Qt.UserRole), checked)

    def toggle(self, player, checked):
        names = [name for name in self.names() if clean_name(name) != clean_name(player)]
        self.setText(", ".join(names + [player] if checked else names))


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
    size, frames per second, sound, motion blur, viewmodel, models forced
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
    width, height = parse_size(settings.value("rec/size", "1920x1080")) or (1920, 1080)
    return {"options": options, "width": width, "height": height, "fps": str(int(settings.value("rec/fps", "60"))),
            "sound": sound, "blur": settings.value("rec/blur", "false") == "true",
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
        self.blur.setToolTip("What moves blurs over each frame's time, as with a camera: "
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
        self.kill_before = seconds_box(settings, "kill_before", 4.0, "Time shown before each frag (cl_demoKillBefore)")
        self.kill_after = seconds_box(settings, "kill_after", 2.0, "Time shown after each frag (cl_demoKillAfter)")
        form.addRow("Each frag plays", lead_row(self.kill_before, self.kill_after, "it"))
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


def only_clip(demo, only="", player="", spans=None, kill_filter=None):
    """A clip of what demoonly plays ("kills", "shownkills" or "watched")
    with kill_filter, or of all of demo; spans: what that is, from
    demoonly_plan(), if known."""
    command = f"demoonly {only}" + (" " + quoted(player) if player else "") if only else "demoonly"
    start = spans[0][0] if (spans and spans[0][0] > 0) else 0
    told = (kill_filter or KillFilter()).commands() if only else []
    return {"demo": demo, "start": start, "end": None, "spans": spans,
            "commands": [*told, f"demoseek {seconds(start)}", "demopause 0", command, "demovideo {part}"]}


def numbered(clips, numbers):
    """The clips of a video, with the number the game draws on the first of
    each (cl_demoClipNumbers), the next ones counting on through the video,
    or none: a clip has one for each stretch it plays."""
    number = 1
    for clip in clips:
        clip["number"] = number if numbers else 0
        clip["commands"] = [f"set cl_demoClipNumbers {clip['number']}", *clip["commands"]]
        number += len(clip["spans"] or [None])
    return clips


CLIP_LIST = ["Clip", "Video", "Demo", "Time", "Fragger", "Victim", "Weapon"]


def clip_rows(clips):
    """The list of a video's numbered clips, as CLIP_LIST: each one's number,
    start in the video and demo, and the time, fragger, victim and weapon of
    each of its frags, a row each (one without frags for a clip with none).
    The start in the video is the planned one, unknown after a demo not
    indexed."""
    rows, video = [], 0
    for clip in clips:
        spans = clip["spans"] or [(clip["start"], None)]
        frags = clip.get("frags") or [[] for _ in spans]
        for i, ((start, end), kills) in enumerate(zip(spans, frags)):
            at = clock(video) if video is not None else ""
            for kill in kills or [None]:
                rows.append([clip["number"] + i, at, clip["demo"], clock(kill["time"] if kill else start)]
                            + ([without_colors(kill["killerName"]), without_colors(kill["victimName"]),
                                WEAPONS[weapon_of(kill)]] if kill else ["", "", ""]))
            video = video + end - start if video is not None and end is not None else None
    return rows


def write_clip_list(clips, video):
    """The list of the numbered clips of video, next to it: video.csv."""
    with open(os.path.splitext(video)[0] + ".csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CLIP_LIST)
        writer.writerows(clip_rows(clips))


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


def demoonly_plan(index, only="", player="", kill_before=None, kill_after=None, kill_filter=None):
    """What demoonly <only> [player] plays in a demo, as the engine does,
    from its index, with demofilter's kill_filter: (how many kills, [(start,
    end)] in msec), all of the demo without only. The player given, or
    whoever is shown for shownkills, is the killer instead of the filter's.
    Team kills aren't counted, and only play with a kill of their killer near
    them; the deaths of the player shown aren't kills."""
    kill_filter = kill_filter or KillFilter()
    duration = index.get("duration", 0)
    watched = index.get("watched", [])
    switches = [w["time"] for w in watched]
    if not only:
        return 0, [(0, duration)]
    if only == "watched":
        # the player given, else the filter's killers
        spans = [(w["time"], watched[i + 1]["time"] if i + 1 < len(watched) else duration)
                 for i, w in enumerate(watched)
                 if (same_player(w["name"], player) if player
                     else kill_filter.killers and kill_filter.has_killer(clean_name(w["name"])))]
        return 0, stretches(spans, switches)
    frags, kills, cut = planned_kills(index, only, player, kill_filter)
    kb = KILL_BEFORE if kill_before is None else kill_before
    ka = KILL_AFTER if kill_after is None else kill_after
    spans = [kill_span(index, k, start, end, kb, ka) for k, start, end in kills]
    return len(frags), stretches([span for span in spans if span[1] > span[0]], switches if cut else [])


def planned_kills(index, only, player="", kill_filter=None):
    """The kills demoonly <only> [player] plays, as demoonly_plan tells:
    (frags, kills, cut), [(kill, start, end)] with when each can play: the
    frags it counts, those and the team kills that play with them, and
    whether the player shown changing cuts joined clips (when they're
    someone's)."""
    kill_filter = kill_filter or KillFilter()
    kill_streaks(kill_levels(index))
    whoever = only.startswith("shown")
    if whoever or player or kill_filter.by_shown():
        kills = [(k, start, end) for k, start, end in shown_kills(index)
                 if (same_player(player, k["killerName"]) if player
                     else whoever or kill_filter.has_killer(clean_name(k["killerName"])))]
        cut = True
    else:
        # anyone's, whoever is shown
        kills, cut = [(k, 0, index.get("duration", 0)) for k in index.get("kills", [])], False
    deaths = deaths_shown(index)
    frags = [x for x in kills if not x[0].get("teamKill") and not is_grenade(x[0]) and id(x[0]) not in deaths
             and kill_filter.keeps(x[0], killer=False)]
    return frags, frags + team_kills_with(kills, [k for k, _, _ in frags]), cut


def clip_frags(index, spans, only="", player="", kill_filter=None):
    """The frags in each of spans, the clips demoonly <only> [player] plays:
    [[kill]], none for all of the demo or while a player is watched."""
    if only in ("", "watched"):
        return [[] for _ in spans]
    frags = [k for k, _, _ in planned_kills(index, only, player, kill_filter)[0]]
    return [[k for k in frags if start <= k["time"] < end] for start, end in spans]


def the(count, one, many):
    """"The 3 frags", "The frag" or "No frags"."""
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
                                       "Time recorded before each frag (cl_demoKillBefore)", "kill_before")
        self.kill_after = seconds_box(settings, "rec/kill_after", 2.0,
                                      "Time recorded after each frag (cl_demoKillAfter)", "kill_after")
        form.addRow("Each frag", lead_row(self.kill_before, self.kill_after, "it"))
        self.numbers = QCheckBox("Number them")
        self.numbers.setToolTip("A number on each clip of the video, and next to it the list of the clips with "
                                "their frags (a .csv of the same name)")
        self.numbers.setChecked(settings.value("rec/numbers", "false") == "true")
        form.addRow("Clips", self.numbers)
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
                           ("kill_after", str(self.kill_after.value())),
                           ("numbers", "true" if self.numbers.isChecked() else "false")):
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
        return {"label": label, "clips": numbered(clips, self.numbers.isChecked()),
                "numbers": self.numbers.isChecked(), **video_settings(self.settings),
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
                 "+set", "com_maxfps", "0", "+set", "r_swapInterval", "0",
                 *(BLUR_CVARS if job.get("blur") else [])]
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

        self.setWindowTitle("MoH Demo Replay")
        self.resize(1200, 720)
        style = self.style()

        # demos
        self.filter = QLineEdit(placeholderText="Filter demos by name, map or player")
        self.filter.setClearButtonEnabled(True)
        self.filter.setToolTip("Demos with all of these words in their name, a level or a player's name; commas "
                               "separate searches, so \"pstn, tommeh\" finds the demos of either")
        self.filter.textChanged.connect(self.filter_demos)
        self.rules = QComboBox()
        self.rules.addItem("All demos", "")
        self.rules.addItem("Default", "default")
        self.rules.addItem("Realism", "realism")
        self.rules.setToolTip("Default or realism servers, told by the speeds of the recorder's weapons")
        self.rules.setCurrentIndex(max(0, self.rules.findData(settings.value("rules", ""))))
        self.rules.currentIndexChanged.connect(self.rules_changed)
        self.recorded_by = NamesField("Demos recorded by players whose names have one of these, separated by commas",
                                   menu_tip="The recorders of the demos, those of the most first",
                                   empty="No demos indexed yet")
        self.recorded_by.edited.connect(self.filter_demos)
        self.demos = self.make_list(DEMO_COLUMNS, fit=False)
        self.demos.setSortingEnabled(True)
        self.demos.sortByColumn(COL_DATE, Qt.DescendingOrder)
        self.demos.itemActivated.connect(lambda item: item.data(COL_DEMO, Qt.UserRole)
                                         and self.play_demo(item.data(COL_DEMO, Qt.UserRole)))
        self.add_menu(self.demos, "Record the demos selected…", self.record_demos)
        # the kills of the demos listed and of the demo playing
        self.killer = NamesField("Frags by players whose names have one of these, separated by commas, while "
                                 "they're shown, or by whoever is shown", offer_shown=True)
        self.victim = NamesField("Frags of players whose names have one of these, separated by commas")
        self.weapon = QToolButton()
        self.weapon.setPopupMode(QToolButton.InstantPopup)
        self.weapon.setToolTip("Frags made with these weapons, as the death messages tell")
        menu = StayOpenMenu(self.weapon)
        menu.setToolTipsVisible(True)
        self.weapon_actions = {}
        for key, label in WEAPONS.items():
            action = self.weapon_actions[key] = menu.addAction(label)
            action.setCheckable(True)
            action.setToolTip(WEAPON_TIPS[key])
            action.toggled.connect(self.weapons_changed)
        menu.addSeparator()
        menu.addAction("Any weapon", lambda: [a.setChecked(False) for a in self.weapon_actions.values()])
        self.weapon.setMenu(menu)
        # applied once typing stops
        self.kill_filter_timer = QTimer(self, singleShot=True, interval=250, timeout=self.apply_kill_filter)
        self.killer.edited.connect(self.kill_filter_timer.start)
        self.victim.edited.connect(self.kill_filter_timer.start)
        self.weapons_changed()
        self.obj = QCheckBox("Obj maps only")
        self.obj.setToolTip("Only the frags made on the objective maps (obj/, as obj_team4 or harbor), "
                            "not on the deathmatch ones (dm/)")
        self.obj.toggled.connect(self.kill_filter_timer.start)
        self.great = QCheckBox("Great frags")
        self.great.setToolTip("Leaves out the sniper frags made with the scope on for over a second while standing "
                              "still, as the fragger's view shows them: no-scopes, quick scopes, shots on the move and "
                              "streaks (frags at most 5 s apart) stay, and so do the frags of other weapons and those "
                              "the demo doesn't show from the fragger's view. Leaves out the frags of AFK players too, "
                              "who had given no input for over 5 s")
        self.great.toggled.connect(self.kill_filter_timer.start)
        left = QWidget()
        box = QVBoxLayout(left)
        box.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.filter, 2)
        row.addWidget(QLabel("Recorder"))
        row.addWidget(self.recorded_by, 1)
        row.addWidget(self.rules)
        box.addLayout(row)
        row = QHBoxLayout()
        for label, widget, stretch in (("Fragger", self.killer, 3), ("Victim", self.victim, 2), ("Weapon", self.weapon, 0)):
            row.addWidget(QLabel(label))
            row.addWidget(widget, stretch)
        row.addWidget(self.obj)
        row.addWidget(self.great)
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
        # what plays: one of three, as the game tells
        self.play_modes = QButtonGroup(self)
        self.everything_button = QPushButton("Everything")
        self.only_kills_button = QPushButton("These frags")
        self.watched_button = QPushButton("While watched")
        modes = QHBoxLayout()
        modes.setSpacing(0)
        for button, text, action in (
                (self.everything_button, "Play all of the demo", lambda: self.game.send("demoonly") or True),
                (self.only_kills_button, "Play only the frags listed, from the one selected or the first",
                 self.only_kills),
                (self.watched_button, "Play only while one of the fraggers is shown, from the first time",
                 self.only_watched)):
            button.setCheckable(True)
            button.setToolTip(text)
            button.clicked.connect(lambda _=False, a=action: self.play_mode_clicked(a))
            self.play_modes.addButton(button)
            modes.addWidget(button)
        self.everything_button.setChecked(True)
        players = QHBoxLayout()
        players.addWidget(QLabel("Play"))
        players.addLayout(modes)
        players.addStretch(1)

        self.kills = self.make_list(["Time", "Fragger", "Victim", "Weapon"])
        self.rounds = self.make_list(["Time", "Round", "Result"])
        # a click jumps, so does Enter
        for tree in (self.kills, self.rounds):
            tree.itemClicked.connect(self.jump_to_item)
            tree.itemActivated.connect(self.jump_to_item)
        self.add_menu(self.kills, "Record the ones selected…", lambda: self.record_selected(self.kills))
        self.tabs = tabs = QTabWidget()
        tabs.addTab(self.kills, "Frags")
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
                                   (QStyle.SP_MediaSeekBackward, "Previous frag listed (Down)",
                                    lambda: self.kill_commands("demoprevkill")),
                                   (None, "Back 10 seconds (Left: 5)", "demoskip -10")):
            controls.addWidget(self.make_button(icon, tip, lambda _=False, c=command: self.game.send(
                *(c() if callable(c) else [c])), "-10 s"))
        controls.addWidget(self.play)
        for icon, tip, command in ((None, "Forward 10 seconds (Right: 5)", "demoskip 10"),
                                   (QStyle.SP_MediaSeekForward, "Next frag listed (Up)",
                                    lambda: self.kill_commands("demonextkill")),
                                   (QStyle.SP_MediaSkipForward, "Next round (PgUp)", "demonextround")):
            controls.addWidget(self.make_button(icon, tip, lambda _=False, c=command: self.game.send(
                *(c() if callable(c) else [c])), "+10 s"))
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

    def weapons_changed(self, _=None):
        chosen = [label for key, label in WEAPONS.items() if self.weapon_actions[key].isChecked()]
        self.weapon.setText(", ".join(chosen) if chosen else "Any weapon")
        self.kill_filter_timer.start()

    def kill_filter(self):
        """The kills chosen with Killer, Victim, Weapon, Obj maps only and
        Great frags."""
        return KillFilter(self.killer.names(), self.victim.names(),
                          [key for key, action in self.weapon_actions.items() if action.isChecked()], self.killer.shown(),
                          self.obj.isChecked(), self.great.isChecked())

    def apply_kill_filter(self):
        """The demos with kills the filter keeps, and those of the demo
        playing."""
        self.kill_filter_timer.stop()
        self.filter_demos()
        self.fill_kills()
        self.show_state()

    def filter_demos(self):
        """Shows the demos with each word of one of the searches (separated
        by commas) in the name, a map or a player's name, and the players
        found; with the kill filter, only those with kills it keeps, and
        their killers; only those recorded by the recorders named, and of the
        rules chosen, if any, and not those shorter than MIN_LENGTH."""
        searches = search_parts(self.filter.text())
        rules = self.rules.currentData()
        recorders = [clean_name(name) for name in self.recorded_by.names()]
        kill_filter = self.kill_filter()
        found_any = False
        for name, item in self.demo_items.items():
            summary = self.library.demos.get(name) or {}
            found, shown = {}, not rules or summary.get("rules") in (rules, "both")
            shown = shown and summary.get("duration", MIN_LENGTH) >= MIN_LENGTH
            if shown and recorders:
                recorder = clean_name(summary.get("recorder", ""))
                shown = bool(recorder) and any(part in recorder for part in recorders)
            if shown and searches:
                hits = search_demo(name, summary, searches)
                shown, found = hits is not None, hits or {}
            if shown and kill_filter:
                count, found = filter_kills(summary, kill_filter)
                shown = count > 0
            item.setHidden(not shown)
            best = sorted(found.values(), key=lambda p: (-p[1], p[0].lower()))
            item.setText(COL_PLAYER, ", ".join(player_kills(p) for p in best))
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
            if (searches or recorders or kill_filter) and count:
                item.setExpanded(True)
        self.demos.setColumnHidden(COL_PLAYER, not found_any)
        self.fit_columns(self.demos)

    def library_changed(self):
        # those who recorded the most first
        recorders = {}
        for summary in self.library.demos.values():
            if summary["recorder"]:
                recorders.setdefault(clean_name(summary["recorder"]), [summary["recorder"], 0])[1] += 1
        ranked = sorted(recorders.values(), key=lambda r: (-r[1], r[0].lower()))
        self.recorded_by.players = [name for name, _ in ranked]
        self.recorded_by.labels = {name: f"{name} ({count})" for name, count in ranked}
        self.fill_demos()
        progress = self.library.progress()
        self.indexing.setText(f"Indexing demos: {progress[0]} of {progress[1]}" if progress else "")

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
        self.index = kill_streaks(kill_levels(index)) if index and index.get("demo") == demo else None
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
        player = self.state.get("player", "")
        kill_filter = self.kill_filter()
        # of the kill filter, without a player
        others = kill_filter.words(killers=False)
        if only in ("kills", "multikills"):
            mode = ("only the frags" if only == "kills" else "only the multi-kills") + (
                " by " + player + others if player else " of whoever is shown" + others if kill_filter.shown
                else kill_filter.words())
        elif only in ("shownkills", "shownmultikills"):
            mode = ("only the frags" if only == "shownkills" else "only the multi-kills") + " of whoever is shown" + others
        elif only == "watched":
            mode = "only while " + (player or or_list(kill_filter.names)) + " is watched"
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
        names = {}
        for kill in index.get("kills", []):
            names.update((clean_name(n), n) for n in (kill["killerName"], kill["victimName"]) if n)
        # for the menus of Killer and Victim
        self.killer.players = self.victim.players = sorted(names.values(), key=str.lower)

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

    def listed_kills(self, kill_filter):
        """The kills to list, as {id(kill): (kill, from, to)} in order, with when
        they can be played, as demoonly kills plays them: those of the
        filter's killers, or whoever's is shown, while they're shown, else
        anyone's. Team kills too, for the clips of the kills near them; the
        filter's victims and weapons aren't looked at."""
        index = self.index or {}
        if not kill_filter.by_shown():
            return {id(k): (k, 0, index.get("duration", 0) + KILL_AFTER) for k in index.get("kills", [])}
        return {id(k): (k, start, end) for k, start, end in shown_kills(index)
                if kill_filter.has_killer(clean_name(k["killerName"]))}

    def kill_commands(self, command):
        """To the next or previous kill listed (demonextkill or demoprevkill),
        telling the game the kill filter first, which it may not have yet."""
        return [*self.kill_filter().commands(), command]

    def switches(self):
        """When the player shown changes: the kills of a player or of whoever
        is shown aren't joined across it, all the kills are."""
        return [w["time"] for w in (self.index or {}).get("watched", [])] if self.kill_filter().by_shown() else []

    def fill_kills(self):
        """The kills the filter keeps, listed, each item with when it starts
        and its clips: the stretches demoonly plays. Team kills aren't kills:
        they're only in the clips of a kill of their killer near them, as
        demoonly plays. Nor are the deaths of the player shown. The game is
        told the filter, for the kill keys and demoonly."""
        kill_filter = self.kill_filter()
        kills = list(self.listed_kills(kill_filter).values())
        kb, ka = self.get_kill_times()
        spans = {id(kill): kill_span(self.index or {}, kill, start, end, kb, ka) for kill, start, end in kills}
        switches = self.switches()
        # items keep their kills by place in the index: Qt gives copies back
        place = {id(kill): i for i, kill in enumerate((self.index or {}).get("kills", []))}
        deaths = deaths_shown(self.index or {})
        self.kills.clear()
        self.kill_marks = []
        for kill, _, _ in kills:
            if kill.get("teamKill") or is_grenade(kill) or id(kill) in deaths or not kill_filter.keeps(kill, killer=False):
                continue
            killer, victim, how = kill["killerName"], kill["victimName"], kill["text"]
            span = spans[id(kill)]
            clips = stretches([span] + [spans[id(k)] for k, _, _ in team_kills_with(kills, [kill])], switches)
            item = QTreeWidgetItem([clock(kill["time"]), killer, victim, WEAPONS[weapon_of(kill)]])
            for column in range(4):
                item.setToolTip(column, plain_tip(how))
            item.setData(0, Qt.UserRole, span[0])
            item.setData(1, Qt.UserRole, clips)
            item.setData(2, Qt.UserRole, [place[id(kill)]])
            self.kills.addTopLevelItem(item)
            self.kill_marks.append((kill["time"], how))
        # of all those of the demo playing, when some are left out
        listed = self.kills.topLevelItemCount()
        if kill_filter and self.index:
            every = sum(1 for kill in (self.index or {}).get("kills", [])
                        if not kill.get("teamKill") and not is_grenade(kill) and id(kill) not in deaths)
            self.tabs.setTabText(0, f"Frags ({listed} of {every})")
        else:
            self.tabs.setTabText(0, f"Frags ({listed})")
        self.tabs.setTabText(1, f"Rounds ({self.rounds.topLevelItemCount()})")
        # for Up and Down too
        self.game.send(*kill_filter.commands())
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
            QMessageBox.information(self, "Only these frags", "There are no frags to play.")
            return False
        self.game.send(*self.kill_filter().commands(), "demoseek " + seconds(item.data(0, Qt.UserRole)),
                       "demoonly kills")
        return True

    def only_watched(self):
        """Plays while one of the filter's killers is watched, from the first
        time; False if none is."""
        kill_filter = self.kill_filter()
        if not kill_filter.killers:
            QMessageBox.information(self, "Only while watched", "Type a fragger first.")
            return False
        watched = (self.index or {}).get("watched", [])
        start = next((w["time"] for w in watched if kill_filter.has_killer(clean_name(w["name"]))), None)
        if start is None:
            QMessageBox.information(self, "Only while watched", or_list(kill_filter.names) + " isn't watched in this demo.")
            return False
        self.game.send(*kill_filter.commands(), "demoseek " + seconds(start), "demoonly watched")
        return True

    # recording

    def kill_texts(self, kill_filter):
        """What Record… calls the filter's kills, the recorder's and those of
        whoever is shown: {key: (one, several, after the count)}."""
        others = kill_filter.words(killers=False)
        shown = (SHOWN_KILLS[0] + others, SHOWN_KILLS[1] + others, SHOWN_WHO)
        return {"kills": shown if kill_filter.shown else ("frag" + kill_filter.words(), "frags" + kill_filter.words(), ""),
                "recorderkills": (RECORDER_KILLS[0] + others, RECORDER_KILLS[1] + others, ""), "shownkills": shown}

    def record(self):
        """Record…: a stretch of the demo playing, the kills of the filter,
        those of the recorder or of whoever is shown with its victims and
        weapons, or while one of its killers is watched."""
        demo = self.state.get("demo")
        if not demo:
            QMessageBox.information(self, "Record a video", "Play a demo first.")
            return
        kill_filter = self.kill_filter()
        index = self.index or {}
        recorder = (index.get("recorder") or {}).get("name", "")
        keys = ["kills", "recorderkills", "shownkills", "watched"]
        # not twice the same kills
        if kill_filter.shown:
            keys.remove("shownkills")
        if not recorder or kill_filter.killers and all(part in clean_name(recorder) for part in kill_filter.killers):
            keys.remove("recorderkills")
        texts = self.kill_texts(kill_filter)
        # whose kills each choice plays instead of the filter's killers
        whose = {"recorderkills": recorder}

        def plans_for(before, after):
            """{key: (count, spans)} with that lead-in and out."""
            return {key: (0, []) if key == "watched" and not kill_filter.killers
                    else demoonly_plan(index, ONLY[key], whose.get(key, ""), before, after, kill_filter) for key in keys}

        def choices(plans):
            listed = []
            for key in keys:
                enabled = bool(plans[key][1]) if key == "watched" else plans[key][0] > 0
                listed.append((key, labels[key] + (", " + about(length(plans[key][1])) if enabled else ""), enabled))
            return listed

        plans = plans_for(*self.get_rec_kill_times())
        labels = {key: the(plans[key][0], *texts[key][:2]) + texts[key][2] for key in keys if key != "watched"}
        labels["watched"] = (f"While {or_list(kill_filter.names)} is watched" if kill_filter.killers
                             else "While a player is watched")
        dialog = RecordDialog(self.settings, choices(plans), self.state.get("time", 0), parent=self,
                              relabel=lambda before, after: choices(plans_for(before, after)))
        if not dialog.exec():
            return
        what = dialog.what()
        # who's in the video's name
        player = ", ".join(kill_filter.names)
        if what == "range":
            start, end = dialog.times()
            label = f"{demo}, {clock(start)} to {clock(end)}"
            clip = range_clip(demo, start, end)
        else:
            label = demo + ", " + labels[what][0].lower() + labels[what][1:]
            player = whose.get(what, "" if what == "shownkills" else player)
            clip = only_clip(demo, ONLY[what], whose.get(what, ""), plans_for(*dialog.kill_times())[what][1],
                             kill_filter)
            clip["frags"] = clip_frags(index, clip["spans"], ONLY[what], whose.get(what, ""), kill_filter)
        self.add_job(dialog.job(label, [clip], demo, player))

    def record_selected(self, tree):
        """The kills selected, in one video or one each."""
        demo = self.state.get("demo")
        items = tree.selectedItems()
        if not demo or not items:
            QMessageBox.information(self, "Record a video", "Select frags of the demo playing first.")
            return
        one = "frag"
        kind = f"{len(items)} {one}s" if len(items) > 1 else one
        kill_filter = self.kill_filter()
        # those of a killer, or of whoever is shown, only while they're shown
        kills = self.listed_kills(kill_filter)
        player = ", ".join(kill_filter.names)
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
        for clip in clips:
            clip["frags"] = [[k for chain in chains for k in chain if clip["start"] <= k["time"] < clip["end"]]]
        if dialog.join.isChecked() or len(clips) == 1:
            self.add_job(dialog.job(f"{demo}, {kind}", clips, demo, player))
        else:
            for clip in clips:
                self.add_job(dialog.job(f"{demo}, {clock(clip['start'])} to {clock(clip['end'])}", [clip], demo, player))

    def record_demos(self):
        """The demos selected, all of them, or the kills of the filter, of
        their recorder or of whoever is shown, with its victims and weapons,
        in one video or one each."""
        names = self.demos_of(self.demos.selectedItems())
        if not names:
            QMessageBox.information(self, "Record a video", "Select demos first.")
            return
        kill_filter = self.kill_filter()
        indexes = {name: self.library.index(name) for name in names}
        # whose kills each choice plays in each demo instead of the filter's
        recorders = {name: (index.get("recorder") or {}).get("name", "") for name, index in indexes.items() if index}
        # not twice the same kills
        keys = [key for key in ("kills", "recorderkills", "shownkills", "everything")
                if key != "shownkills" or not kill_filter.shown]

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
                    if key == "recorderkills":
                        plan = (demoonly_plan(index, "kills", recorders[name], before, after, kill_filter)
                                if recorders[name] else (0, []))
                    else:
                        plan = demoonly_plan(index, "" if key == "everything" else key, "", before, after, kill_filter)
                    if plan[0] or key == "everything":
                        plans[key][name] = plan
            return plans

        texts = self.kill_texts(kill_filter)

        def choices(plans):
            def text(key, label):
                """label, in how many demos, how long."""
                spans = [plan[1] for plan in plans[key].values()]
                if len(spans) < len(names) and len(names) > 1 and key != "everything":
                    label += f", in {len(spans)} of the {len(names)} demos"
                if spans and None not in spans:
                    label += ", " + about(sum(length(s) for s in spans))
                return key, label, bool(spans)
            count = {key: sum(plan[0] for plan in plans[key].values()) for key in plans}
            return [text(key, the(count[key], *texts[key][:2]) + texts[key][2]) if key != "everything" else
                    text(key, "All of the demo" if len(names) == 1 else f"All of the {len(names)} demos")
                    for key in keys]

        dialog = RecordDialog(self.settings, choices(plans_for(*self.get_rec_kill_times())), join=len(names) > 1,
                              parent=self, relabel=lambda before, after: choices(plans_for(before, after)))
        if not dialog.exec():
            return
        what = dialog.what()
        plans = plans_for(*dialog.kill_times())
        chosen = list(plans[what])
        kind = {"kills": SHOWN_KILLS[1] if kill_filter.shown else "frags" + kill_filter.words(),
                "recorderkills": RECORDER_KILLS[1], "shownkills": SHOWN_KILLS[1], "everything": ""}[what]
        # who's in the videos' names, and whose kills play in each demo
        player = "" if what in ("shownkills", "everything") or kill_filter.shown else ", ".join(kill_filter.names)
        whose = recorders if what == "recorderkills" else {}
        if what == "recorderkills":
            player = whose.get(chosen[0], "") if len(chosen) == 1 else ""
        only = "" if what == "everything" else ONLY[what]
        clips = [only_clip(name, only, whose.get(name) or "", plans[what][name][1], kill_filter) for name in chosen]
        for clip in clips:
            if clip["spans"]:
                clip["frags"] = clip_frags(indexes[clip["demo"]], clip["spans"], only, whose.get(clip["demo"]) or "",
                                           kill_filter)
        if dialog.join.isChecked() or len(clips) == 1:
            demo = chosen[0] if len(chosen) == 1 else f"{len(chosen)} demos"
            self.add_job(dialog.job(demo + (f", {kind}" if kind else ""), clips, demo, player))
        else:
            for name, clip in zip(chosen, clips):
                label = name + (f", {kind}" if kind else "")
                self.add_job(dialog.job(label, [clip], name, whose.get(name) or player))

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
            if job.get("numbers"):
                try:
                    write_clip_list(job["clips"], video)
                except OSError as e:
                    self.statusBar().showMessage(f"Recorded {video}, but couldn't write its list of clips: {e}")
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
