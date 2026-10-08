/*
===========================================================================
Copyright (C) 2026 the OpenMoHAA team

This file is part of OpenMoHAA source code.

OpenMoHAA source code is free software; you can redistribute it
and/or modify it under the terms of the GNU General Public License as
published by the Free Software Foundation; either version 2 of the License,
or (at your option) any later version.

OpenMoHAA source code is distributed in the hope that it will be
useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with OpenMoHAA source code; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
===========================================================================
*/

// Added in OPM
//  mohdemoindex: indexes demos without the game, for the app to list what's
//  in them (players, kills, levels) without playing them:
//
//   mohdemoindex <output folder> <demo>...
//
//  Each index is the game's demoindex.json for that demo, written to
//  <output folder>/<demo name without extension>.json and replaced
//  atomically. The name of each demo done is printed on its own line as soon
//  as it's written, and demos that can't be read are skipped.

#include "cl_demoindex.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

// used by the message code
static cvar_t protocol = {};
static cvar_t shownet  = {};
cvar_t       *com_protocol = &protocol;
cvar_t       *cl_shownet   = &shownet;

static int ReadFile(void *ctx, void *buffer, int len)
{
    return (int)fread(buffer, 1, len, (FILE *)ctx);
}

static std::string DemoName(const char *path)
{
    std::string name  = path;
    size_t      slash = name.find_last_of("/\\");
    size_t      dot;

    if (slash != std::string::npos) {
        name = name.substr(slash + 1);
    }
    dot = name.rfind('.');

    return dot == std::string::npos || !dot ? name : name.substr(0, dot);
}

static bool IndexDemo(const char *folder, const char *demo)
{
    demoIndex_t index;
    std::string name = DemoName(demo);
    std::string path = std::string(folder) + "/" + name + ".json";
    std::string tmp  = path + ".tmp";
    FILE       *f;
    char       *json;
    bool        ok;

    f = fopen(demo, "rb");
    if (!f) {
        fprintf(stderr, "Couldn't open %s\n", demo);
        return false;
    }
    DemoIndex_Build(&index, ReadFile, f);
    fclose(f);

    json = DemoIndex_ToJSON(&index, name.c_str());
    DemoIndex_Free(&index);
    if (!json) {
        return false;
    }

    f  = fopen(tmp.c_str(), "wb");
    ok = f && fputs(json, f) >= 0;
    ok = f && !fclose(f) && ok;
#ifdef _WIN32
    // rename doesn't replace files there
    ok = ok && (remove(path.c_str()), true);
#endif
    ok = ok && !rename(tmp.c_str(), path.c_str());
    free(json);
    if (!ok) {
        fprintf(stderr, "Couldn't write %s\n", path.c_str());
        remove(tmp.c_str());
        return false;
    }

    printf("%s\n", name.c_str());
    fflush(stdout);
    return true;
}

int main(int argc, char **argv)
{
    int failed = 0;
    int i;

    if (argc < 3) {
        fprintf(stderr, "Usage: mohdemoindex <output folder> <demo>...\n");
        return 2;
    }

    protocol.integer = PROTOCOL_MOH;

    for (i = 2; i < argc; i++) {
        failed += !IndexDemo(argv[1], argv[i]);
    }
    return failed ? 1 : 0;
}
