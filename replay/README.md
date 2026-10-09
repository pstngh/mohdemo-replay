# MoH Demo Replay

Rewatch Medal of Honor: Allied Assault demos (`.dm3`) with buttons: the list
of demos, their kills and rounds, play/pause, speed and seeking. It needs
OpenMoHAA built from this repository, which adds the demo commands, Python 3
and PySide6.

    python3 replay/mohreplay.py [--exe PATH] [--game FOLDER] [--demos FOLDER] [DEMO]

On the first run, Settings asks for the game program (found by itself when
built in `.cmake/RelWithDebInfo/`), the folder with the game files (the one
with `main/Pak0.pk3`) and the folder with the demos. The list shows each
demo's length (demos under 5 minutes aren't listed), recorder, levels and rules (default or realism servers, told by the
recorder's speeds with each weapon; blank when they spectated or played
too little), the menu next to the filter shows only default or realism
demos, and the filter finds demos by name, level or player: for a player, it shows their kills in each demo, and playing one
chooses them. For that, `mohdemoindex`, built next to the game, indexes the
demos in the background the first time (about two minutes for 700), and the
indexes are kept in `~/.cache/mohdemo-replay` (`~/Library/Caches` on macOS). Double-click a demo to
play it, click a kill, a multi-kill or a round to jump to it. "Only these
kills" plays the kills listed (by the player chosen), from the one selected
or the first, and on the Multi-kills tab, the multi-kills: two kills or
more by a player, each at most 3 seconds after the one before. A player's
kills are only those made while the demo shows them (the recorder playing,
or the player the recorder follows), and only while they're shown, so a
video of them never shows someone else.
The time slider marks the kills listed under it and the rounds above it:
hovering one tells what it is, elsewhere the time there, and a click jumps
there.

Record… makes an MP4 of the demo playing: a stretch of time, the kills or
the multi-kills (of the player chosen above) one after the other, or only
while a player is watched. A right-click on kills or multi-kills selected
records those, and on demos selected, all of them or the kills or
multi-kills of the player the filter found, each demo with its own spelling
of the name: in one video, or one each. Videos are recorded one after the
other by a second copy of the game, in the background, without a window on
Linux and faster than real time, while you keep watching; FFmpeg joins the
clips of a video without encoding them again. The Videos panel lists them
(Cancel, Open folder, double-click to watch one), and each lands in the
folder chosen, named from a pattern (`{demo}`, `{start}`, `{player}`,
`{date}`). Size, frames per second, quality, codec (H.264,
H.265 or the graphics card's H.264 encoder: VAAPI on Linux, VideoToolbox on
macOS), sound and its bitrate are settings, and the Advanced box takes
FFmpeg output options instead. It needs FFmpeg.

On macOS, the game program can be the `openmohaa.app` bundle, and
`mohdemoindex` is looked for next to it as well. The recorder shows a window
there, as SDL can't draw without one, and the sound of videos needs the
game built with OpenAL Soft's headers, as the release builds are. The app
hasn't been tried on a Mac yet.

The app starts the game with a throwaway home folder, sends it console
commands through its pipe (`com_pipefile`) and shows what the game reports
in `demoindex.json` and `demostate.json` (`cl_demoFiles 1`). On Linux it
asks for Wayland first, then XWayland. The settings changed in the game (its
options, the console, key binds) are kept for the next games in
`~/.config/mohdemo-replay/omconfig.cfg` (`~/Library/Preferences` on macOS),
and videos are recorded with them; the window size and the app's keys are
always the app's.

## Keys in the game window

During a demo, only Esc brings up the menu and Tab shows the scores; the
other letters, numbers, Space and Enter do nothing. The app binds these keys:

| Key | Does |
| --- | --- |
| Pause | pause or play |
| Left / Right | back or forward 5 seconds |
| Down / Up | previous or next kill |
| PgDn / PgUp | previous or next round |

## Console commands

The buttons only send these, so they also work in the console or with key
binds, without the app:

| Command | Does |
| --- | --- |
| `demoseek <time>` | jump to a time (seconds or minutes:seconds), alone prints the time |
| `demoskip <time>` | move forward, or back with a negative time |
| `demopause [0\|1]` | pause or play, toggles without an argument |
| `demonextkill [player]`, `demoprevkill [player]` | jump to before the next or previous kill, by that player while shown if given |
| `demonextround`, `demoprevround` | jump to the next or previous round |
| `demoonly kills [player]` | play only the kills, by that player while shown if given |
| `demoonly multikills [player]` | play only the multi-kills, by that player while shown if given |
| `demoonly watched <player>` | play only while that player is shown |
| `demoonly` | play everything again |
| `timescale <speed>` | playback speed, 1 is normal |
| `demovideo <name> [end time]` | record into an MP4, see below |

`cl_demoKillBefore` and `cl_demoKillAfter` set how many seconds of each kill
are shown (4 and 2). A multi-kill is two kills or more by a player, each at
most `cl_demoMultiKill` seconds (3) after the one before.

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
`<name>.mp4.log`.

## Tests

    python3 replay/tests/test_mohreplay.py [-v]

They run the app without a window against a fake game
(`replay/tests/fakegame.py`), so they need neither game files nor a screen,
and run with the engine's tests (`ctest`). With `MOHREPLAY_TEST_GAME` (the
game files folder) and `MOHREPLAY_TEST_DEMOS` (a folder of real demos) set,
they also play and record a real demo with the game built in
`.cmake/RelWithDebInfo`, without a window.
