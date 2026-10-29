#!/usr/bin/env python3
"""Lists the shaders of a MOHAA map with their properties and where their
surfaces are, to pick camera positions for tests (standard library only).

  bspinfo.py <map, e.g. dm/mohdm1> [keyword ...]
  bspinfo.py <map> --view x y z [x y z ...]

Prints the worldspawn keys (farplane fog, sky), then every shader used by
world surfaces whose name or script contains one of the keywords (default:
polygonoffset), with the centers of its first surfaces, terrain patches
(type "terrain", 512x512 units, center at the mean height) included. Static
models whose path contains a keyword are listed with their origins, after a
count of all of them. The decals of the map's .dcl file are counted too (the
renderer only loads them when the .dcl map time matches the BSP's) and
listed, projection point and shader, when a keyword matches their shader or
is "decal". Reads the pk3 files of $MOH_BASEPATH/main (default
~/Documents/moh/main). The player spawn points (for tele) are info_player_*
entities in the entity lump.

--view prints the cluster and area of the eye of the view lists' `shot x y
z ...` (the eye is DEFAULT_VIEWHEIGHT, 82 units, above the tele position):
cluster -1 means outside the world, where part of the frame is undefined
(see README.md, Testing).
"""

import glob
import os
import re
import struct
import sys
import zipfile

BASE = os.path.join(os.environ.get("MOH_BASEPATH", os.path.expanduser("~/Documents/moh")), "main")


def pk3s():
    # later pk3s override earlier ones, like the game (alphabetical order)
    return sorted(glob.glob(os.path.join(BASE, "*.pk3")), key=lambda p: os.path.basename(p).lower())


def read_file(name):
    data = None
    for p in pk3s():
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if n.lower() == name.lower():
                    data = z.read(n)
    return data


def shader_scripts():
    text = {}
    for p in pk3s():
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if n.lower().startswith("scripts/") and n.lower().endswith(".shader"):
                    text[n.lower()] = z.read(n).decode("latin-1")
    shaders = {}
    for body in text.values():
        body = re.sub(r"//[^\n]*", "", body)
        pos = 0
        while True:
            m = re.search(r"([^\s{}]+)\s*\{", body[pos:])
            if not m:
                break
            name = m.group(1).lower()
            start = pos + m.end()
            depth, i = 1, start
            while i < len(body) and depth:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                i += 1
            shaders.setdefault(name, body[start:i - 1])
            pos = i
    return shaders


def view_clusters(bsp, lump, version, points):
    # dnode_t: plane, children[2], mins[3], maxs[3]; dplane_t: normal, dist;
    # dleaf_t: cluster, area, ... (64 bytes from version 18, 56 before)
    poff, _ = lump(1)
    noff, _ = lump(9)
    loff, _ = lump(8)
    leafsize = 64 if version > 17 else 56
    for x, y, z in points:
        eye = (x, y, z + 82)
        num = 0
        while num >= 0:
            plane, c0, c1 = struct.unpack_from("<3i", bsp, noff + num * 36)
            nx, ny, nz, dist = struct.unpack_from("<4f", bsp, poff + plane * 16)
            num = c0 if nx * eye[0] + ny * eye[1] + nz * eye[2] - dist >= 0 else c1
        cluster, area = struct.unpack_from("<2i", bsp, loff + (-1 - num) * leafsize)
        note = "  (outside the world)" if cluster < 0 else ""
        print(f"view {x:g} {y:g} {z:g} (eye z {z + 82:g}): cluster {cluster} area {area}{note}")


def main():
    if len(sys.argv) > 2 and sys.argv[2] == "--view":
        coords = [float(v) for v in sys.argv[3:]]
        if not coords or len(coords) % 3:
            sys.exit("--view needs x y z triples")
        bsp = read_file(f"maps/{sys.argv[1]}.bsp")
        if bsp is None:
            sys.exit("map not found")
        version = struct.unpack_from("<i", bsp, 4)[0]
        lumps = [struct.unpack_from("<ii", bsp, 12 + 8 * i) for i in range(28)]
        view_clusters(bsp, lambda n: lumps[n + 1 if version <= 18 and n > 12 else n], version,
                      [coords[i:i + 3] for i in range(0, len(coords), 3)])
        return
    mapname = sys.argv[1]
    keywords = [k.lower() for k in sys.argv[2:]] or ["polygonoffset"]
    bsp = read_file(f"maps/{mapname}.bsp")
    if bsp is None:
        sys.exit("map not found")
    ident, version, checksum = struct.unpack_from("<4sii", bsp, 0)
    lumps = [struct.unpack_from("<ii", bsp, 12 + 8 * i) for i in range(28)]

    def lump(n):
        if version <= 18 and n > 12:
            n += 1
        return lumps[n]

    off, ln = lump(0)
    shaders = []
    for i in range(ln // 140):
        name = bsp[off + i * 140:off + i * 140 + 64].split(b"\0")[0].decode("latin-1")
        shaders.append(name)
    off, ln = lump(3)
    surfs = [struct.unpack_from("<3i2i2i5i3f9f2if", bsp, off + i * 108) for i in range(ln // 108)]
    voff, _ = lump(4)
    eoff, eln = lump(14)
    ents = bsp[eoff:eoff + eln].decode("latin-1")
    world = ents[:ents.find("}")]
    print(f"version {version}, {len(shaders)} shaders, {len(surfs)} surfaces")
    print("worldspawn:", " ".join(l.strip() for l in world.splitlines() if l.strip() not in ("{",)))

    # player spawn points: positions for tele (add the eye height, about 60)
    spawns = []
    for ent in re.findall(r"\{([^}]*)\}", ents):
        keys = dict(re.findall(r'"([^"]+)"\s+"([^"]*)"', ent))
        if keys.get("classname", "").startswith("info_player") and "origin" in keys:
            spawns.append(f"{keys['classname']} {keys['origin']} angle {keys.get('angle', keys.get('angles', '0'))}")
    print(f"{len(spawns)} spawn points:")
    for line in spawns[:8]:
        print("  ", line)

    # LOD terrain: cTerraPatch_t (388 bytes), origin x << 6, y << 6, heights
    # z0 + 2 * heightmap over 8x8 cells of 64 units
    toff, tln = lump(22)
    terrain = {}
    for i in range(tln // 388):
        o = toff + i * 388
        x, y, z0, shader = struct.unpack_from("<bbhH", bsp, o + 36)
        hmap = bsp[o + 304:o + 385]
        terrain.setdefault(shaders[shader], []).append(
            (x * 64 + 256, y * 64 + 256, z0 + 2 * sum(hmap) / 81.0))
    if terrain:
        print(f"{sum(len(v) for v in terrain.values())} terrain patches")

    # static models: cStaticModel_t (164 bytes)
    soff, sln = lump(25)
    statics = [struct.unpack_from("<128s3f3ff", bsp, soff + i * 164) for i in range(sln // 164)]
    counts = {}
    for m in statics:
        name = m[0].split(b"\0")[0].decode("latin-1").lower()
        counts[name] = counts.get(name, 0) + 1
    print(f"{len(statics)} static models, {len(counts)} different")
    for name, n in sorted(counts.items()):
        if not any(k in name for k in keywords):
            continue
        origins = [m[1:4] for m in statics if m[0].split(b"\0")[0].decode("latin-1").lower() == name]
        print(f"   {n:3d} {name}: " + ", ".join(f"({o[0]:.0f} {o[1]:.0f} {o[2]:.0f})" for o in origins[:4]))

    # decals: dclHeader_t (5 ints), map time (32 bytes, version 2), then
    # dclSavedMarkDef_t (124 bytes: shader, vPos, vProjection, ...)
    dcl = read_file(f"maps/{mapname}.dcl")
    if dcl:
        ident, dver, _, ndecals, nfrags = struct.unpack_from("<4s4i", dcl, 0)
        dtime = dcl[20:52].split(b"\0")[0].decode("latin-1") if dver == 2 else ""
        mtime = re.search(r'"map_time"\s+"([^"]*)"', world)
        match = mtime is not None and mtime.group(1) == dtime
        print(f"{ndecals} decals in maps/{mapname}.dcl, map time {dtime!r}: "
              f"{'loaded' if match else 'not loaded (BSP map time differs)' if dtime else 'not loaded (version 1 file, no map time)'}")
        off = 52 if dver == 2 else 20
        for i in range(ndecals):
            d = struct.unpack_from("<64s3f", dcl, off + i * 124)
            name = d[0].split(b"\0")[0].decode("latin-1")
            if "decal" in keywords or any(k in name.lower() for k in keywords):
                print(f"   decal {name} at {d[1]:.0f} {d[2]:.0f} {d[3]:.0f}")

    scripts = shader_scripts()
    by_shader = {}
    for s in surfs:
        by_shader.setdefault(shaders[s[0]], []).append(s)
    for name, lst in terrain.items():
        by_shader.setdefault(name, []).extend(("terrain", c) for c in lst)
    for name, lst in sorted(by_shader.items()):
        body = scripts.get(name.lower(), "")
        low = body.lower()
        if not any(k in low or k in name.lower() for k in keywords):
            continue
        props = sorted({k for k in ("polygonoffset", "blendfunc", "alphafunc", "deformvertexes", "tcmod",
                                     "nofog", "fogonly", "sky", "portal", "cull", "sort", "rgbgen", "alphagen")
                        if k in low})
        print(f"\n{name}  [{', '.join(props)}]  {len(lst)} surfaces")
        for s in lst[:6]:
            if s[0] == "terrain":
                c = s[1]
                print(f"   terrain patch   center {c[0]:8.1f} {c[1]:8.1f} {c[2]:8.1f}")
                continue
            first, count = s[3], s[4]
            pts = [struct.unpack_from("<3f", bsp, voff + (first + k) * 44) for k in range(count)]
            # drawVert_t: xyz, st, lightmap, normal, color
            nrm = struct.unpack_from("<3f", bsp, voff + first * 44 + 28)
            c = [sum(p[k] for p in pts) / len(pts) for k in range(3)]
            print(f"   type {s[2]} verts {count:4d} center {c[0]:8.1f} {c[1]:8.1f} {c[2]:8.1f}"
                  f"  normal {nrm[0]:5.2f} {nrm[1]:5.2f} {nrm[2]:5.2f}")


if __name__ == "__main__":
    main()
