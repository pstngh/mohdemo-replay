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

// snd_stream_async.h -- decode-ahead worker for 2D sound streams (music, dialog).
//
// While attached, the worker thread owns the codec stream: it drops the
// bytes the stream starts past (a start offset, a loop start), then decodes
// chunks ahead of playback; the main thread only pops finished PCM and never
// waits for the worker. Opening a stream stays on the main thread; a stream
// is attached once and released with the job, which closes it.
//
// Worker contract: codec->read runs on the worker thread, so it must not
// allocate from the zone or hunk, print, or call Com_Error. A negative read
// result marks the job dead and ends the stream like end of file.

#pragma once

#include "../snd_codec.h"

typedef struct sndAsyncStream_s sndAsyncStream_t;

// Whether streams decode on the worker (s_streamAsync 1); else on the main thread.
qboolean S_AsyncStream_Enabled(void);

// Hand an open stream to the worker, which first decodes and drops skipBytes.
// Returns NULL when async decode is disabled, every job slot is in use, or the
// chunk does not fit; the caller keeps decoding inline.
sndAsyncStream_t *S_AsyncStream_Attach(snd_stream_t *stream, unsigned int chunkBytes, unsigned int skipBytes);

// Next decoded chunk: > 0 bytes copied, 0 = not decoded yet, -1 = end of stream.
int S_AsyncStream_Pop(sndAsyncStream_t *job, void *out, unsigned int outSize);

// Take the job back and close its stream: now, or once the worker is done
// with its read (S_AsyncStream_Update). Never waits.
void S_AsyncStream_Release(sndAsyncStream_t *job);

// Main thread, every frame: close the released streams the worker has let go.
void S_AsyncStream_Update(void);

// Stop the worker thread (sound shutdown). All jobs must be released.
void S_AsyncStream_Shutdown(void);
