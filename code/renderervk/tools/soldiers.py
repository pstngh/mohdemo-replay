#!/usr/bin/env python3
"""Writes a view list (shots.py's format) of a multiplayer scene: soldier
models around the camera, as players would be, and muzzle flash lights.

  soldiers.py <map> x y z yaw <tag> [--side DEG] [--what TEXT] > views/<list>.txt

The camera stands at x y z (a `tele` position) looking along yaw. 20
script_models with player TIKIs (models/player/*.tik, allied and axis
alternating) are spawned with `spmap` (the `script` command needs a single
player game): 16 in a fan of 60 degrees in front of the camera, 160 to 1060
units away, and 4 beside it, outside the view. Each is dropped to the floor
(physics_on makes droptofloor trace against the world; the velocity it
gives is cancelled, so the soldiers stand still), turned, given an
animation of the player's (run, walk, idle, crouch) and most a weapon in
the right hand. With `fixedtime 16` the poses are the same in every run.
Three shots: <tag>_front; <tag>_flash, the same view with three dynamic
lights next to three of the soldiers (1.6 s of game time later, so the
poses differ); <tag>_side, the view turned by --side degrees (default 90)
towards the soldiers beside the camera. Positions that fall inside a wall
or on a roof stay there: check the frames. The first shot comes more than
33 s of game time after the map is loaded, when the heads no longer blink
(the skeletor's blink times wrap at 32.8 s of Sys_Milliseconds), so the
faces are the same in every run too.
"""

import argparse
import math

TIKIS = ["allied_Airborne", "german_Wehrmacht_Soldier", "american_Ranger", "german_Elite_Sentry", "allied_SAS",
         "german_Panzer_Grenadier", "american_Army", "german_Waffenss_Shutze", "allied_Manon",
         "german_Afrika_Private", "allied_Pilot", "german_Kradshutzen"]
# weapon (None: unarmed) and an animation of models/player/base/anims_*.txt
WEAPONS = [("thompsonsmg", "smg_stand_run_fwd"), ("mp40", "mp40_stand_idle"), ("m1_garand", "rifle_crouch_idle"),
           ("kar98", "rifle_stand_walk_fwd"), ("bar", "mg_stand_idle"), ("mp44", "mp44_stand_run_fwd"),
           ("springfield", "rifle_stand_idle"), ("mp40", "mp40_crouch_walk_fwd"), (None, "unarmed_stand_run_fwd"),
           ("colt45", "pistol_stand_idle1"), ("shotgun", "shotgun_stand_run_fwd"), ("bazooka", "bazooka_stand_idle")]
# soldier index, color, radius of the muzzle flash lights
FLASHES = [(2, "1 0.8 0.4", 200), (5, "1 0.7 0.3", 240), (7, "1 0.85 0.5", 180)]
COUNT, BESIDE = 20, 4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("map")
    ap.add_argument("pos", nargs=3, type=float)
    ap.add_argument("yaw", type=float)
    ap.add_argument("tag")
    ap.add_argument("--side", type=float, default=90)
    ap.add_argument("--what", default="")
    args = ap.parse_args()
    x, y, z = args.pos
    yaw, front = args.yaw, COUNT - BESIDE

    places = []
    for i in range(COUNT):
        if i < front:
            a = yaw + (i / (front - 1) - 0.5) * 60 + (7 if i % 2 else -7)
            d = 160 + 900 * ((i * 0.618034) % 1.0)
        else:
            k = i - front
            a = yaw + (90 + 12 * (k // 2)) * (1 if k % 2 else -1)
            d = 260 + 140 * (k // 2)
        places.append((x + d * math.cos(math.radians(a)), y + d * math.sin(math.radians(a)), a))

    def g(v):
        return f"{v:g}"

    out = [f"# Multiplayer view with players{': ' + args.what if args.what else ''}. Written by",
           f"# soldiers.py {args.map} {g(x)} {g(y)} {g(z)} {g(yaw)} {args.tag} --side {g(args.side)} (see its",
           "# docstring): 20 player models around the camera, then three muzzle",
           "# flash lights and a view to the side.",
           f"spmap {args.map}"]
    out += [f"set spawn script_model model player/{TIKIS[i % len(TIKIS)]}.tik targetname s{i + 1}" for i in range(COUNT)]
    out.append("set wait 200")
    for i, (px, py, a) in enumerate(places):
        # physics_on gives a velocity of 0 0 1 that physics_off keeps: cancel it
        out += [f'set script s{i + 1} origin "{px:.0f} {py:.0f} {z + 40:.0f}"', f"set script s{i + 1} physics_on",
                f"set script s{i + 1} droptofloor 512", f"set script s{i + 1} physics_off",
                f'set script s{i + 1} physics_velocity "0 0 -1"']
    out.append("set wait 200")  # physics_off resets the model: animate after it
    for i, (px, py, a) in enumerate(places):
        weapon, anim = WEAPONS[i % len(WEAPONS)]
        out += [f"set script s{i + 1} angle {(a + 180 + (i * 37) % 90 - 45) % 360:.0f}", f"set script s{i + 1} anim {anim}"]
        if weapon:
            out.append(f"set script s{i + 1} attachmodel models/weapons/{weapon}.tik tag_weapon_right")
    # the skeletor keeps the time of the next blink in a short
    # (skeletor_c::m_timeNextBlink): eyes blink at random during the first
    # 32.8 s of Sys_Milliseconds, never after, so the shots come later
    out.append("set wait 27000")
    view = f"{g(x)} {g(y)} {g(z)}"
    out.append(f"shot {args.tag}_front {view} 0 {g(yaw)} 0")
    out += [f"set spawn fx/dummy.tik targetname {args.tag}l{k}" for k in (1, 2, 3)]
    out.append("set wait 200")
    for k, (i, color, radius) in enumerate(FLASHES, 1):
        sx, sy = (float(f"{v:.0f}") for v in places[i][:2])  # the origin written above
        dx, dy = x - sx, y - sy
        d = math.hypot(dx, dy)
        out.append(f'set script {args.tag}l{k} origin "{sx + 24 * dx / d:.0f} {sy + 24 * dy / d:.0f} {z + 50:.0f}"')
        out.append(f"set script {args.tag}l{k} light {color} {radius}")
    out.append(f"shot {args.tag}_flash {view} 0 {g(yaw)} 0")
    out.append(f"shot {args.tag}_side {view} 0 {g((yaw + args.side) % 360)} 0")
    print("\n".join(out))


if __name__ == "__main__":
    main()
