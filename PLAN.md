# mohdemo-replay plan

Goal: rewatch old Medal of Honor: Allied Assault matches from their demos, in a
window with buttons, and record clips. Not for cheat review.

## Decisions

- Allied Assault demos only (`.dm3`, protocol 8).
- Linux (Wayland) and macOS. No Windows: the app controls the game through
  `com_pipefile`, which OpenMoHAA only implements on Unix-like systems.
- Demos were recorded both while spectating and while playing; handle both.
- The game keeps working on its own: every new feature is a console command
  first, the app only sends those commands.
- The app is written from scratch, in this repo (Python + PySide6, Qt 6),
  Wayland native. The game is launched with `SDL_VIDEODRIVER=wayland`, falling
  back to XWayland if that fails.
  [fecmtc/moharena-demo](https://github.com/fecmtc/moharena-demo) is a
  reference for ideas only: it has no licence, so none of its code is copied.
- Recording: the game pipes its own frames into FFmpeg, which writes an MP4
  (see step 5). Screen capture doesn't work with FFmpeg on Wayland.
- Playback with VSync on at 60 fps: launch with `r_swapInterval 1` and
  `com_maxfps 60` (the engine defaults are 0 and 85).

## Principles

As light, lean, robust, reliable and portable as possible:

- Thin app, smart engine: exact seeking, the index and recording are engine
  console commands, usable with key binds without the app. The app is buttons
  and a list, a few hundred lines.
- No extras: no x-ray, no overlay HUD, no window-moving tricks (Wayland
  forbids them), no AppleScript. Dependencies: Python, PySide6, FFmpeg.
- The game reports its state (time, index) in a file the app reads; the app
  never guesses. It copes with the game crashing or quitting, broken demos and
  missing maps, and never blocks on the pipe.
- Same code on Linux (Wayland) and macOS, no build step for the app.
- Engine changes stay small and marked, one commit per step, each built
  before it's pushed. The index has tests that run on real demos offline.

## Already on main

`ui_hud` (on by default, kept across map and demo loads) also hiding
chat/kill messages/spectator hints,
`cg_followplayer` (live spectating), `.dm3` demos, `loopdemos` /
`stoploopdemos`, no team/weapon menus during demos, view bob with
`cg_animationviewmodel`, a Linux x86_64 client-only CI build on `main`,
step 1: `demoseek <time>` / `demoskip <time>` (seconds or minutes:seconds),
demos that load a new level midway (a map change, in about a third of the
test demos) playing through it, and `demopause [0|1]` (toggles without an
argument): the demo and its sound stop and go on from the same time, and
seeking keeps it paused. No more snapshots dropped when the recorder's
connection lagged, and no "connection interrupted" icon in demos. Step 2: the
demo index. Step 3: kill, round and player navigation. Step 4: the app,
`replay/mohreplay.py`. Step 5: `demovideo`, MP4 with sound, and the app's
Record… button.

Since then: tests for the app against a fake game (`replay/tests`, run by
`ctest` and CI); kills and rounds marked on the time slider, which jumps
where it's clicked; `mohdemoindex`, which indexes every demo in the
background so the list shows their length and levels and finds them by
player; `demoonly multikills [player]` (`cl_demoMultiKill`, 3 s) and a
Multi-kills tab; a queue of recordings in a Videos panel, of the kills
selected or of several demos, clips joined by FFmpeg without encoding them
again. Fixed on the way: recordings shorter than a poll, half videos kept
when FFmpeg quit, the level's music gone after the first round, SIGTERM
hanging the game when it lands in the renderer, and a macOS app bundle not
taken for the game; CI now builds `main` for macOS too.

## Steps, in order

1. **Seek and rewind (engine).** A demo only plays forward (each snapshot is a
   delta of earlier ones), so seeking backwards restarts the demo, then reads
   ahead without drawing until the exact target time. Commands to jump to a
   time and to move by +/- N seconds. Watch out for cgame's server command
   buffer: commands read while cgame isn't running are "cycled out" if too many
   pile up, so let cgame consume them while skipping. Done.
2. **Demo index (engine).** Done. When a demo loads (`demo`, `loopdemos`, not
   a seek), `code/client/cl_demoindex.cpp` reads it once with its own copy of
   the client's parsing (about 1 s for 50 minutes) and keeps the index for
   step 3. With `cl_demoFiles 1`, the game writes two files in
   `<fs_homepath>/main/`, replaced atomically, times in msec from the demo's
   first snapshot, the same as `demoseek`:
   - `demoindex.json`, once per demo: `duration`, `truncated`, `recorder`,
     `maps` (levels loaded midway too), `watched` (who is shown from when:
     the followed player while spectating, -1 in free view, else the
     recorder), `kills` (`killer`/`killerName`, `victim`/`victimName`,
     `text`; client numbers are -1 when unknown, killer is -1 for suicides),
     `rounds` (when the level restarts: snapshots toggle
     `SNAPFLAG_SERVERCOUNT`, about 3.3 s after a round end, also without
     one, like at the start of a match) and `roundEnds` (`text`: "Axis
     win!", "Allies win!", "It's a draw!").
   - `demostate.json`: `demo` (empty when none plays), `time`, `duration`,
     `paused`, `seeking`, `only` and `player` (what `demoonly` plays),
     `recording` (the video being written, empty once FFmpeg is done);
     written when one of them changes, at most every 100 ms while the time
     goes on.

   Found on the test demos: kills are `\x04` prints starting with the victim,
   in about 25 wordings; the recorder's `\x03You killed X` repeats one of
   them. Names are matched as whole words against the players, then the
   players who left. A recording starts at the first full snapshot after the
   gamestate, so the commands in between are missing, sometimes with a
   player's name: those are learned from "X has entered the battle" and the
   like, without client numbers.
3. **Kill and follow queues (engine).** Done, from the index:
   - `demonextkill [player]`, `demoprevkill [player]`: to
     `cl_demoKillBefore` seconds (4) before the next or previous kill, by
     that player if given; previous replays the current kill when more than
     a second into it.
   - `demonextround`, `demoprevround`: to the next or previous round start
     or level load; previous restarts the current round when more than 3
     seconds into it.
   - `demoonly kills [player]`: only the kills, from `cl_demoKillBefore`
     before to `cl_demoKillAfter` (2) after, joined when less than a second
     apart. `demoonly watched <player>`: only while that player is shown.
     `demoonly` alone: everything. It jumps over the rest, pauses after the
     last stretch and plays everything from there; a new demo plays
     everything.

   Players are given by name, without colors or case, like the index's
   `killerName` and `watched` names.
4. **The app.** Done: `replay/mohreplay.py` (see `replay/README.md`). It
   starts `openmohaa` with a throwaway `fs_homepath` holding a link to the
   demos folder, `cl_demoFiles 1` and key binds (Pause, arrows, PgUp/PgDn),
   asking for Wayland first, then XWayland if the game quits within 5 s. It
   sends commands through `com_pipefile` without ever blocking (commands
   wait in a queue until the game reads the pipe), reads `demoindex.json`
   and `demostate.json` every 100 ms, and shows the demos (by date), the
   kills (by player) and rounds to jump to, play/pause/kill/round/skip
   buttons, a time slider and the speed (`timescale`). When the game quits
   or crashes, it says so, and a double-click starts it again; missing maps
   show in the status bar. On closing it sends `quit`, then SIGTERM after
   3 s and SIGKILL after 2 more: five test copies running with
   `r_swapInterval 1` once hung in a futex wait and ignored SIGTERM after new
   game windows covered them (fresh copies exit fine; suspected: VSync on
   Wayland blocking while the window isn't shown). The record button comes
   with step 5.
5. **Recording (engine).** Engine done. OpenMoHAA had the AVI writer
   (`cl_avi.cpp`) turned off; it's back, and like ioquake3's `video-pipe`
   (`cl_aviPipeFormat`, `FS_PipeOpenWrite`) its stream can go into FFmpeg:
   `demovideo <name> [end time]` writes `videos/<name>.mp4`, see
   `replay/README.md`. Only the OpenAL sound system is built, so there is no
   software mixer to take the sound from: with `s_loopback 1` at startup,
   OpenAL Soft's loopback device renders it (`ALC_SOFT_loopback`), exactly
   `rate / cl_aviFrameRate` samples per frame, and nothing is played. So the
   app records in a second game, offscreen. Frames step exactly
   1/`cl_aviFrameRate` s (they were rounded up, 2% fast at 60), seeks and
   pauses aren't recorded, and a write error (FFmpeg missing or quitting)
   stops the recording instead of the demo. 30 s at 960x540 and 60 fps take
   about 10 s to record. App done too: Record… starts a second game
   (offscreen on Linux, `s_loopback 1`, no frame cap, no VSync), sends it
   `demoseek` + `demovideo` (or `demoonly` first), follows `recording` in
   its `demostate.json` and moves the video to the chosen folder; 10 s at
   1280x720 take about 17 s from the click. Its settings:
   - resolution: the game's window size (`r_mode -1`, `r_customwidth`,
     `r_customheight`), optionally drawn larger and scaled down;
   - frame rate: 60 (`cl_aviFrameRate 60`); the recorder steps the game 1/60 s
     per frame, so videos are smooth whatever the screen does;
   - quality: one slider (CRF);
   - codec: H.264 (default), H.265, or the hardware encoder (VideoToolbox on
     macOS, VAAPI on Linux);
   - sound on/off and bitrate, output folder and file name pattern;
   - an advanced box for raw FFmpeg options.

## Testing

Cloud sessions have no game files, so playback can't be tried there; it's
tested in game. The app is tested against a fake game, without game files
or a screen: `python3 replay/tests/test_mohreplay.py`, with
`MOHREPLAY_TEST_GAME` and `MOHREPLAY_TEST_DEMOS` set to also play and
record a real demo. The index is tested offline, without game files:
`DEMOINDEX_TEST_DEMOS=<folder of demos> ctest -R demoindex` (skipped without
it), or `test_demoindex [--json] <demo or folder>`. moharena-demo has two real
AA demos (`demos/1.dm_8`, 7 minutes, and `demos/mohdm6.dm_8`, 12 minutes, both
Free-For-All, so no rounds); the 696 test demos all pass.

Locally, the game can be driven through `com_pipefile` and checked with
`screenshotJPEG` and `demoseek` (prints the time). `SDL_VIDEODRIVER=offscreen`
runs it without a window, with the GPU, and `QT_QPA_PLATFORM=offscreen` does
the same for the app, which can then be driven from a script that imports it
(`QSettings` keeps the paths; `Window.grab()` takes screenshots). During a demo,
letter and number keys act as Escape, so test binds go on the arrows,
Home/End and PgUp/PgDn. Demos of maps missing from the game files (custom
maps) don't load, and `1371e2f1f50b5b41-obj-obj_team2.dm3` shows the loading
screen throughout: its recorder never joined, so the view is outside the map
(the same with upstream OpenMoHAA).
