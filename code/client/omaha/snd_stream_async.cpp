/*
===========================================================================
Copyright (C) 2026 Project: Omaha

This file is part of Project: Omaha source code.

Project: Omaha builds upon OpenMoHAA / ioquake3 / F.A.K.K. foundations.
Project: Omaha source code is free software; you can redistribute it
and/or modify it under the terms of the GNU General Public License as
published by the Free Software Foundation; either version 2 of the License,
or (at your option) any later version.

Project: Omaha source code is distributed in the hope that it will be
useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Project: Omaha source code; if not, see COPYING.txt in the
source tree, or write to the Free Software Foundation, Inc.,
51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
===========================================================================
*/

// snd_stream_async.cpp -- decode-ahead worker for 2D sound streams.

#include "snd_stream_async.h"

#include <condition_variable>
#include <mutex>
#include <string.h>
#include <thread>

#define ASYNC_STREAM_MAX_JOBS   16
#define ASYNC_STREAM_RING       16
#define ASYNC_STREAM_CHUNK_SIZE (2048 * 2 * 2)

struct sndAsyncStream_s {
    snd_stream_t *stream;
    unsigned int  chunkBytes;
    unsigned int  skip; // bytes still to decode and drop before the first chunk (start offset, loop start)
    unsigned int  sizes[ASYNC_STREAM_RING];
    byte          data[ASYNC_STREAM_RING][ASYNC_STREAM_CHUNK_SIZE];
    int           head;  // next chunk to pop (main thread)
    int           count; // decoded chunks waiting
    bool          used;  // the slot holds a stream (attached, or released while the worker still reads it)
    bool          eof;
    bool          busy;  // worker is inside S_CodecReadStream for this job
    bool          dead;  // never decode again (released, or handles closed by FS_Shutdown)
};

static std::mutex              s_asyncMutex;
static std::condition_variable s_asyncWork;
static std::condition_variable s_asyncIdle;
static std::thread             s_asyncThread;
static bool                    s_asyncQuit;
// allocated on first use and kept: an attach costs no allocation
static sndAsyncStream_t       *s_asyncJobs[ASYNC_STREAM_MAX_JOBS];
static int                     s_asyncNext;
static cvar_t                 *s_streamAsync;

static sndAsyncStream_t *S_AsyncStream_NextJob(void)
{
    int i;

    for (i = 0; i < ASYNC_STREAM_MAX_JOBS; i++) {
        sndAsyncStream_t *job = s_asyncJobs[(s_asyncNext + i) % ASYNC_STREAM_MAX_JOBS];

        if (job && job->used && !job->dead && !job->eof && job->count < ASYNC_STREAM_RING) {
            s_asyncNext = (s_asyncNext + i + 1) % ASYNC_STREAM_MAX_JOBS;
            return job;
        }
    }
    return NULL;
}

static void S_AsyncStream_Worker(void)
{
    std::unique_lock<std::mutex> lock(s_asyncMutex);

    while (!s_asyncQuit) {
        sndAsyncStream_t *job = S_AsyncStream_NextJob();
        unsigned int      want;
        int               slot, bytes;
        bool              skipping;

        if (!job) {
            s_asyncWork.wait(lock);
            continue;
        }

        // The tail slot is outside [head, head + count), so the main thread
        // never reads it while we decode into it unlocked. A skip decodes into
        // it too and drops the bytes.
        slot      = (job->head + job->count) % ASYNC_STREAM_RING;
        skipping  = job->skip > 0;
        want      = skipping ? Q_min(job->skip, job->chunkBytes) : job->chunkBytes;
        job->busy = true;
        lock.unlock();

        bytes = S_CodecReadStream(job->stream, want, job->data[slot]);

        lock.lock();
        job->busy = false;
        if (job->dead) {
            // released: the main thread closes the stream (S_AsyncStream_Update)
        } else if (bytes > 0 && (unsigned int)bytes <= want) {
            if (skipping) {
                job->skip -= (unsigned int)bytes;
            } else {
                job->sizes[slot] = (unsigned int)bytes;
                job->count++;
            }
        } else {
            if (bytes < 0 || (unsigned int)bytes > want) {
                job->dead = true;
            }
            job->eof = true;
        }
        s_asyncIdle.notify_all();
    }
}

// FS_Shutdown closes every open handle; keep the worker off them from now on.
static void S_AsyncStream_FSShutdown(void)
{
    std::unique_lock<std::mutex> lock(s_asyncMutex);
    int                          i;

    for (i = 0; i < ASYNC_STREAM_MAX_JOBS; i++) {
        if (s_asyncJobs[i] && s_asyncJobs[i]->used) {
            s_asyncJobs[i]->dead = true;
        }
    }
    for (i = 0; i < ASYNC_STREAM_MAX_JOBS; i++) {
        while (s_asyncJobs[i] && s_asyncJobs[i]->busy) {
            s_asyncIdle.wait(lock);
        }
    }
}

qboolean S_AsyncStream_Enabled(void)
{
#if defined(__EMSCRIPTEN__) && !defined(__EMSCRIPTEN_PTHREADS__)
    // no threads: the streams decode on the main thread
    return qfalse;
#endif
    if (!s_streamAsync) {
        s_streamAsync = Cvar_Get("s_streamAsync", "1", CVAR_ARCHIVE);
    }
    return s_streamAsync->integer ? qtrue : qfalse;
}

sndAsyncStream_t *S_AsyncStream_Attach(snd_stream_t *stream, unsigned int chunkBytes, unsigned int skipBytes)
{
    sndAsyncStream_t *job = NULL;
    int               i, freeSlot = -1;

    if (!S_AsyncStream_Enabled() || !stream || !chunkBytes || chunkBytes > ASYNC_STREAM_CHUNK_SIZE) {
        return NULL;
    }

    {
        std::lock_guard<std::mutex> lock(s_asyncMutex);

        for (i = 0; i < ASYNC_STREAM_MAX_JOBS; i++) {
            if (!s_asyncJobs[i] || !s_asyncJobs[i]->used) {
                freeSlot = i;
                break;
            }
        }
        if (freeSlot < 0) {
            // every slot streams: this one decodes on the main thread
            return NULL;
        }
        if (!s_asyncJobs[freeSlot]) {
            s_asyncJobs[freeSlot] = new sndAsyncStream_t;
        }

        job             = s_asyncJobs[freeSlot];
        job->stream     = stream;
        job->chunkBytes = chunkBytes;
        job->skip       = skipBytes;
        job->head       = 0;
        job->count      = 0;
        job->used       = true;
        job->eof        = false;
        job->busy       = false;
        job->dead       = false;

        if (!s_asyncThread.joinable()) {
            s_asyncQuit   = false;
            s_asyncThread = std::thread(S_AsyncStream_Worker);
        }
        fs_shutdownHook = S_AsyncStream_FSShutdown;
    }
    s_asyncWork.notify_one();

    return job;
}

int S_AsyncStream_Pop(sndAsyncStream_t *job, void *out, unsigned int outSize)
{
    unsigned int size;
    int          slot;

    {
        std::lock_guard<std::mutex> lock(s_asyncMutex);

        if (!job->count) {
            // dead here means FS_Shutdown closed the handle: end the stream like a failed read
            return (job->eof || job->dead) ? -1 : 0;
        }
        slot = job->head;
    }

    // The head slot stays ours until count is decremented below.
    size = job->sizes[slot];
    if (size > outSize) {
        size = outSize;
    }
    memcpy(out, job->data[slot], size);

    {
        std::lock_guard<std::mutex> lock(s_asyncMutex);

        job->head = (job->head + 1) % ASYNC_STREAM_RING;
        job->count--;
    }
    s_asyncWork.notify_one();

    return (int)size;
}

void S_AsyncStream_Release(sndAsyncStream_t *job)
{
    snd_stream_t *stream;

    {
        std::lock_guard<std::mutex> lock(s_asyncMutex);

        job->dead = true;
        if (job->busy) {
            // the worker still reads it: closed by a later S_AsyncStream_Update
            return;
        }
        stream      = job->stream;
        job->stream = NULL;
        job->used   = false;
    }

    S_CodecCloseStream(stream);
}

void S_AsyncStream_Update(void)
{
    snd_stream_t *closing[ASYNC_STREAM_MAX_JOBS];
    int           i, n = 0;

    {
        std::lock_guard<std::mutex> lock(s_asyncMutex);

        for (i = 0; i < ASYNC_STREAM_MAX_JOBS; i++) {
            sndAsyncStream_t *job = s_asyncJobs[i];

            if (job && job->used && job->dead && !job->busy && job->stream) {
                closing[n++] = job->stream;
                job->stream  = NULL;
                job->used    = false;
            }
        }
    }

    for (i = 0; i < n; i++) {
        S_CodecCloseStream(closing[i]);
    }
}

void S_AsyncStream_Shutdown(void)
{
    int i;

    if (s_asyncThread.joinable()) {
        {
            std::lock_guard<std::mutex> lock(s_asyncMutex);

            // Every stream channel releases its job in clear_stream before the host shuts down.
            for (i = 0; i < ASYNC_STREAM_MAX_JOBS; i++) {
                assert(!s_asyncJobs[i] || !s_asyncJobs[i]->used || s_asyncJobs[i]->dead);
            }
            s_asyncQuit = true;
        }
        s_asyncWork.notify_all();
        s_asyncThread.join();
    }

    // the worker is gone: close what it was still reading
    S_AsyncStream_Update();

    for (i = 0; i < ASYNC_STREAM_MAX_JOBS; i++) {
        delete s_asyncJobs[i];
        s_asyncJobs[i] = NULL;
    }
}
