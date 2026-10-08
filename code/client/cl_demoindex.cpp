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
//  cl_demoindex.cpp: reads a demo in one pass, the way the client parses
//  it (cl_parse.cpp) but with its own state, and lists what happens in it

#include "cl_demoindex.h"
#include "../qcommon/bg_compat.h"
#include "../fgame/bg_public.h"

#include <stdlib.h>

// the same as the client's MAX_PARSE_ENTITIES
#define DI_PARSE_ENTITIES 8192

typedef struct {
    qboolean      valid;
    int           messageNum;
    int           serverTime;
    int           snapFlags;
    playerState_t ps;
    int           numEntities;
    int           parseEntitiesNum;
} diSnapshot_t;

typedef struct {
    char name[MAX_NAME_LENGTH];
    int  client;
} diName_t;

// players remembered after they leave
#define DI_KNOWN_NAMES 256

typedef struct {
    demoIndex_t *index;

    gameState_t   gameState;
    int           clientNum;
    float         serverFrameTime;
    int           serverCommandSequence;
    char          bigConfigString[BIG_INFO_STRING];
    entityState_t baselines[MAX_GENTITIES];

    int           messageNum;
    diSnapshot_t  snap; // the last valid one
    diSnapshot_t  snapshots[PACKET_BACKUP];
    entityState_t parseEntities[DI_PARSE_ENTITIES];
    int           parseEntitiesNum;

    qboolean started; // got the first snapshot of active play
    int      startTime;
    int      lastTime;
    int      firstUntimed; // events waiting for the time of the next snapshot
    int      watched;

    diName_t knownNames[DI_KNOWN_NAMES];
    int      numKnownNames;

    byte msgData[MAX_MSGLEN];
} diParser_t;

/*
===============
DI_AddEvent
===============
*/
static demoEvent_t *DI_AddEvent(diParser_t *p, demoEventType_t type)
{
    demoIndex_t *index = p->index;
    demoEvent_t *ev;

    if (index->numEvents == index->maxEvents) {
        index->maxEvents = index->maxEvents ? index->maxEvents * 2 : 256;
        index->events    = (demoEvent_t *)realloc(index->events, index->maxEvents * sizeof(demoEvent_t));
    }

    ev = &index->events[index->numEvents++];
    memset(ev, 0, sizeof(*ev));
    ev->type   = type;
    ev->time   = -1;
    ev->client = -1;
    ev->other  = -1;
    return ev;
}

/*
===============
DI_SetEventTimes

Server commands come before the snapshot in a message, and cgame runs them
when that snapshot is shown
===============
*/
static void DI_SetEventTimes(diParser_t *p, int serverTime)
{
    demoIndex_t *index = p->index;
    int          time;

    time = p->started ? serverTime - p->startTime : 0;
    if (time < 0) {
        time = 0;
    }

    for (; p->firstUntimed < index->numEvents; p->firstUntimed++) {
        index->events[p->firstUntimed].time = time;
    }
}

/*
===============
DI_ConfigString
===============
*/
static const char *DI_ConfigString(diParser_t *p, int index)
{
    return p->gameState.stringData + p->gameState.stringOffsets[index];
}

/*
===============
DI_PlayerName
===============
*/
static void DI_PlayerName(diParser_t *p, int client, char *name, int size)
{
    if (client < 0 || client >= MAX_CLIENTS) {
        name[0] = 0;
        return;
    }

    Q_strncpyz(name, Info_ValueForKey(DI_ConfigString(p, CS_PLAYERS + client), "name"), size);
}

/*
===============
DI_SetConfigString

Like CL_ConfigstringModified
===============
*/
static qboolean DI_SetConfigString(diParser_t *p, int index, const char *s)
{
    gameState_t *oldGs;
    const char  *dup;
    size_t       len;
    int          i;

    if (index < 0 || index >= MAX_CONFIGSTRINGS) {
        return qfalse;
    }

    if (!strcmp(DI_ConfigString(p, index), s)) {
        return qtrue;
    }

    oldGs = (gameState_t *)malloc(sizeof(gameState_t));
    *oldGs = p->gameState;

    memset(&p->gameState, 0, sizeof(p->gameState));
    p->gameState.dataCount = 1;

    for (i = 0; i < MAX_CONFIGSTRINGS; i++) {
        dup = i == index ? s : oldGs->stringData + oldGs->stringOffsets[i];
        if (!dup[0]) {
            continue;
        }

        len = strlen(dup);
        if (len + 1 + p->gameState.dataCount > MAX_GAMESTATE_CHARS) {
            free(oldGs);
            return qfalse;
        }

        p->gameState.stringOffsets[i] = p->gameState.dataCount;
        memcpy(p->gameState.stringData + p->gameState.dataCount, dup, len + 1);
        p->gameState.dataCount += len + 1;
    }

    free(oldGs);
    return qtrue;
}

/*
===============
DI_Tokenize

Like Cmd_TokenizeString, into argv pointing in buffer
===============
*/
static int DI_Tokenize(const char *text, char *buffer, char **argv, int maxArgs)
{
    int argc = 0;

    while (argc < maxArgs) {
        while (*text && (unsigned char)*text <= ' ') {
            text++;
        }
        if (!*text || (text[0] == '/' && (text[1] == '/' || text[1] == '*'))) {
            break;
        }

        argv[argc++] = buffer;
        if (*text == '"') {
            text++;
            while (*text && *text != '"') {
                *buffer++ = *text++;
            }
            if (*text) {
                text++;
            }
        } else {
            while ((unsigned char)*text > ' ' && *text != '"'
                   && !(text[0] == '/' && (text[1] == '/' || text[1] == '*'))) {
                *buffer++ = *text++;
            }
        }
        *buffer++ = 0;
    }

    return argc;
}

/*
===============
DI_RememberName

Kill messages can come after the killer left, so the names of the players
are kept, the newest last
===============
*/
static void DI_RememberName(diParser_t *p, const char *name, int client)
{
    diName_t *known;

    if (!name[0]) {
        return;
    }

    known = &p->knownNames[p->numKnownNames++ % DI_KNOWN_NAMES];
    Q_strncpyz(known->name, name, sizeof(known->name));
    known->client = client;
}

/*
===============
DI_RememberPlayer
===============
*/
static void DI_RememberPlayer(diParser_t *p, int client)
{
    char name[MAX_NAME_LENGTH];

    DI_PlayerName(p, client, name, sizeof(name));
    DI_RememberName(p, name, client);
}

/*
===============
DI_IsNameAt

A whole name at s: at the start of the text or after a space, and followed
by a space, "'s" or the end of the line
===============
*/
static qboolean DI_IsNameAt(const char *text, const char *s, size_t len, qboolean atStart)
{
    char next;

    if (atStart ? s != text : s == text || s[-1] != ' ') {
        return qfalse;
    }

    next = s[len];
    return next == ' ' || next == '\n' || !next || (next == '\'' && s[len + 1] == 's');
}

/*
===============
DI_FindName
===============
*/
static const char *DI_FindName(const char *text, const char *name, qboolean atStart)
{
    const char *s;
    size_t      len = strlen(name);

    if (!len) {
        return NULL;
    }

    if (atStart) {
        return !strncmp(text, name, len) && DI_IsNameAt(text, text, len, qtrue) ? text : NULL;
    }

    for (s = strstr(text, name); s; s = strstr(s + 1, name)) {
        if (DI_IsNameAt(text, s, len, qfalse)) {
            return s;
        }
    }

    return NULL;
}

/*
===============
DI_FindPlayer

The player with the longest name in text, at its start if atStart. Players
who left count if no one else matches. Returns the client number, -1 if not
found or not known, and the name, empty if not found.
===============
*/
static int DI_FindPlayer(diParser_t *p, const char *text, qboolean atStart, char *name, const char **end)
{
    char        current[MAX_NAME_LENGTH];
    const char *s;
    int         best = -1;
    size_t      bestLen = 0;
    int         i;

    name[0] = 0;

    for (i = 0; i < MAX_CLIENTS; i++) {
        DI_PlayerName(p, i, current, sizeof(current));
        if (strlen(current) > bestLen && (s = DI_FindName(text, current, atStart))) {
            best    = i;
            bestLen = strlen(current);
            Q_strncpyz(name, current, MAX_NAME_LENGTH);
            *end = s + bestLen;
        }
    }

    for (i = p->numKnownNames - 1; best < 0 && i >= 0 && i >= p->numKnownNames - DI_KNOWN_NAMES; i--) {
        diName_t *known = &p->knownNames[i % DI_KNOWN_NAMES];
        if ((s = DI_FindName(text, known->name, atStart))) {
            best = known->client;
            Q_strncpyz(name, known->name, MAX_NAME_LENGTH);
            *end = s + strlen(known->name);
        }
    }

    return best;
}

/*
===============
DI_LearnName

Demos start being written at the first full snapshot after the gamestate, so
the commands that came in between are missing, and with them the names of
the players who joined then. They're learned from the join messages instead,
without client numbers.
===============
*/
static void DI_LearnName(diParser_t *p, char *text)
{
    static const char *joins[] = {
        " has entered the battle", " has joined the Allies", " has joined the Axis", " is preparing for deployment"};
    char        name[MAX_NAME_LENGTH];
    const char *end;
    size_t      len, joinLen;
    int         i;

    len = strlen(text);
    for (i = 0; i < (int)ARRAY_LEN(joins); i++) {
        joinLen = strlen(joins[i]);
        if (len > joinLen && !strcmp(text + len - joinLen, joins[i])) {
            break;
        }
    }

    if (i == (int)ARRAY_LEN(joins)) {
        return;
    }

    text[len - joinLen] = 0;
    if (DI_FindPlayer(p, text, qtrue, name, &end) < 0 && !name[0]) {
        DI_RememberName(p, text, -1);
    }
}

/*
===============
DI_Print

Kills come to everyone as "\x04Victim was ... by Killer ...", or a few other
wordings, always starting with the victim, and nothing else comes that way.
The killer also gets "\x03You killed Victim", which isn't needed. Round ends
come as "\x03Axis win!".
===============
*/
static void DI_Print(diParser_t *p, const char *s)
{
    char         text[MAX_STRING_CHARS];
    demoEvent_t *ev;
    const char  *end;
    char        *c;

    if (!*s) {
        return;
    }

    Q_strncpyz(text, s + 1, sizeof(text));
    c = strchr(text, '\n');
    if (c) {
        *c = 0;
    }

    if (*s == HUD_MESSAGE_CHAT_RED[0]) {
        ev        = DI_AddEvent(p, DEMOEVENT_KILL);
        ev->other = DI_FindPlayer(p, text, qtrue, ev->otherName, &end);
        if (ev->otherName[0]) {
            ev->client = DI_FindPlayer(p, end, qfalse, ev->name, &end);
        }
        Q_strncpyz(ev->text, text, sizeof(ev->text));
        return;
    }

    if (*s != HUD_MESSAGE_WHITE[0]) {
        return;
    }

    if (!strcmp(text, "Axis win!") || !strcmp(text, "Allies win!") || !strcmp(text, "It's a draw!")) {
        ev = DI_AddEvent(p, DEMOEVENT_ROUNDEND);
        Q_strncpyz(ev->text, text, sizeof(ev->text));
        return;
    }

    DI_LearnName(p, text);
}

/*
===============
DI_ServerCommand

Like CL_GetServerCommand, for the commands the index needs
===============
*/
static void DI_ServerCommand(diParser_t *p, const char *s)
{
    char  buffer[BIG_INFO_STRING];
    char  joined[BIG_INFO_STRING];
    char *argv[MAX_STRING_TOKENS];
    int   argc;
    int   i;

    if (strlen(s) >= sizeof(buffer)) {
        return;
    }

    argc = DI_Tokenize(s, buffer, argv, MAX_STRING_TOKENS);
    if (!argc) {
        return;
    }

    if (!strcmp(argv[0], "bcs0")) {
        Com_sprintf(p->bigConfigString, sizeof(p->bigConfigString), "cs %s \"%s", argc > 1 ? argv[1] : "", argc > 2 ? argv[2] : "");
        return;
    }

    if (!strcmp(argv[0], "bcs1") || !strcmp(argv[0], "bcs2")) {
        Q_strcat(p->bigConfigString, sizeof(p->bigConfigString), argc > 2 ? argv[2] : "");
        if (!strcmp(argv[0], "bcs2")) {
            Q_strcat(p->bigConfigString, sizeof(p->bigConfigString), "\"");
            Q_strncpyz(joined, p->bigConfigString, sizeof(joined));
            DI_ServerCommand(p, joined);
        }
        return;
    }

    if (!strcmp(argv[0], "cs") && argc > 1) {
        // the rest of the arguments, like Cmd_ArgsFrom
        joined[0] = 0;
        for (i = 2; i < argc; i++) {
            Q_strcat(joined, sizeof(joined), argv[i]);
            if (i < argc - 1) {
                Q_strcat(joined, sizeof(joined), " ");
            }
        }
        i = CPT_NormalizeConfigstring(atoi(argv[1]));
        if (DI_SetConfigString(p, i, joined) && i >= CS_PLAYERS && i < CS_PLAYERS + MAX_CLIENTS) {
            DI_RememberPlayer(p, i - CS_PLAYERS);
        }
        return;
    }

    if (!strcmp(argv[0], "print") && argc > 1) {
        DI_Print(p, argv[1]);
        return;
    }
}

/*
===============
DI_ParseGamestate

Like CL_ParseGamestate, but the frames to delta from are kept, as in
CL_ClearStateKeepingFrames
===============
*/
static qboolean DI_ParseGamestate(diParser_t *p, msg_t *msg)
{
    entityState_t nullstate;
    demoEvent_t  *ev;
    const char   *s;
    size_t        len;
    int           cmd, i, csNum, newnum;

    memset(&p->gameState, 0, sizeof(p->gameState));
    memset(p->baselines, 0, sizeof(p->baselines));
    p->snap.valid = qfalse;

    p->serverCommandSequence = MSG_ReadLong(msg);
    p->gameState.dataCount   = 1;

    while (1) {
        cmd = MSG_ReadByte(msg);
        if (msg->readcount > (int)msg->cursize) {
            return qfalse;
        }

        if (cmd == svc_EOF) {
            break;
        }

        if (cmd == svc_configstring) {
            i     = MSG_ReadShort(msg);
            csNum = CPT_NormalizeConfigstring(i);
            if (csNum < 0 || csNum >= MAX_CONFIGSTRINGS) {
                return qfalse;
            }

            s   = MSG_ReadScrambledBigString(msg);
            len = strlen(s);
            if (len + 1 + p->gameState.dataCount > MAX_GAMESTATE_CHARS) {
                return qfalse;
            }

            p->gameState.stringOffsets[csNum] = p->gameState.dataCount;
            memcpy(p->gameState.stringData + p->gameState.dataCount, s, len + 1);
            p->gameState.dataCount += len + 1;
        } else if (cmd == svc_baseline) {
            newnum = MSG_ReadEntityNum(msg);
            if (newnum < 0 || newnum >= MAX_GENTITIES) {
                return qfalse;
            }
            MSG_GetNullEntityState(&nullstate);
            MSG_ReadDeltaEntity(msg, &nullstate, &p->baselines[newnum], newnum, p->serverFrameTime);
        } else {
            return qfalse;
        }
    }

    p->clientNum = MSG_ReadLong(msg);
    MSG_ReadLong(msg); // checksum feed
    p->serverFrameTime = MSG_ReadServerFrameTime(msg, &p->gameState);

    for (i = 0; i < MAX_CLIENTS; i++) {
        DI_RememberPlayer(p, i);
    }

    ev = DI_AddEvent(p, DEMOEVENT_MAP);
    Q_strncpyz(ev->text, Info_ValueForKey(DI_ConfigString(p, CS_SERVERINFO), "mapname"), sizeof(ev->text));

    if (!p->index->recorderName[0]) {
        p->index->recorder = p->clientNum;
        DI_PlayerName(p, p->clientNum, p->index->recorderName, sizeof(p->index->recorderName));
    }

    return qtrue;
}

/*
===============
DI_DeltaEntity
===============
*/
static void DI_DeltaEntity(diParser_t *p, msg_t *msg, diSnapshot_t *frame, int newnum, entityState_t *old, qboolean unchanged)
{
    entityState_t *state;

    state = &p->parseEntities[p->parseEntitiesNum & (DI_PARSE_ENTITIES - 1)];

    if (unchanged) {
        *state = *old;
    } else {
        MSG_ReadDeltaEntity(msg, old, state, newnum, p->serverFrameTime);
    }

    if (state->number == (MAX_GENTITIES - 1)) {
        return; // entity was delta removed
    }
    p->parseEntitiesNum++;
    frame->numEntities++;
}

/*
===============
DI_ParsePacketEntities

Like CL_ParsePacketEntities
===============
*/
static qboolean DI_ParsePacketEntities(diParser_t *p, msg_t *msg, diSnapshot_t *oldframe, diSnapshot_t *newframe)
{
    entityState_t *oldstate = NULL;
    int            oldindex = 0;
    int            oldnum;
    int            newnum;

    newframe->parseEntitiesNum = p->parseEntitiesNum;
    newframe->numEntities      = 0;

    if (!oldframe || !oldframe->numEntities) {
        oldnum = 99999;
    } else {
        oldstate = &p->parseEntities[oldframe->parseEntitiesNum & (DI_PARSE_ENTITIES - 1)];
        oldnum   = oldstate->number;
    }

    while (1) {
        newnum = MSG_ReadEntityNum(msg);
        if (newnum == (MAX_GENTITIES - 1)) {
            break;
        }

        if (msg->readcount > (int)msg->cursize || newnum >= MAX_GENTITIES) {
            return qfalse;
        }

        while (oldnum < newnum) {
            // one or more entities from the old packet are unchanged
            DI_DeltaEntity(p, msg, newframe, oldnum, oldstate, qtrue);

            oldindex++;
            if (oldindex >= oldframe->numEntities) {
                oldnum = 99999;
            } else {
                oldstate = &p->parseEntities[(oldframe->parseEntitiesNum + oldindex) & (DI_PARSE_ENTITIES - 1)];
                oldnum   = oldstate->number;
            }
        }

        if (oldnum == newnum) {
            // delta from previous state
            DI_DeltaEntity(p, msg, newframe, newnum, oldstate, qfalse);

            oldindex++;
            if (oldindex >= oldframe->numEntities) {
                oldnum = 99999;
            } else {
                oldstate = &p->parseEntities[(oldframe->parseEntitiesNum + oldindex) & (DI_PARSE_ENTITIES - 1)];
                oldnum   = oldstate->number;
            }
            continue;
        }

        if (oldnum > newnum) {
            // delta from baseline
            DI_DeltaEntity(p, msg, newframe, newnum, &p->baselines[newnum], qfalse);
        }
    }

    // any remaining entities in the old frame are copied over
    while (oldnum != 99999) {
        DI_DeltaEntity(p, msg, newframe, oldnum, oldstate, qtrue);

        oldindex++;
        if (oldindex >= oldframe->numEntities) {
            oldnum = 99999;
        } else {
            oldstate = &p->parseEntities[(oldframe->parseEntitiesNum + oldindex) & (DI_PARSE_ENTITIES - 1)];
            oldnum   = oldstate->number;
        }
    }

    return qtrue;
}

/*
===============
DI_UpdateWatched

The followed player while spectating, nobody in free view, else the
player who recorded the demo
===============
*/
static void DI_UpdateWatched(diParser_t *p)
{
    demoEvent_t *ev;
    int          watched;

    if (p->snap.ps.pm_flags & PMF_CAMERA_VIEW) {
        watched = p->snap.ps.stats[STAT_INFOCLIENT];
        if (watched < 0 || watched >= MAX_CLIENTS) {
            watched = -1;
        }
    } else if (p->snap.ps.pm_flags & PMF_SPECTATING) {
        watched = -1;
    } else {
        watched = p->clientNum;
    }

    if (watched == p->watched) {
        return;
    }
    p->watched = watched;

    ev         = DI_AddEvent(p, DEMOEVENT_WATCH);
    ev->client = watched;
    DI_PlayerName(p, watched, ev->name, sizeof(ev->name));
}

/*
===============
DI_ParseSnapshot

Like CL_ParseSnapshot
===============
*/
static qboolean DI_ParseSnapshot(diParser_t *p, msg_t *msg)
{
    static server_sound_t sounds[MAX_SERVER_SOUNDS];
    diSnapshot_t         *old;
    diSnapshot_t          newSnap;
    byte                  areamask[MAX_MAP_AREA_BYTES];
    int                   deltaNum;
    int                   numSounds;
    int                   len;
    int                   i;

    memset(&newSnap, 0, sizeof(newSnap));
    newSnap.serverTime = MSG_ReadLong(msg);
    MSG_ReadByte(msg); // time residual
    newSnap.messageNum = p->messageNum;

    deltaNum = MSG_ReadByte(msg);
    deltaNum = deltaNum ? newSnap.messageNum - deltaNum : -1;
    newSnap.snapFlags = MSG_ReadByte(msg);

    old = NULL;
    if (deltaNum <= 0) {
        newSnap.valid = qtrue; // uncompressed frame
    } else {
        old = &p->snapshots[deltaNum & PACKET_MASK];
        if (old->valid && old->messageNum == deltaNum
            && p->parseEntitiesNum - old->parseEntitiesNum <= DI_PARSE_ENTITIES - 128) {
            newSnap.valid = qtrue;
        }
    }

    len = MSG_ReadByte(msg);
    if (len > (int)sizeof(areamask)) {
        return qfalse;
    }
    MSG_ReadData(msg, areamask, len);

    MSG_ReadDeltaPlayerstate(msg, old ? &old->ps : NULL, &newSnap.ps, p->serverFrameTime);
    newSnap.ps.pm_flags = CPT_NormalizePlayerStateFlags(newSnap.ps.net_pm_flags);

    if (!DI_ParsePacketEntities(p, msg, old, &newSnap)) {
        return qfalse;
    }

    MSG_ReadSounds(msg, sounds, &numSounds);

    if (msg->readcount > (int)msg->cursize) {
        return qfalse;
    }

    if (!p->started && newSnap.valid && !(newSnap.snapFlags & SNAPFLAG_NOT_ACTIVE)) {
        // like the client, demo times count from the first active snapshot
        p->started   = qtrue;
        p->startTime = newSnap.serverTime;
    }
    p->lastTime = newSnap.serverTime;
    DI_SetEventTimes(p, newSnap.serverTime);

    if (!newSnap.valid) {
        return qtrue;
    }

    // clear the valid flags of the snapshots skipped since the last one
    i = p->snap.messageNum + 1;
    if (newSnap.messageNum - i >= PACKET_BACKUP) {
        i = newSnap.messageNum - (PACKET_BACKUP - 1);
    }
    for (; i < newSnap.messageNum; i++) {
        p->snapshots[i & PACKET_MASK].valid = qfalse;
    }

    if (p->snap.valid && ((newSnap.snapFlags ^ p->snap.snapFlags) & SNAPFLAG_SERVERCOUNT)) {
        // the level restarted, a new round in round-based modes
        DI_AddEvent(p, DEMOEVENT_ROUNDSTART);
    }

    p->snap                                       = newSnap;
    p->snapshots[newSnap.messageNum & PACKET_MASK] = newSnap;

    if (p->started) {
        DI_UpdateWatched(p);
        DI_SetEventTimes(p, newSnap.serverTime);
    }

    return qtrue;
}

/*
===============
DI_ParseMessage

Like CL_ParseServerMessage. The cgame messages come last, and only cgame
knows how to read them, so the message ends there.
===============
*/
static qboolean DI_ParseMessage(diParser_t *p, msg_t *msg)
{
    const char *s;
    int         cmd;
    int         seq;

    MSG_Bitstream(msg);
    MSG_ReadLong(msg); // reliable acknowledge

    while (1) {
        if (msg->readcount > (int)msg->cursize) {
            return qfalse;
        }

        cmd = MSG_ReadByte(msg);
        switch (cmd) {
        case svc_EOF:
        case svc_cgameMessage:
            return qtrue;
        case svc_nop:
            break;
        case svc_serverCommand:
            seq = MSG_ReadLong(msg);
            s   = MSG_ReadScrambledString(msg);
            if (seq > p->serverCommandSequence) {
                p->serverCommandSequence = seq;
                DI_ServerCommand(p, s);
            }
            break;
        case svc_gamestate:
            if (!DI_ParseGamestate(p, msg)) {
                return qfalse;
            }
            break;
        case svc_snapshot:
            if (!DI_ParseSnapshot(p, msg)) {
                return qfalse;
            }
            break;
        case svc_centerprint:
            MSG_ReadScrambledString(msg);
            break;
        case svc_locprint:
            MSG_ReadShort(msg);
            MSG_ReadShort(msg);
            MSG_ReadScrambledString(msg);
            break;
        default:
            return qfalse;
        }
    }
}

/*
===============
DemoIndex_Build
===============
*/
void DemoIndex_Build(demoIndex_t *index, demoIndexRead_t read, void *ctx)
{
    diParser_t *p;
    msg_t       msg;
    int         seq;
    int         len;

    memset(index, 0, sizeof(*index));
    index->recorder = -1;

    p = (diParser_t *)calloc(1, sizeof(diParser_t));
    p->index   = index;
    p->watched = -2;

    while (1) {
        // the same framing as CL_ReadDemoMessage
        if (read(ctx, &seq, 4) != 4 || read(ctx, &len, 4) != 4) {
            break;
        }

        p->messageNum = LittleLong(seq);
        len           = LittleLong(len);
        if (len == -1) {
            break;
        }

        if (len < 0 || len > MAX_MSGLEN || read(ctx, p->msgData, len) != len) {
            index->truncated = qtrue;
            break;
        }

        MSG_Init(&msg, p->msgData, sizeof(p->msgData));
        msg.cursize = len;

        if (!DI_ParseMessage(p, &msg)) {
            index->truncated = qtrue;
            break;
        }
    }

    // events after the last snapshot
    DI_SetEventTimes(p, p->lastTime);

    if (p->started) {
        index->duration = p->lastTime - p->startTime;
    }

    free(p);
}

/*
===============
DemoIndex_Free
===============
*/
void DemoIndex_Free(demoIndex_t *index)
{
    free(index->events);
    memset(index, 0, sizeof(*index));
    index->recorder = -1;
}

typedef struct {
    char  *data;
    size_t size;
    size_t len;
} diBuffer_t;

/*
===============
DI_Append
===============
*/
static void DI_Append(diBuffer_t *b, const char *s, size_t len)
{
    if (b->len + len + 1 > b->size) {
        b->size = (b->len + len + 1) * 2;
        b->data = (char *)realloc(b->data, b->size);
    }

    memcpy(b->data + b->len, s, len);
    b->len += len;
    b->data[b->len] = 0;
}

static void QDECL DI_Printf(diBuffer_t *b, const char *fmt, ...) Q_PRINTF_FUNC(2, 3);

/*
===============
DI_Printf
===============
*/
static void QDECL DI_Printf(diBuffer_t *b, const char *fmt, ...)
{
    char    text[1024];
    va_list argptr;
    int     len;

    va_start(argptr, fmt);
    len = Q_vsnprintf(text, sizeof(text), fmt, argptr);
    va_end(argptr);

    DI_Append(b, text, len);
}

/*
===============
DI_String

A JSON string. The game's text is Latin-1, JSON is UTF-8.
===============
*/
static void DI_String(diBuffer_t *b, const char *s)
{
    const unsigned char *c;
    char                 out[8];

    DI_Append(b, "\"", 1);
    for (c = (const unsigned char *)s; *c; c++) {
        if (*c == '"' || *c == '\\') {
            out[0] = '\\';
            out[1] = *c;
            DI_Append(b, out, 2);
        } else if (*c < 0x20) {
            Com_sprintf(out, sizeof(out), "\\u%04x", *c);
            DI_Append(b, out, 6);
        } else if (*c >= 0x80) {
            out[0] = 0xC0 | (*c >> 6);
            out[1] = 0x80 | (*c & 0x3F);
            DI_Append(b, out, 2);
        } else {
            DI_Append(b, (const char *)c, 1);
        }
    }
    DI_Append(b, "\"", 1);
}

/*
===============
DI_EventsToJSON
===============
*/
static void DI_EventsToJSON(diBuffer_t *b, const demoIndex_t *index, demoEventType_t type, const char *key)
{
    const demoEvent_t *ev;
    qboolean           first = qtrue;
    int                i;

    DI_Printf(b, ",\n\"%s\": [", key);

    for (i = 0; i < index->numEvents; i++) {
        ev = &index->events[i];
        if (ev->type != type) {
            continue;
        }

        DI_Printf(b, "%s\n {\"time\": %d", first ? "" : ",", ev->time);
        first = qfalse;

        switch (type) {
        case DEMOEVENT_MAP:
            DI_Append(b, ", \"map\": ", 9);
            DI_String(b, ev->text);
            break;
        case DEMOEVENT_WATCH:
            DI_Printf(b, ", \"client\": %d, \"name\": ", ev->client);
            DI_String(b, ev->name);
            break;
        case DEMOEVENT_KILL:
            DI_Printf(b, ", \"killer\": %d, \"killerName\": ", ev->client);
            DI_String(b, ev->name);
            DI_Printf(b, ", \"victim\": %d, \"victimName\": ", ev->other);
            DI_String(b, ev->otherName);
            DI_Append(b, ", \"text\": ", 10);
            DI_String(b, ev->text);
            break;
        case DEMOEVENT_ROUNDSTART:
            break;
        case DEMOEVENT_ROUNDEND:
            DI_Append(b, ", \"text\": ", 10);
            DI_String(b, ev->text);
            break;
        }

        DI_Append(b, "}", 1);
    }

    DI_Append(b, first ? "]" : "\n]", first ? 1 : 2);
}

/*
===============
DemoIndex_ToJSON

Times are in msec from the demo's first snapshot
===============
*/
char *DemoIndex_ToJSON(const demoIndex_t *index, const char *demoName)
{
    diBuffer_t b;

    memset(&b, 0, sizeof(b));

    DI_Append(&b, "{\n\"demo\": ", 10);
    DI_String(&b, demoName);
    DI_Printf(&b, ",\n\"duration\": %d,\n\"truncated\": %s", index->duration, index->truncated ? "true" : "false");
    DI_Printf(&b, ",\n\"recorder\": {\"client\": %d, \"name\": ", index->recorder);
    DI_String(&b, index->recorderName);
    DI_Append(&b, "}", 1);

    DI_EventsToJSON(&b, index, DEMOEVENT_MAP, "maps");
    DI_EventsToJSON(&b, index, DEMOEVENT_WATCH, "watched");
    DI_EventsToJSON(&b, index, DEMOEVENT_KILL, "kills");
    DI_EventsToJSON(&b, index, DEMOEVENT_ROUNDSTART, "rounds");
    DI_EventsToJSON(&b, index, DEMOEVENT_ROUNDEND, "roundEnds");

    DI_Append(&b, "\n}\n", 3);
    return b.data;
}

/*
===============
DemoIndex_StateJSON

What is playing, for the app's clock
===============
*/
char *DemoIndex_StateJSON(
    const char *demoName, int time, int duration, qboolean isPaused, qboolean isSeeking, const char *only, const char *player
)
{
    diBuffer_t b;

    memset(&b, 0, sizeof(b));

    DI_Append(&b, "{\"demo\": ", 9);
    DI_String(&b, demoName);
    DI_Printf(
        &b,
        ", \"time\": %d, \"duration\": %d, \"paused\": %s, \"seeking\": %s, \"only\": ",
        time,
        duration,
        isPaused ? "true" : "false",
        isSeeking ? "true" : "false"
    );
    DI_String(&b, only);
    DI_Append(&b, ", \"player\": ", 12);
    DI_String(&b, only[0] ? player : "");
    DI_Append(&b, "}\n", 2);
    return b.data;
}
