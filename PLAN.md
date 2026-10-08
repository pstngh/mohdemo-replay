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

`ui_hud` off by default and hiding chat/kill messages/spectator hints,
`cg_followplayer` (live spectating), `.dm3` demos, `loopdemos` /
`stoploopdemos`, no team/weapon menus during demos, view bob with
`cg_animationviewmodel`, and a Linux x86_64 client-only CI build on `main`.

## Steps, in order

1. **Seek and rewind (engine).** A demo only plays forward (each snapshot is a
   delta of earlier ones), so seeking backwards restarts the demo, then reads
   ahead without drawing until the exact target time. Commands to jump to a
   time and to move by +/- N seconds. Watch out for cgame's server command
   buffer: commands read while cgame isn't running are "cycled out" if too many
   pile up, so let cgame consume them while skipping.
2. **Demo index (engine).** One pass when a demo loads, written to a file the
   app reads (the pipe only goes from the app to the game):
   - Kills: `print` server commands starting with `\x04`, like
     `Victim was perforated by Killer's' SMG in the head`. Match names against
     the player configstrings rather than parsing the wording. Unacknowledged
     commands are repeated in later messages, so dedupe by command sequence.
     The recorder's own kills also come as `\x03You killed X`.
   - Round ends: `print` of "Axis win!", "Allies win!" or "It's a draw!"; the
     next round starts about 3 seconds later. Only in round-based modes.
   - Who is watched: `ps.stats[STAT_INFOCLIENT]` while `PMF_CAMERA_VIEW` is
     set when spectating, otherwise the recorder.
   - The current demo time, for the app's clock.
3. **Kill and follow queues (engine).** Next/previous kill, play only kills
   (optionally by one player), play only the stretches where a given player is
   watched, next round.
4. **The app.** Starts `openmohaa` with a throwaway `fs_homepath`, sends
   commands through `com_pipefile`, shows a clickable list of kills and
   rounds, play/pause/speed/seek buttons, record button.
5. **Recording (engine).** Port ioquake3's `video-pipe` command
   (`cl_aviPipeFormat`, `FS_PipeOpenWrite`) into `code/client/cl_avi.cpp` and
   `cl_main.cpp`: the AVI stream goes into `ffmpeg -i pipe:0` and comes out as
   an H.264 MP4. Check that sound is recorded: the AVI writer gets its audio
   from the software mixer (`snd_mix.c`), so OpenAL sound may need turning off
   while recording. Settings in the app:
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
tested in game. The index can be tested offline: moharena-demo has two real
AA demos (`demos/1.dm_8`, 7 minutes, and `demos/mohdm6.dm_8`, 12 minutes, both
Free-For-All, so no rounds).
