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
//  Demo index: the kills, round ends, levels and watched players of a demo,
//  read in one pass without playing it. It only needs the message code, so
//  it's also built into a test that runs on demo files without game files.

#pragma once

#include "../qcommon/q_shared.h"
#include "../qcommon/qcommon.h"

typedef enum {
    DEMOEVENT_MAP,        // a level was loaded: text is the map name
    DEMOEVENT_WATCH,      // the player shown from now on: client
    DEMOEVENT_KILL,       // client killed other, client is -1 for no killer
    DEMOEVENT_ROUNDSTART, // the level restarted, as at the start of a round
    DEMOEVENT_ROUNDEND    // text is the message, like "Axis win!"
} demoEventType_t;

typedef struct {
    demoEventType_t type;
    int             time;   // msec from the demo's first snapshot
    int             client; // killer or watched player, -1 if none or unknown
    int             other;  // victim, -1 if unknown
    char            name[MAX_NAME_LENGTH];
    char            otherName[MAX_NAME_LENGTH];
    char            text[MAX_STRING_CHARS];
    // for a level: the recorder's snapshots at a top speed only realism
    // servers give, and at one only default servers give
    int realismTicks;
    int defaultTicks;
} demoEvent_t;

typedef struct {
    int          numEvents;
    demoEvent_t *events;
    int          duration;  // msec from the first snapshot to the last one
    int          recorder;  // client number of the player who recorded it
    char         recorderName[MAX_NAME_LENGTH];
    qboolean     truncated; // stopped at broken data
    int          maxEvents; // allocated
} demoIndex_t;

// reads up to len bytes, returns how many were read
typedef int (*demoIndexRead_t)(void *ctx, void *buffer, int len);

void DemoIndex_Build(demoIndex_t *index, demoIndexRead_t read, void *ctx);
void DemoIndex_Free(demoIndex_t *index);
// the rules a level was played under, from its realism and default ticks:
// "realism", "default", "mixed" or "" when there's too little to tell
const char *DemoIndex_LevelRules(const demoEvent_t *level);
// returns the index as JSON, to free with free()
char *DemoIndex_ToJSON(const demoIndex_t *index, const char *demoName);
typedef struct {
    const char *demo; // empty when no demo plays
    int         time;
    int         duration;
    qboolean    paused;
    qboolean    seeking;
    const char *only;      // what demoonly plays: "", "kills", "watched", "multikills", "shownkills" or "shownmultikills"
    const char *player;    // the player demoonly is about
    const char *recording; // the video being recorded, if any
} demoState_t;

// returns what is playing as JSON, to free with free()
char *DemoIndex_StateJSON(const demoState_t *state);
