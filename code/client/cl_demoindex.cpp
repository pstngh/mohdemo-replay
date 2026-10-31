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

// a field of view with a scope on: the sniper rifles' is 20, against 80
// without
#define DI_SCOPE_FOV 60

// a player's bone controllers that follow their view and lean: head, torso,
// arms and pelvis (HEAD_TAG to PELVIS_TAG), the pelvis' roll being 0.8 of
// their lean
#define DI_BODY_BONES 4
#define DI_PELVIS     3

// what a player's entity shows of their input: their place, view, lean,
// animations and box, which crouching lowers
typedef struct {
    vec3_t origin;
    vec3_t angles;
    vec3_t bones[DI_BODY_BONES];
    int    anims[MAX_FRAMEINFOS];
    int    solid;
} diPose_t;

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
    int      level; // the map event of the level playing, -1 before the first
    int      teams[MAX_CLIENTS]; // the team each player was last seen on, TEAM_NONE if not seen
    int      unscopedTime; // the last snapshot of the player in view without a scope on
    diPose_t poses[MAX_CLIENTS]; // each player as last seen
    int      seenTime[MAX_CLIENTS]; // the snapshot each player was last seen in, 0 if not since the level started
    int      inputTime[MAX_CLIENTS]; // the last snapshot showing each player's input

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
    ev->scope  = -1;
    ev->speed  = -1;
    ev->idle   = -1;
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
DI_IsTeamKill

The obituary of a team kill reads like any other, so the teams of the
killer and the victim are compared, as last seen before it, in team games.
Also when everyone is on one team, as in practice games on objective maps:
the game takes those kills off too.
===============
*/
static qboolean DI_IsTeamKill(diParser_t *p, int killer, int victim)
{
    if (killer < 0 || killer >= MAX_CLIENTS || victim < 0 || victim >= MAX_CLIENTS || killer == victim) {
        return qfalse;
    }
    if (atoi(Info_ValueForKey(DI_ConfigString(p, CS_SERVERINFO), "g_gametype")) < GT_TEAM) {
        return qfalse;
    }

    return p->teams[killer] != TEAM_NONE && p->teams[killer] == p->teams[victim];
}

/*
What death messages tell of the weapon: their words after the victim's name,
and, if after is set, what comes right after the killer's name ("": nothing
but a space). As Player::Obituary writes them, then as servers of the
French, German, Spanish and Portuguese games wrote them in the demos seen,
each checked against the gun the killer held then. The French write sniper
and rifle kills alike, as sniper kills. Text is Latin-1, its escapes split
where a letter after them would be read as a digit.
*/
typedef struct {
    const char  *words;
    const char  *after;
    demoWeapon_t weapon;
} diObituary_t;

static const diObituary_t diObituaries[] = {
    {"was sniped by",                                 NULL,                          DEMOWEAPON_SNIPER },
    {"was rifled by",                                 NULL,                          DEMOWEAPON_RIFLE  },
    {"was perforated by",                             NULL,                          DEMOWEAPON_SMG    },
    {"was machine-gunned by",                         NULL,                          DEMOWEAPON_MG     },
    {"was gunned down by",                            NULL,                          DEMOWEAPON_PISTOL },
    {"was pumped full of buckshot by",                NULL,                          DEMOWEAPON_SHOTGUN},
    {"was hunted down by",                            NULL,                          DEMOWEAPON_SHOTGUN},
    {"was bashed by",                                 NULL,                          DEMOWEAPON_MELEE  },
    {"was clubbed by",                                NULL,                          DEMOWEAPON_MELEE  },
    {"tripped on",                                    NULL,                          DEMOWEAPON_GRENADE},
    {"is picking",                                    NULL,                          DEMOWEAPON_GRENADE},
    {"played catch with himself",                     NULL,                          DEMOWEAPON_GRENADE},
    {"caught some shrapnel",                          NULL,                          DEMOWEAPON_GRENADE},
    {"took himself out of commision",                 NULL,                          DEMOWEAPON_OTHER  },
    {"took",                                          NULL,                          DEMOWEAPON_ROCKET },
    {"rocketed himself",                              NULL,                          DEMOWEAPON_ROCKET },
    {"caught a rocket",                               NULL,                          DEMOWEAPON_ROCKET },
    {"was shot",                                      NULL,                          DEMOWEAPON_OTHER  },
    {"was blown away by",                             NULL,                          DEMOWEAPON_OTHER  },
    {"was killed by",                                 NULL,                          DEMOWEAPON_OTHER  },
    {"was crushed by",                                NULL,                          DEMOWEAPON_OTHER  },
    {"was telefragged by",                            NULL,                          DEMOWEAPON_OTHER  },
    {"was burned up by",                              NULL,                          DEMOWEAPON_OTHER  },
    {"was melted by",                                 NULL,                          DEMOWEAPON_OTHER  },
    {"was pushed over the edge by",                   NULL,                          DEMOWEAPON_OTHER  },
    {"was knocked out by",                            NULL,                          DEMOWEAPON_OTHER  },
    {"was run over by",                               NULL,                          DEMOWEAPON_OTHER  },
    {"was impaled by",                                NULL,                          DEMOWEAPON_OTHER  },
    {"stepped on",                                    NULL,                          DEMOWEAPON_OTHER  },
    {"was burned to a crisp",                         NULL,                          DEMOWEAPON_OTHER  },
    {"was melted to nothing",                         NULL,                          DEMOWEAPON_OTHER  },
    {"cratered",                                      NULL,                          DEMOWEAPON_OTHER  },
    {"blew himself up",                               NULL,                          DEMOWEAPON_OTHER  },
    {"blew up",                                       NULL,                          DEMOWEAPON_OTHER  },
    {"shot himself",                                  NULL,                          DEMOWEAPON_OTHER  },
    {"was hoist on his own pitard",                   NULL,                          DEMOWEAPON_OTHER  },
    {"died",                                          NULL,                          DEMOWEAPON_OTHER  },
    // French
    {"a \xe9t\xe9 tu\xe9 par",                        " avec un \xe9" "clat de grenade", DEMOWEAPON_GRENADE},
    {"a \xe9t\xe9 tu\xe9 par",                        NULL,                          DEMOWEAPON_SNIPER },
    {"s\x92" "est fait tuer par",                     " avec un pistolet mitrailleur", DEMOWEAPON_SMG  },
    {"a \xe9t\xe9 fusill\xe9 par",                    NULL,                          DEMOWEAPON_MG     },
    // without a hit location, a pistol's butt
    {"s\x92" "est fait descendre par",                "",                            DEMOWEAPON_MELEE  },
    {"s\x92" "est fait descendre par",                NULL,                          DEMOWEAPON_PISTOL },
    {"a \xe9t\xe9 \xe9limin\xe9 par",                 NULL,                          DEMOWEAPON_MELEE  },
    {"s\x92" "est fait abattre par",                  NULL,                          DEMOWEAPON_SHOTGUN},
    {"s\x92" "est fait liquider par",                 NULL,                          DEMOWEAPON_SHOTGUN},
    {"a tr\xe9" "buch\xe9 sur",                       NULL,                          DEMOWEAPON_GRENADE},
    {"s'est suicid\xe9.",                             NULL,                          DEMOWEAPON_OTHER  },
    {"s'est explos\xe9 tout seul.",                   NULL,                          DEMOWEAPON_OTHER  },
    {"est mort",                                      NULL,                          DEMOWEAPON_OTHER  },
    // German
    {"wurde getroffen durch",                         NULL,                          DEMOWEAPON_SNIPER },
    {"wurde niedergeschossen durch",                  NULL,                          DEMOWEAPON_RIFLE  },
    {"wurde perforiert durch",                        NULL,                          DEMOWEAPON_SMG    },
    {"wurde zersiebt durch",                          NULL,                          DEMOWEAPON_MG     },
    {"wurde erschossen durch",                        NULL,                          DEMOWEAPON_PISTOL },
    {"wurde mit Schrot vollgepumpt durch",            NULL,                          DEMOWEAPON_SHOTGUN},
    {"wurde zur Strecke gebracht durch",              NULL,                          DEMOWEAPON_SHOTGUN},
    {"wurde geschlagen durch",                        NULL,                          DEMOWEAPON_MELEE  },
    {"wurde niedergekn\xfcppelt durch",               NULL,                          DEMOWEAPON_MELEE  },
    {"trat auf",                                      NULL,                          DEMOWEAPON_GRENADE},
    {"w\xe4hlt",                                      NULL,                          DEMOWEAPON_GRENADE},
    {"ist auf die eigene Granate getreten",           NULL,                          DEMOWEAPON_GRENADE},
    {"spielt mit sich selber Fangen",                 NULL,                          DEMOWEAPON_GRENADE},
    {"nahm",                                          NULL,                          DEMOWEAPON_ROCKET },
    {"wurde gesprengt durch",                         NULL,                          DEMOWEAPON_OTHER  },
    {"ist in die Luft geflogen",                      NULL,                          DEMOWEAPON_OTHER  },
    {"hat sich in die Luft gesprengt",                NULL,                          DEMOWEAPON_OTHER  },
    {"hat sich selbst aus dem Verkehr gezogen",       NULL,                          DEMOWEAPON_OTHER  },
    {"eingestampft",                                  NULL,                          DEMOWEAPON_OTHER  },
    // Spanish and Portuguese
    {"fue snipeado por",                              NULL,                          DEMOWEAPON_SNIPER },
    {"fue abatido con un rifle de francotirador por", NULL,                          DEMOWEAPON_SNIPER },
    {"se ha suicidado",                               NULL,                          DEMOWEAPON_OTHER  },
    {"levou uma snipada do",                          NULL,                          DEMOWEAPON_SNIPER },
};

/*
===============
DI_WordsAt

Whether words come at s, after spaces, as whole words
===============
*/
static qboolean DI_WordsAt(const char *s, const char *words)
{
    size_t len = strlen(words);

    while (*s == ' ') {
        s++;
    }
    return !strncmp(s, words, len) && (s[len] == ' ' || !s[len]);
}

/*
===============
DI_FindWords

Where words are in text as whole words, NULL if they aren't
===============
*/
static const char *DI_FindWords(const char *text, const char *words)
{
    const char *s;

    for (s = strstr(text, words); s; s = strstr(s + 1, words)) {
        if ((s == text || s[-1] == ' ') && DI_WordsAt(s, words)) {
            return s;
        }
    }
    return NULL;
}

/*
===============
DI_Weapon

The weapon of a kill from its message, the words after the victim's name
(afterVictim) and, if the killer was found, after theirs (afterKiller); the
whole text when the victim wasn't found
===============
*/
static demoWeapon_t DI_Weapon(const char *text, const char *afterVictim, const char *afterKiller)
{
    const diObituary_t *o;
    const char         *at;
    int                 i;

    for (i = 0; i < (int)ARRAY_LEN(diObituaries); i++) {
        o  = &diObituaries[i];
        at = afterVictim ? (DI_WordsAt(afterVictim, o->words) ? afterVictim : NULL) : DI_FindWords(text, o->words);
        if (!at) {
            continue;
        }
        if (o->after && !o->after[0]) {
            // nothing after the killer
            if (!afterKiller || !DI_WordsAt(afterKiller, "")) {
                continue;
            }
        } else if (o->after) {
            if (afterKiller ? strncmp(afterKiller, o->after, strlen(o->after)) : !strstr(at, o->after)) {
                continue;
            }
        }
        return o->weapon;
    }

    // a message not known, but telling
    if (Q_stristr(text, "grenade") || Q_stristr(text, "shrapnel") || Q_stristr(text, "granate")
        || Q_stristr(text, "granada")) {
        return DEMOWEAPON_GRENADE;
    }
    return DEMOWEAPON_UNKNOWN;
}

/*
===============
DemoIndex_WeaponName
===============
*/
static const char *diWeaponNames[DEMOWEAPON_COUNT] = {
    "", "pistol", "rifle", "sniper", "smg", "mg", "shotgun", "rocket", "grenade", "melee", "other"};

const char *DemoIndex_WeaponName(demoWeapon_t weapon)
{
    return weapon >= 0 && weapon < DEMOWEAPON_COUNT ? diWeaponNames[weapon] : "";
}

/*
===============
DemoIndex_WeaponNamed
===============
*/
demoWeapon_t DemoIndex_WeaponNamed(const char *name)
{
    int i;

    for (i = DEMOWEAPON_UNKNOWN + 1; i < DEMOWEAPON_COUNT; i++) {
        if (!Q_stricmp(name, diWeaponNames[i])) {
            return (demoWeapon_t)i;
        }
    }
    return DEMOWEAPON_COUNT;
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
    const char  *afterVictim, *afterKiller;
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
        ev          = DI_AddEvent(p, DEMOEVENT_KILL);
        ev->other   = DI_FindPlayer(p, text, qtrue, ev->otherName, &end);
        afterVictim = afterKiller = NULL;
        if (ev->otherName[0]) {
            afterVictim = end;
            ev->client  = DI_FindPlayer(p, end, qfalse, ev->name, &end);
            if (ev->name[0]) {
                afterKiller = end;
            }
        }
        ev->teamKill = DI_IsTeamKill(p, ev->client, ev->other);
        ev->weapon   = DI_Weapon(text, afterVictim, afterKiller);
        ev->grenade  = ev->weapon == DEMOWEAPON_GRENADE;
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
            if (!joined[0]) {
                // left: whoever comes next in that slot isn't seen yet
                p->teams[i - CS_PLAYERS] = TEAM_NONE;
            }
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
    memset(p->teams, 0, sizeof(p->teams));
    memset(p->seenTime, 0, sizeof(p->seenTime));
    // a level starts with who is shown, as kills are only looked up in theirs
    p->watched = -2;
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
    p->level = p->index->numEvents - 1;

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
DI_InView

The player ps shows: the followed player while spectating, whose view and
movement ps then has, nobody in free view, else the player who recorded the
demo
===============
*/
static int DI_InView(const diParser_t *p, const playerState_t *ps)
{
    int client;

    if (ps->pm_flags & PMF_CAMERA_VIEW) {
        client = ps->stats[STAT_INFOCLIENT];
        return client >= 0 && client < MAX_CLIENTS ? client : -1;
    }
    if (ps->pm_flags & PMF_SPECTATING) {
        return -1;
    }
    return p->clientNum;
}

/*
===============
DI_UpdateWatched
===============
*/
static void DI_UpdateWatched(diParser_t *p)
{
    demoEvent_t *ev;
    int          watched;

    watched = DI_InView(p, &p->snap.ps);
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
DI_UpdateTeams

The recorder's team from their stats, which stay theirs while they follow
someone, and the other players' from the flags of their entities
===============
*/
static void DI_UpdateTeams(diParser_t *p)
{
    const entityState_t *ent;
    int                  team = p->snap.ps.stats[STAT_TEAM];
    int                  i;

    if (p->clientNum >= 0 && p->clientNum < MAX_CLIENTS && (team == TEAM_ALLIES || team == TEAM_AXIS)) {
        p->teams[p->clientNum] = team;
    }

    for (i = 0; i < p->snap.numEntities; i++) {
        ent = &p->parseEntities[(p->snap.parseEntitiesNum + i) & (DI_PARSE_ENTITIES - 1)];
        if (ent->number < 0 || ent->number >= MAX_CLIENTS) {
            continue;
        }
        if (ent->eFlags & EF_ALLIES) {
            p->teams[ent->number] = TEAM_ALLIES;
        } else if (ent->eFlags & EF_AXIS) {
            p->teams[ent->number] = TEAM_AXIS;
        }
    }
}

/*
===============
DI_UpdateIdle

When each player in view last gave input, as far as their entity shows it,
up to the last snapshot, lastTime being the one before: moving, turning,
firing and all else that changes their animations change their pose, and
crouching or leaning holds a key, so idle players stand upright, their pose
not changing. A player out of view in the snapshot before counts as giving
input when seen again.
===============
*/
static void DI_UpdateIdle(diParser_t *p, int lastTime)
{
    const entityState_t *ent;
    diPose_t             pose;
    vec3_t               mins, maxs;
    int                  i, j;

    for (i = 0; i < p->snap.numEntities; i++) {
        ent = &p->parseEntities[(p->snap.parseEntitiesNum + i) & (DI_PARSE_ENTITIES - 1)];
        if (ent->number < 0 || ent->number >= MAX_CLIENTS) {
            continue;
        }

        memset(&pose, 0, sizeof(pose));
        VectorCopy(ent->netorigin, pose.origin);
        VectorCopy(ent->netangles, pose.angles);
        for (j = 0; j < DI_BODY_BONES; j++) {
            VectorCopy(ent->bone_angles[j], pose.bones[j]);
        }
        for (j = 0; j < MAX_FRAMEINFOS; j++) {
            pose.anims[j] = ent->frameInfo[j].index;
        }
        pose.solid = ent->solid;
        IntegerToBoundingBox(ent->solid, mins, maxs);

        if (p->seenTime[ent->number] != lastTime || memcmp(&pose, &p->poses[ent->number], sizeof(pose))
            || maxs[2] <= CROUCH_RUN_MAXS_Z || ent->bone_angles[DI_PELVIS][2]) {
            p->inputTime[ent->number] = p->snap.serverTime;
        }
        p->poses[ent->number]    = pose;
        p->seenTime[ent->number] = p->snap.serverTime;
    }
}

/*
===============
DI_CountRulesSpeed

Realism servers change the top speeds: the StG 44 and BAR run at 233 and
every other weapon at 275, against 244 to 271 on default servers (at the
1.1 speed multiplier of the servers seen). Counted while the recorder plays,
on the ground, as the top speed reads 220 in the air. The speeds that tell
(at 0.6 when walking or crouched, and crouched with the StG 44 or BAR on
realism) come from 4,453 levels of demos labelled by their speeds and the
damage of hits: none is ever given by the other kind of server, while 275,
165 and 99 (unarmed, pistols and grenades) are given by both.
===============
*/
static void DI_CountRulesSpeed(diParser_t *p, const playerState_t *ps)
{
    demoEvent_t *level;

    if (p->level < 0 || ps->pm_type != PM_NORMAL || (ps->pm_flags & PMF_SPECTATING)
        || ps->groundEntityNum == ENTITYNUM_NONE) {
        return;
    }

    level = &p->index->events[p->level];
    switch (ps->speed) {
    case 233:
    case 139:
    case 83:
        level->realismTicks++;
        break;
    case 271:
    case 262:
    case 257:
    case 244:
    case 240:
    case 162:
    case 157:
    case 154:
    case 146:
    case 144:
    case 132:
    case 97:
    case 94:
    case 88:
    case 86:
    case 79:
        level->defaultTicks++;
        break;
    }
}

/*
===============
DemoIndex_LevelRules

At least 200 snapshots (10 s at 20 a second) that tell, 80% of them one
kind. On the same 4,453 levels, no level labelled default came out realism
and none labelled realism came out default.
===============
*/
const char *DemoIndex_LevelRules(const demoEvent_t *level)
{
    int total = level->realismTicks + level->defaultTicks;

    if (total < 200) {
        return "";
    }
    if (level->realismTicks * 5 >= total * 4) {
        return "realism";
    }
    if (level->defaultTicks * 5 >= total * 4) {
        return "default";
    }
    return "mixed";
}

/*
===============
DI_SetKillAims

The kills waiting for snap, made by the player in view: how long they had a
scope on before the shot, as the snapshot before shows, and their speed.
The death message comes with the snapshot of the shot.
===============
*/
static void DI_SetKillAims(diParser_t *p, const diSnapshot_t *snap)
{
    const float *velocity = snap->ps.velocity;
    demoEvent_t *ev;
    char         name[MAX_NAME_LENGTH];
    int          client;
    int          i;

    if (!snap->valid || !p->snap.valid) {
        return;
    }
    client = DI_InView(p, &snap->ps);
    if (client < 0 || DI_InView(p, &p->snap.ps) != client) {
        return;
    }
    DI_PlayerName(p, client, name, sizeof(name));

    for (i = p->firstUntimed; i < p->index->numEvents; i++) {
        ev = &p->index->events[i];
        // kill messages only name the killer: a kill by the name of the
        // player in view is theirs, also when someone else has that name,
        // like a spectator recording the demo as the player they follow
        if (ev->type != DEMOEVENT_KILL || (ev->client != client && (!name[0] || strcmp(ev->name, name)))) {
            continue;
        }
        ev->scope = p->snap.ps.fov < DI_SCOPE_FOV ? snap->serverTime - p->unscopedTime : 0;
        ev->speed = (int)(sqrt(velocity[0] * velocity[0] + velocity[1] * velocity[1]) + 0.5);
    }
}

/*
===============
DI_SetKillIdles

The kills waiting for the next snapshot: how long their victim had given no
input, if in view in the last snapshot, the one before the shot's.
===============
*/
static void DI_SetKillIdles(diParser_t *p)
{
    demoEvent_t *ev;
    int          i;

    if (!p->snap.valid) {
        return;
    }

    for (i = p->firstUntimed; i < p->index->numEvents; i++) {
        ev = &p->index->events[i];
        if (ev->type == DEMOEVENT_KILL && ev->other >= 0 && ev->other < MAX_CLIENTS
            && p->seenTime[ev->other] == p->snap.serverTime) {
            ev->idle = p->snap.serverTime - p->inputTime[ev->other];
        }
    }
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
    int                   lastTime;
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
    DI_SetKillAims(p, &newSnap);
    if (newSnap.valid) {
        DI_SetKillIdles(p);
    }
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

    lastTime = p->snap.valid ? p->snap.serverTime : -1;
    if (!p->snap.valid || DI_InView(p, &p->snap.ps) != DI_InView(p, &newSnap.ps) || newSnap.ps.fov >= DI_SCOPE_FOV) {
        p->unscopedTime = newSnap.serverTime;
    }
    p->snap                                       = newSnap;
    p->snapshots[newSnap.messageNum & PACKET_MASK] = newSnap;
    DI_CountRulesSpeed(p, &newSnap.ps);
    DI_UpdateTeams(p);
    DI_UpdateIdle(p, lastTime);

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
    p->level   = -1;

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
            DI_Printf(b, ", \"rules\": \"%s\", \"realismTicks\": %d, \"defaultTicks\": %d",
                DemoIndex_LevelRules(ev), ev->realismTicks, ev->defaultTicks);
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
            DI_Printf(b, ", \"teamKill\": %s", ev->teamKill ? "true" : "false");
            DI_Printf(b, ", \"grenade\": %s", ev->grenade ? "true" : "false");
            DI_Printf(b, ", \"weapon\": \"%s\"", DemoIndex_WeaponName(ev->weapon));
            if (ev->scope >= 0) {
                DI_Printf(b, ", \"scope\": %d, \"speed\": %d", ev->scope, ev->speed);
            }
            if (ev->idle >= 0) {
                DI_Printf(b, ", \"idle\": %d", ev->idle);
            }
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
char *DemoIndex_StateJSON(const demoState_t *state)
{
    diBuffer_t b;

    memset(&b, 0, sizeof(b));

    DI_Append(&b, "{\"demo\": ", 9);
    DI_String(&b, state->demo);
    DI_Printf(
        &b,
        ", \"time\": %d, \"duration\": %d, \"paused\": %s, \"seeking\": %s, \"only\": ",
        state->time,
        state->duration,
        state->paused ? "true" : "false",
        state->seeking ? "true" : "false"
    );
    DI_String(&b, state->only);
    DI_Append(&b, ", \"player\": ", 12);
    DI_String(&b, state->only[0] ? state->player : "");
    DI_Append(&b, ", \"recording\": ", 15);
    DI_String(&b, state->recording);
    DI_Append(&b, "}\n", 2);
    return b.data;
}
