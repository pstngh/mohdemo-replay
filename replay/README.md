# MoH Demo Replay

Rewatch Medal of Honor: Allied Assault demos (`.dm3`) with buttons: the list
of demos, their frags and rounds, play/pause, speed and seeking. It needs
OpenMoHAA built from this repository, which adds the demo commands, Python 3
and PySide6.

    python3 replay/mohreplay.py [--exe PATH] [--game FOLDER] [--demos FOLDER] [DEMO]

On the first run, Settings asks for the game program (found by itself when
built in `.cmake/RelWithDebInfo/`), the folder with the game files (the one
with `main/Pak0.pk3`) and the folder with the demos, whose folders are
looked in too: the list shows them in their folders, which tell how many of
their demos are shown, and a demo there is named by its path in the folder
of demos (the filter finds a folder's name too). The list shows each
demo's length (demos under 5 minutes aren't listed), recorder, levels and rules (default or realism servers, told by the
recorder's speeds with each weapon; blank when they spectated or played
too little), the menu next to the filter shows only default or realism
demos, and the filter finds demos by name, level or player: for a player, it
shows their frags in each demo. For that, `mohdemoindex`, built next to the
game, indexes the demos in the background the first time (about two minutes
for 700, and again once after it changes), and the indexes are kept in
`~/.cache/mohdemo-replay`.

Fragger, Victim and Weapon, under the filter, choose the frags, those of
the demos listed and of the demo playing, each on its own or together:
Fragger and Victim take parts of names, separated by commas, as the filter does
(`pstn, tommeh` finds pstN's frags under any clan tag, and tommeh's), and
their menus add the players of the demo playing; Weapon ticks sniper, rifle,
SMG, StG / BAR (the StG 44 and the BAR, which the game calls machine guns),
shotgun, pistol, bazooka, melee (a hit with a gun's butt) or other, as the
death messages tell (also the French, German, Spanish and Portuguese ones
seen; the French write a rifle frag as a sniper frag). The list then shows
only the demos with such frags, with how many of each fragger, and the
demo playing lists only those. A player's frags are only those made while the
demo shows them (the recorder playing, or the player the recorder follows),
and only while they're shown, so a video of them never shows someone else.
"Whoever is shown", in Fragger's menu, keeps the frags shown: the recorder's
while they play and those of the players they follow, while they follow
them.

Double-click a demo to play it (the game starts then, or with Restart game
in the toolbar, where the folder's button chooses another), click a frag or
a round to jump to it; hovering a frag tells how it was made. Play chooses
what plays: Everything, These frags, the frags listed, from the one
selected or the first, or While watched, while one of the fraggers is shown.
Settings also sets how long
each frag plays before and after it (4 and 2 seconds), and Viewmodel and
Force models, in the toolbar, show the gun in the player's hands and show
everyone as an airborne soldier or a winter German, whatever they wore.
The time slider marks the frags listed under it and the rounds above it:
hovering one tells what it is, elsewhere the time there, and a click jumps
there.

Record… makes an MP4 of the demo playing: a stretch of time, the frags
listed, the recorder's own or the frags shown (both of the victims and with
the weapons chosen), one after the other, or only while a fragger is
watched. A right-click on frags selected records those, and on demos or
folders selected, all of their demos, the frags chosen, the recorder's, or
the frags shown: in one video, or one each. Only the choices with something to record are offered; each tells how many
frags it holds and how long the video will be, and the last one is chosen
again next time. Videos are recorded one after the
other by a second copy of the game, in the background, without a window on
Linux and faster than real time, while you keep watching; FFmpeg joins the
clips of a video without encoding them again. The Videos panel lists them,
with how far the one recording is and how long it has left
(Cancel, Open folder, double-click to watch one), and each lands in the
folder chosen, named from a pattern (`{demo}`, the demo's file name,
`{start}`, `{player}`, `{date}`), with each frag played from how long before
it to how long after. The size, frames per second, quality, codec (H.264,
H.265 or the graphics card's H.264 encoder, with VAAPI), sound and its bitrate, the viewmodel and the models forced are in
the Videos tab of Settings, which the Record dialog sums up in a line, with
Change… to set them there, and the Advanced box takes FFmpeg output options
instead. Motion blur is the renderer's: what moves blurs
over all of a frame's time, as with a camera, along each pixel's motion.
Motion looks smoother, a cut between two frags stays clean, and recording
takes about 1.7 times as long with the graphics card's encoder, 1.1 times
with H.264 (at 1920x1080, 60 frames a second). The clips are written in a hidden
`.mohreplay-parts` folder next to the video, not in memory. It needs FFmpeg.

The game built here, on MoHAA Reloaded's Vulkan port, runs on Linux only:
that port has no macOS build.

The app starts the game with a throwaway home folder, sends it console
commands through its pipe (`com_pipefile`) and shows what the game reports
in `demoindex.json` and `demostate.json` (`cl_demoFiles 1`). On Linux it
asks for Wayland first, then XWayland. The settings changed in the game (its
options, the console, key binds) are kept for the next games in the game
folder, as `main/configs/omconfig.cfg` and `main/omconfig.cfg`, which are
read first, and in `~/.config/mohdemo-replay/omconfig.cfg`, and videos are recorded with them. The
app's keys and its pipe aren't kept, so a game run from that folder without
the app doesn't get them. Some
are always the app's: the window size, the app's keys, a field of view of
80, the most detailed models (`r_uselod 0`), the viewmodel and the models
forced, and frames drawn at the screen's refresh rate (`r_swapInterval 1`,
`com_maxfps 0`).

## Keys in the game window

During a demo, only Esc brings up the menu and Tab shows the scores; the
other letters, numbers, Space and Enter do nothing. The app binds these keys:

| Key | Does |
| --- | --- |
| Pause | pause or play |
| Left / Right | back or forward 5 seconds |
| Down / Up | previous or next frag listed: the frags chosen in the app |
| PgDn / PgUp | previous or next round |

## Console commands

The game's commands call frags kills, and fraggers killers.

The buttons only send these, so they also work in the console or with key
binds, without the app:

| Command | Does |
| --- | --- |
| `demoseek <time>` | jump to a time (seconds or minutes:seconds), alone prints the time |
| `demoskip <time>` | move forward, or back with a negative time |
| `demopause [0\|1]` | pause or play, toggles without an argument |
| `demonextkill [player]`, `demoprevkill [player]` | jump to before the next or previous kill of `demofilter`, by that player while shown if given |
| `demonextshownkill`, `demoprevshownkill` | jump to before the next or previous kill of whoever is shown |
| `demonextround`, `demoprevround` | jump to the next or previous round |
| `demofilter [clear \| shown \| killer <name> \| victim <name> \| weapon <weapon>...]` | the kills the kill commands go to: by players whose names have one of the killers in them, while shown, or by whoever is shown, of players whose names have one of the victims, made with one of the weapons (`pistol`, `rifle`, `sniper`, `smg`, `mg`, `shotgun`, `rocket`, `melee`, `other`); each adds, but `clear`; alone, it prints them. A player given to a command is the killer instead, the victims and weapons still apply |
| `demoonly kills [player]` | play only the kills of `demofilter`, by that player while shown if given |
| `demoonly multikills [player]` | play only the multi-kills, by that player while shown if given |
| `demoonly shownkills` | play only the kills of whoever is shown (the recorder playing, or the player they follow), while they're shown |
| `demoonly shownmultikills` | the same, of the multi-kills |
| `demoonly watched [player]` | play only while that player is shown, or one of `demofilter`'s killers |
| `demoonly` | play everything again |
| `timescale <speed>` | playback speed, 1 is normal |
| `demovideo <name> [end time]` | record into an MP4, see below |

`cl_demoKillBefore` and `cl_demoKillAfter` set how many seconds of each kill
are shown (4 and 2). Kills less than a second apart play as one, without a
jump, except for a player's kills or whoever's is shown when the player
shown changes in between: that stays a cut. A multi-kill is two kills or
more by a player, each at most `cl_demoMultiKill` seconds (3) after the one
before.

Team kills (the killer and the victim last seen on the same team, also when
everyone plays on one team) aren't kills: they aren't listed, counted, part
of multi-kills or gone to by the next and previous kill, and they only play,
and are only recorded, with a kill by the same player at most 2 seconds
before or after them: a teammate hit while shooting at an enemy. Grenade
kills (a grenade or its shrapnel, as the death message tells) aren't kills either, nor
is the death of the player shown at the time: the recorder killed while
playing, or the player they follow. A kill plays only inside its level: its
clip starts no earlier than the level and ends with it.

While the recorder follows a player, most servers with the 1.12 Reborn patch
put the camera in that player's head, where their own arms and gun are in
the way. `cg_followcamera 1` (the default) puts it behind them, where stock
servers do; `cg_followcamera 0` shows it as recorded.

## Recording

`demovideo <name> [end time]` records the demo from now on into
`videos/<name>.mp4` in the game's home folder, through FFmpeg, until the end
time, `stopvideo`, the end of the demo or the end of what `demoonly` plays.
Seeks and pauses aren't recorded, so `demoseek 12:30; demovideo clip 12:45`
records exactly 15 seconds, and `demoonly kills t-; demovideo frags` all of
a player's kills one after the other.

The game steps exactly 1/`cl_aviFrameRate` of a second per frame, so videos
are smooth whatever the screen does; it records faster than real time when
it can. The sound is recorded when the game is started with
`+set s_loopback 1`: it is then rendered for the video and not played.
`cl_aviPipeFormat` holds FFmpeg's output options (H.264, CRF 20, AAC 192k by
default) and `cl_aviFFmpeg` the FFmpeg program; FFmpeg's errors go to
`<name>.mp4.log`. The fps counter (`fps 1`) isn't drawn in videos.

## Tests

    python3 replay/tests/test_mohreplay.py [-v]

They run the app without a window against a fake game
(`replay/tests/fakegame.py`), so they need neither game files nor a screen,
and run with the engine's tests (`ctest`). With `MOHREPLAY_TEST_GAME` (the
game files folder) and `MOHREPLAY_TEST_DEMOS` (a folder of real demos) set,
they also play and record a real demo with the game built in
`.cmake/RelWithDebInfo`, without a window.
