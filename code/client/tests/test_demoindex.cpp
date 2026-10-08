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

// Indexes real Allied Assault demos (.dm3, .dm_8) and checks what comes out:
//
//   test_demoindex [--json] <demo or folder>...
//
// Without arguments, the demos are taken from the folder in the
// DEMOINDEX_TEST_DEMOS environment variable, and the test is skipped if
// it isn't set, as there are no demos in the repository.

#include "../cl_demoindex.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <string>
#include <vector>

// used by the message code
static cvar_t protocol = {};
static cvar_t shownet  = {};
cvar_t       *com_protocol = &protocol;
cvar_t       *cl_shownet   = &shownet;

static const int TEST_SKIPPED = 77;

static int ReadFile(void *ctx, void *buffer, int len)
{
    return (int)fread(buffer, 1, len, (FILE *)ctx);
}

static bool IsDemo(const std::string& name)
{
    std::string ext = std::filesystem::path(name).extension().string();

    return !Q_stricmp(ext.c_str(), ".dm3") || !Q_stricmp(ext.c_str(), ".dm_8");
}

static void AddDemos(const std::string& path, std::vector<std::string>& demos)
{
    std::error_code error;

    if (!std::filesystem::is_directory(path, error)) {
        demos.push_back(path);
        return;
    }

    for (const auto& entry : std::filesystem::directory_iterator(path, error)) {
        if (IsDemo(entry.path().filename().string())) {
            demos.push_back(entry.path().string());
        }
    }
}

static bool Check(const char *demo, bool condition, const char *what)
{
    if (!condition) {
        printf("FAILED %s: %s\n", demo, what);
    }
    return condition;
}

static bool TestDemo(const char *demo, bool json)
{
    demoIndex_t index;
    FILE       *f;
    char       *text;
    bool        ok = true;
    int         kills = 0, rounds = 0, roundEnds = 0, watched = 0, maps = 0;
    int         lastTime = 0;
    int         i;

    f = fopen(demo, "rb");
    if (!Check(demo, f != NULL, "can't open it")) {
        return false;
    }

    DemoIndex_Build(&index, ReadFile, f);
    fclose(f);

    for (i = 0; i < index.numEvents; i++) {
        const demoEvent_t *ev = &index.events[i];

        ok &= Check(demo, ev->time >= lastTime, "event times go back");
        ok &= Check(demo, ev->time <= index.duration, "an event is after the end");
        lastTime = ev->time;

        switch (ev->type) {
        case DEMOEVENT_MAP:
            ok &= Check(demo, ev->text[0] != 0, "a level has no map name");
            maps++;
            break;
        case DEMOEVENT_WATCH:
            ok &= Check(demo, ev->client < MAX_CLIENTS, "a watched player is out of range");
            watched++;
            break;
        case DEMOEVENT_KILL:
            ok &= Check(demo, ev->text[0] != 0, "a kill has no message");
            ok &= Check(demo, !ev->otherName[0] || !strncmp(ev->text, ev->otherName, strlen(ev->otherName)),
                        "a kill message doesn't start with the victim");
            ok &= Check(demo, !ev->name[0] || strstr(ev->text + strlen(ev->otherName), ev->name),
                        "a kill message doesn't have the killer");
            kills++;
            break;
        case DEMOEVENT_ROUNDSTART:
            rounds++;
            break;
        case DEMOEVENT_ROUNDEND:
            roundEnds++;
            break;
        }
    }

    ok &= Check(demo, maps > 0 || index.truncated, "no level");
    ok &= Check(demo, index.events && index.events[0].type == DEMOEVENT_MAP && !index.events[0].time, "doesn't start with a level");

    text = DemoIndex_ToJSON(&index, demo);
    ok &= Check(demo, text && text[0] == '{' && text[strlen(text) - 2] == '}', "bad JSON");
    if (json) {
        fputs(text, stdout);
    } else {
        printf(
            "%s %s: %d:%02d, %d levels, %d rounds, %d round ends, %d kills, %d watched%s\n",
            ok ? "ok" : "FAILED",
            demo,
            index.duration / 60000,
            index.duration / 1000 % 60,
            maps,
            rounds,
            roundEnds,
            kills,
            watched,
            index.truncated ? ", truncated" : ""
        );
    }

    free(text);
    DemoIndex_Free(&index);
    return ok;
}

int main(int argc, char **argv)
{
    std::vector<std::string> demos;
    bool                     json   = false;
    int                      failed = 0;
    int                      i;

    protocol.integer = PROTOCOL_MOH;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--json")) {
            json = true;
        } else {
            AddDemos(argv[i], demos);
        }
    }

    if (argc == 1) {
        const char *folder = getenv("DEMOINDEX_TEST_DEMOS");
        if (!folder || !folder[0]) {
            printf("Skipped: set DEMOINDEX_TEST_DEMOS to a folder of Allied Assault demos\n");
            return TEST_SKIPPED;
        }
        AddDemos(folder, demos);
    }

    if (demos.empty()) {
        printf("No demos found\n");
        return 1;
    }

    for (const std::string& demo : demos) {
        failed += !TestDemo(demo.c_str(), json);
    }

    if (!json) {
        printf("%d of %d demos failed\n", failed, (int)demos.size());
    }
    return failed ? 1 : 0;
}
