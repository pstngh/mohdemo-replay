# MoH Demo Replay

Rewatch Medal of Honor: Allied Assault demos (`.dm3`) with buttons: the list
of demos, their kills and rounds, play/pause, speed and seeking. It needs
OpenMoHAA built from this repository, which adds the demo commands, Python 3
and PySide6.

    python3 replay/mohreplay.py [--exe PATH] [--game FOLDER] [--demos FOLDER] [DEMO]

On the first run, Settings asks for the game program (found by itself when
built in `.cmake/RelWithDebInfo/`), the folder with the game files (the one
with `main/Pak0.pk3`) and the folder with the demos. Double-click a demo to
play it, a kill or a round to jump to it.

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

`cl_demoKillBefore` and `cl_demoKillAfter` set how many seconds of each kill
are shown (4 and 2).
