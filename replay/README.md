# MoH Demo Replay

Rewatch Medal of Honor: Allied Assault demos (`.dm3`) with buttons: the list
of demos, their kills and rounds, play/pause, speed and seeking. It needs
OpenMoHAA built from this repository, which adds the demo commands, Python 3
and PySide6.

    python3 replay/mohreplay.py [--exe PATH] [--game FOLDER] [--demos FOLDER] [DEMO]

On the first run, Settings asks for the game program (found by itself when
built in `.cmake/RelWithDebInfo/`), the folder with the game files (the one
with `main/Pak0.pk3`) and the folder with the demos. The list shows each
demo's length and levels, and the filter finds demos by name, level or
player: for a player, it shows their kills in each demo, and playing one
chooses them. For that, `mohdemoindex`, built next to the game, indexes the
demos in the background the first time (about two minutes for 700), and the
indexes are kept in `~/.cache/mohdemo-replay`. Double-click a demo to
play it, click a kill or a round to jump to it. "Only these kills" plays the
kills listed (by the player chosen), from the one selected or the first.
The time slider marks the kills listed under it and the rounds above it:
hovering one tells what it is, elsewhere the time there, and a click jumps
there.

Record… makes an MP4 of the demo playing: a stretch of time, the kills
(of the player chosen above) one after the other, or only while a player is
watched. A second copy of the game records it in the background, without a
window on Linux and faster than real time, while you keep watching; the
video lands in the folder chosen, named from a pattern (`{demo}`, `{start}`,
`{player}`, `{date}`). Size, frames per second, quality, codec (H.264,
H.265 or the graphics card's H.264 encoder: VAAPI on Linux, VideoToolbox on
macOS), sound and its bitrate are settings, and the Advanced box takes
FFmpeg output options instead. It needs FFmpeg.

The app starts the game with a throwaway home folder, sends it console
commands through its pipe (`com_pipefile`) and shows what the game reports
in `demoindex.json` and `demostate.json` (`cl_demoFiles 1`). On Linux it
asks for Wayland first, then XWayland.

## Keys in the game window

During a demo, letters, numbers, Space, Tab and Enter stop it, so the app
binds other keys:

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
| `demonextkill [player]`, `demoprevkill [player]` | jump to before the next or previous kill, by that player if given |
| `demonextround`, `demoprevround` | jump to the next or previous round |
| `demoonly kills [player]` | play only the kills, by that player if given |
| `demoonly watched <player>` | play only while that player is shown |
| `demoonly` | play everything again |
| `timescale <speed>` | playback speed, 1 is normal |
| `demovideo <name> [end time]` | record into an MP4, see below |

`cl_demoKillBefore` and `cl_demoKillAfter` set how many seconds of each kill
are shown (4 and 2).

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
