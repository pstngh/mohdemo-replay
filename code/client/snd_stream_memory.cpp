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

// snd_stream_memory.cpp -- the soundtrack's mp3 files kept in memory
//
// A song of the soundtrack is a 2D stream (omaha_channel_two_d_stream). Opening
// an mp3 file stream scans the whole file for its length (S_MP3_Scanfile), and
// a round restart starts the soundtrack again: 1 to 2.7 ms in that frame
// before the files were kept, with a 3 minute mp3.
//
// With s_soundtrackCache 1, each song of a soundtrack (MUSIC_LoadSoundtrackFile)
// is read into memory, scanned and its first SM_HEAD_SAMPLES decoded (the head)
// when the soundtrack loads, which a round restart doesn't repeat (the files
// stay until the next map load). A stream of it (S_CodecLoad(name, NULL))
// costs no file system call or scan, and what the main thread reads of it, a
// song's start offset and first chunk and a loop's restart, is copied from the
// head; the reads past the head decode, with the stream's own decoder
// (S_MP3_OpenMemoryStream with the scanned info, made by the first of them),
// on the thread that owns the stream (the sound system's decode-ahead worker,
// snd_stream_async.cpp). A decoder has its own memory and dithering sequence:
// no file system or zone call, and the same PCM every time the file is decoded,
// in reads of any size (the head is the same bytes). Opening, closing and
// flushing are main thread only.

#include "snd_local.h"
#include "snd_codec.h"

#ifdef USE_CODEC_MAD

// soundtrack files kept in memory beyond those in use
#define SM_CACHE_BYTES (64 << 20)
// samples decoded at the load: the start offset (0.063 s) and a chunk (2048) and more
#define SM_HEAD_SAMPLES 8192

struct smFile_t {
    char        name[MAX_QPATH];
    byte       *data; // the compressed file
    int         length;
    snd_info_t  info; // from the scan
    byte       *head; // the first SM_HEAD_SAMPLES decoded
    int         headLength;
    bool        headAll; // the head is the whole file
    int         refs; // open streams
    int         lastUse;
    bool        flushed; // freed when its last stream closes
    smFile_t   *next;
};

struct smReader_t {
    snd_stream_t  stream; // what the channel holds (stream.ptr: this)
    smFile_t     *file;
    snd_stream_t *dec; // made by the first read past the head
    int           pos; // bytes read
    bool          failed;
};

static smFile_t *sm_files;
static int       sm_useCount;

static int  SM_ReadStream(snd_stream_t *stream, int bytes, void *buffer);
static void SM_CloseStream(snd_stream_t *stream);

static snd_codec_t sm_codec = {(char *)"mp3", NULL, NULL, SM_ReadStream, SM_CloseStream, NULL};

/*
=================
SM_FreeFile
=================
*/
static void SM_FreeFile(smFile_t *file)
{
    smFile_t **prev;

    for (prev = &sm_files; *prev && *prev != file; prev = &(*prev)->next) {}
    if (*prev) {
        *prev = file->next;
    }
    free(file->data);
    free(file->head);
    delete file;
}

/*
=================
SM_FreeFiles

The files no stream uses, all of them or until the cache is below its size.
=================
*/
static void SM_FreeFiles(bool all)
{
    smFile_t *file, *oldest;
    int       total;

    for (;;) {
        total  = 0;
        oldest = NULL;
        for (file = sm_files; file; file = file->next) {
            total += file->length;
            if (!file->refs && (!oldest || file->lastUse < oldest->lastUse)) {
                oldest = file;
            }
        }
        if (!oldest || (!all && total <= SM_CACHE_BYTES)) {
            return;
        }
        SM_FreeFile(oldest);
    }
}

/*
=================
SM_FindFile
=================
*/
static smFile_t *SM_FindFile(const char *filename)
{
    smFile_t *file;

    for (file = sm_files; file; file = file->next) {
        if (!file->flushed && !Q_stricmp(file->name, filename)) {
            return file;
        }
    }
    return NULL;
}

/*
=================
S_StreamMemory_Prefetch

A song of the soundtrack that loads: read into memory and scanned.
=================
*/
void S_StreamMemory_Prefetch(const char *filename)
{
    smFile_t     *file;
    snd_stream_t *scan;
    void         *data;
    long          length;
    int           headSize;

    if (!s_soundtrackCache || !s_soundtrackCache->integer || strlen(filename) >= MAX_QPATH
        || Q_stricmp(COM_GetExtension(filename), "mp3")) {
        return;
    }

    file = SM_FindFile(filename);
    if (file) {
        file->lastUse = ++sm_useCount;
        return;
    }

    length = FS_ReadFile(filename, &data);
    if (!data) {
        // the codec's streams try the other extensions
        return;
    }

    file         = new smFile_t();
    file->data   = (byte *)malloc(length);
    file->length = length;
    if (file->data) {
        memcpy(file->data, data, length);
    }
    FS_FreeFile(data);

    scan = file->data ? S_MP3_OpenMemoryStream(file->data, file->length, NULL) : NULL;
    if (!scan) {
        free(file->data);
        delete file;
        return;
    }
    file->info = scan->info;
    S_MP3_CloseMemoryStream(scan);

    // the head, with a decoder of its own (as every stream's)
    headSize   = (int)(SM_HEAD_SAMPLES * file->info.width * file->info.channels);
    file->head = (byte *)malloc(headSize);
    scan       = file->head ? S_MP3_OpenMemoryStream(file->data, file->length, &file->info) : NULL;
    if (scan) {
        file->headLength = S_MP3_CodecReadStream(scan, headSize, file->head);
        S_MP3_CloseMemoryStream(scan);
    }
    if (file->headLength <= 0) {
        // no head: every read decodes
        file->headLength = 0;
    } else {
        file->headAll = file->headLength < headSize;
    }

    Q_strncpyz(file->name, filename, sizeof(file->name));
    file->lastUse = ++sm_useCount;
    file->next    = sm_files;
    sm_files      = file;
    SM_FreeFiles(false);
}

/*
=================
S_StreamMemory_Open

A stream of a soundtrack file in memory, NULL when it isn't one (the codec's
file streams).
=================
*/
snd_stream_t *S_StreamMemory_Open(const char *filename)
{
    smFile_t   *file;
    smReader_t *r;

    if (!sm_files) {
        return NULL;
    }

    file = SM_FindFile(filename);
    if (!file) {
        return NULL;
    }

    r                = new smReader_t();
    r->file          = file;
    r->stream.codec  = &sm_codec;
    r->stream.info   = file->info;
    r->stream.length = file->length;
    r->stream.ptr    = r;

    file->refs++;
    file->lastUse = ++sm_useCount;
    return &r->stream;
}

/*
=================
SM_ReadStream

Any thread that owns the stream: the head copied, then the first read past it
makes the decoder, which decodes the head again to get past it.
=================
*/
static int SM_ReadStream(snd_stream_t *stream, int bytes, void *buffer)
{
    smReader_t *r    = (smReader_t *)stream->ptr;
    smFile_t   *file = r->file;
    byte        scratch[8192];
    int         done = 0, got, skip;

    if (r->pos < file->headLength) {
        done = Q_min(bytes, file->headLength - r->pos);
        memcpy(buffer, file->head + r->pos, done);
        r->pos += done;
    }
    if (done == bytes || (file->headAll && r->pos >= file->headLength)) {
        return done;
    }

    if (!r->dec) {
        if (r->failed) {
            return done;
        }
        r->dec = S_MP3_OpenMemoryStream(file->data, file->length, &file->info);
        for (skip = r->pos; r->dec && skip > 0; skip -= got) {
            got = S_MP3_CodecReadStream(r->dec, Q_min(skip, (int)sizeof(scratch)), scratch);
            if (got <= 0) {
                break;
            }
        }
        if (!r->dec || skip > 0) {
            r->failed = true;
            return done;
        }
    }

    got = S_MP3_CodecReadStream(r->dec, bytes - done, (byte *)buffer + done);
    if (got > 0) {
        r->pos += got;
        done += got;
    }
    return done;
}

/*
=================
SM_CloseStream
=================
*/
static void SM_CloseStream(snd_stream_t *stream)
{
    smReader_t *r    = (smReader_t *)stream->ptr;
    smFile_t   *file = r->file;

    if (r->dec) {
        S_MP3_CloseMemoryStream(r->dec);
    }
    delete r;

    file->refs--;
    if (!file->refs && file->flushed) {
        SM_FreeFile(file);
    }
}

/*
=================
S_StreamMemory_Flush

A map loads: the files are forgotten (the next soundtrack reads its files
again); those a stream still reads go with their last stream.
=================
*/
void S_StreamMemory_Flush(void)
{
    smFile_t *file;

    SM_FreeFiles(true);
    for (file = sm_files; file; file = file->next) {
        file->flushed = true;
    }
}

#else // USE_CODEC_MAD

// without mp3 support the codec's file streams play everything

void S_StreamMemory_Prefetch(const char *filename) {}

snd_stream_t *S_StreamMemory_Open(const char *filename)
{
    return NULL;
}

void S_StreamMemory_Flush(void) {}

#endif // USE_CODEC_MAD
