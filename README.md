# MoH Demo Replay

Rewatch and record **Medal of Honor: Allied Assault** multiplayer demos. A
small desktop app lists your demos and what happens in them (kills and
rounds), jumps straight to any moment, and records MP4 videos
with sound. It runs on a modified [OpenMoHAA](https://github.com/openmoh/openmohaa),
the open-source engine for the game, here the Vulkan port of
[MoHAA Reloaded](https://github.com/pstngh/MoHAA-Reloaded/tree/vulkan),
which does the work through new console commands.

![The app with a 52-minute demo of The Hunt: the demos where Mako plays, and the multi-kills of this one](replay/screenshot.png)

*The demos where Mako plays, and the multi-kills of the one playing. The
player names are made up.*

## What it does

- **Find demos.** The list shows each demo's date, length, recorder, levels
  and rules: default or realism servers, told apart by how fast the recorder
  runs with each weapon. Demos under 5 minutes are left out. Show only
  default or realism demos, and filter by demo name, level or player: type
  a name to see every demo that player is in, with their kills.
- **Choose the kills.** By killer, by victim and by weapon (sniper, rifle,
  SMG, StG / BAR, shotgun, pistol, bazooka, melee), several of each, on
  their own or together: the list shows only the demos with such kills, and
  the demo playing lists, plays and records only those.
- **Move around a demo.** Its kills and rounds are listed and
  marked on the time slider. A click jumps there, 4 seconds before a kill.
  Pause, skip 10 seconds, go to the next or previous kill or round, play
  from 0.25× to 4×.
- **Watch only what matters.** Play only the kills chosen, of
  everyone, of some players (while the demo shows them) or of whoever is
  shown (the recorder's frags and those of the players they follow), or
  only while a player is watched. The rest is skipped.
- **Record videos.** MP4 with sound, at the size and frame rate you choose,
  recorded in the background faster than real time while you keep watching.
  Record a stretch of time, the kills, the kills you select,
  or the kills chosen or the frags shown across many demos, in one video or
  one each. Videos wait in a queue.
- **No app needed.** Everything is a game console command (`demoseek`,
  `demonextkill`, `demoonly`, `demovideo`…), so it all works with key binds
  too.

The game's console also plays multi-kills (`demoonly multikills`): two
kills or more by one player, each at most 3 seconds after the one before.

## Requirements

- Linux (Wayland or X11), with a GPU and a Vulkan 1.1 driver (Mesa's RADV,
  ANV or NVK, or NVIDIA's). Not Windows: the app talks to the game through
  a named pipe, which the game only has on Unix-like systems.
- The game files of Medal of Honor: Allied Assault (the folder with
  `main/Pak0.pk3`), and the files of any custom map the demos use.
- Allied Assault demos (`.dm3`), recorded while playing or spectating.
  Spearhead and Breakthrough demos aren't supported.
- Python 3 and PySide6 for the app, FFmpeg to record.
- To build the game: CMake 3.25 or later, Ninja, a C++ compiler, Flex,
  Bison, libcurl, SDL 3 (3.2.0 or later) and the Vulkan headers 1.4.

On Ubuntu or Debian:

```bash
sudo apt install cmake ninja-build g++ flex bison libcurl4-openssl-dev python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets ffmpeg
```

SDL 3 is `libsdl3-dev` on Ubuntu 25.04 and later and Debian 13. Ubuntu 22.04
and 24.04 have none, and their Vulkan headers are too old: build both as
[MoHAA Reloaded's README](https://github.com/pstngh/MoHAA-Reloaded/tree/vulkan#building)
shows, and add `-DSDL3_DIR=...` and `-DVulkanHeaders_DIR=...` to the
configure line below.

## Build

```bash
git clone https://github.com/pstngh/mohdemo-replay
cd mohdemo-replay
cmake -B .cmake -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo -DBUILD_SERVER=OFF
cmake --build .cmake
```

This makes the game (`openmohaa`) and the demo indexer (`mohdemoindex`) in
`.cmake/RelWithDebInfo`, where the app looks for them. With an SDL 3 built
by hand, copy its `libSDL3.so.0` there too.

## Run

```bash
python3 replay/mohreplay.py
```

The first time, Settings asks for the game program (found by itself in
`.cmake/RelWithDebInfo`), the game files folder and the demos folder. The
demos are then indexed in the background, once: about two minutes for 700.
Double-click a demo to play it. The game plays it in its own window next to
the app; Wayland doesn't let one program show another's window inside its
own.

The folders can also be given on the command line, with a demo to play:

```bash
python3 replay/mohreplay.py --exe PATH --game FOLDER --demos FOLDER DEMO
```

In the game window, letters and numbers stop a demo, so the app binds other
keys:

| Key | Does |
| --- | --- |
| Pause | pause or play |
| Left / Right | back or forward 5 seconds |
| Down / Up | previous or next kill |
| PgDn / PgUp | previous or next round |

[replay/README.md](replay/README.md) has the details of the app, the console
commands and their settings, and recording.

## How it works

- The app starts the game with a throwaway home folder, sends it console
  commands through a pipe (`com_pipefile`) and shows what the game reports
  in two files, `demostate.json` and `demoindex.json`. It never waits on the
  game, and copes with it quitting or crashing.
- A demo only plays forward, so going back replays it from the start
  without drawing, up to the exact time asked.
- `mohdemoindex` reads a demo the way the game does and lists its kills,
  rounds, levels and who is watched when, without game files. The app keeps
  the indexes in `~/.cache/mohdemo-replay` until the demo or `mohdemoindex`
  changes.
- A second copy of the game records, without a window on Linux. It steps
  exactly one frame time per frame and pipes the frames to FFmpeg, and
  the sound mixer renders the sound of each frame, so the video stays
  smooth and in sync however fast it's made.

[PLAN.md](PLAN.md) has the decisions behind it and how it was built.

## Tests

```bash
python3 replay/tests/test_mohreplay.py
```

The app's tests run against a fake game, so they need neither game files
nor a screen. With `MOHREPLAY_TEST_GAME` (the game files folder) and
`MOHREPLAY_TEST_DEMOS` (a folder of demos) set, they also play and record a
real demo with the game built in `.cmake`. `ctest` in `.cmake` runs them
with the engine's tests; the demo index is checked on real demos with
`DEMOINDEX_TEST_DEMOS` set to a folder of them.

## Credits and license

Built on the `vulkan` branch of
[MoHAA Reloaded](https://github.com/pstngh/MoHAA-Reloaded) (its Vulkan
renderer and JayRewd's Omaha sound mixer), itself built on
[OpenMoHAA](https://github.com/openmoh/openmohaa),
[ioquake3](https://github.com/ioquake/ioq3) and the F.A.K.K SDK. For the
game itself (installing it, playing online, running a server), see
OpenMoHAA and the [documentation](docs/markdown) kept from it.

GPL-2.0, see [COPYING.txt](COPYING.txt). Not affiliated with or endorsed by
Electronic Arts.
