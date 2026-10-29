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

/*
 * PCM and IMA ADPCM decode for the Omaha mixer.
 * IMA follows the IMA/DVI ADPCM WAV layout.
 */

#include "snd_adpcm_omaha.h"

#include <stdlib.h>
#include <string.h>

/* IMA/DVI step sizes. */
static const int kImaStep[89] = {
	7,     8,     9,     10,    11,    12,    13,    14,    16,    17,    19,    21,    23,    25,    28,    31,
	34,    37,    41,    45,    50,    55,    60,    66,    73,    80,    88,    97,    107,   118,   130,   143,
	157,   173,   190,   209,   230,   253,   279,   307,   337,   371,   408,   449,   494,   544,   598,   658,
	724,   796,   876,   963,   1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,  2272,  2499,  2749,  3024,
	3327,  3660,  4026,  4428,  4871,  5358,  5894,  6484,  7132,  7845,  8630,  9493,  10442, 11487, 12635, 13899,
	15289, 16818, 18500, 20350, 22358, 24633, 27086, 29794, 32767
};

static const int kImaIndex[16] = {-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8};

static int Om_Clamp16(int v)
{
	if (v > 32767) {
		return 32767;
	}
	if (v < -32768) {
		return -32768;
	}
	return v;
}

static int Om_ImaNibble(int *sample, int *index, int nibble)
{
	int step = kImaStep[*index];
	int diff = step >> 3;

	if (nibble & 1) {
		diff += step >> 2;
	}
	if (nibble & 2) {
		diff += step >> 1;
	}
	if (nibble & 4) {
		diff += step;
	}
	if (nibble & 8) {
		diff = -diff;
	}
	*sample = Om_Clamp16(*sample + diff);
	*index += kImaIndex[nibble & 15];
	if (*index < 0) {
		*index = 0;
	}
	if (*index > 88) {
		*index = 88;
	}
	return *sample;
}

/*
 * WAVE DVI/IMA ADPCM: 4-byte header per channel, then 4-byte packets of 8
 * nibbles (low nibble first). Stereo interleaves those packets per channel.
 */
static int Om_ImaNibbleAt(const unsigned char *nibbles, int channels, int n)
{
	const int byteShift  = (n & 1) * 4;
	const int wordOffset = (n >> 1) & ~3;
	const int byteOffset = wordOffset * channels + ((n >> 1) & 3);
	return (nibbles[byteOffset] >> byteShift) & 0xf;
}

/*
 * Fixed in Omaha: whole blocks plus a truncated last block (a header and whole
 * 8-sample packets), so files and stream chunks need not end on a block. maxFrames
 * > 0 trims to the exact count from the fact chunk.
 */
static qboolean Om_DecodeIma(
	const unsigned char *src, int sizeBytes, int channels, int samplesPerBlock, int maxFrames, omPcmDecode_t *out
)
{
	int blockBytes, blocks, rem, tailFrames, frames;
	int b, ch, n, frame0;
	float *dst;

	if (channels < 1 || channels > 2 || samplesPerBlock <= 1 || ((samplesPerBlock - 1) % 8) != 0) {
		return qfalse;
	}
	blockBytes = ((samplesPerBlock - 1) / 2 + 4) * channels;
	if (blockBytes <= 0) {
		return qfalse;
	}
	blocks     = sizeBytes / blockBytes;
	rem        = sizeBytes % blockBytes;
	tailFrames = rem >= 4 * channels ? 1 + (rem - 4 * channels) / (4 * channels) * 8 : 0;
	frames     = blocks * samplesPerBlock + tailFrames;
	if (maxFrames > 0 && maxFrames < frames) {
		frames = maxFrames;
	}
	if (frames <= 0) {
		return qfalse;
	}
	out->channels    = channels;
	out->frames      = frames;
	out->interleaved = (float *)malloc((size_t)frames * (size_t)channels * sizeof(float));
	if (!out->interleaved) {
		return qfalse;
	}
	dst = out->interleaved;
	for (b = 0, frame0 = 0; frame0 < frames; b++, frame0 += samplesPerBlock) {
		const unsigned char *block = src + b * blockBytes;
		int                  count = b < blocks ? samplesPerBlock : tailFrames;
		if (count > frames - frame0) {
			count = frames - frame0;
		}
		for (ch = 0; ch < channels; ch++) {
			int                  sample  = (int)(short)(block[ch * 4] | (block[ch * 4 + 1] << 8));
			int                  index   = block[ch * 4 + 2];
			const unsigned char *nibbles = block + (channels + ch) * 4;

			if (index > 88) {
				index = 88;
			}
			dst[(frame0 * channels) + ch] = (float)sample * (1.0f / 32768.0f);
			for (n = 0; n < count - 1; n++) {
				sample = Om_ImaNibble(&sample, &index, Om_ImaNibbleAt(nibbles, channels, n));
				dst[((frame0 + n + 1) * channels) + ch] = (float)sample * (1.0f / 32768.0f);
			}
		}
	}
	return qtrue;
}

static qboolean Om_DecodePcm(omSampleFormat_t fmt, const void *data, int sizeBytes, omPcmDecode_t *out)
{
	int channels = (fmt == OM_FMT_STEREO8 || fmt == OM_FMT_STEREO16 || fmt == OM_FMT_STEREO_F32) ? 2 : 1;
	int i, n;
	float *dst;

	if (fmt == OM_FMT_MONO16 || fmt == OM_FMT_STEREO16) {
		n = sizeBytes / (2 * channels);
	} else if (fmt == OM_FMT_MONO_F32 || fmt == OM_FMT_STEREO_F32) {
		n = sizeBytes / (4 * channels);
	} else {
		n = sizeBytes / channels;
	}
	if (n <= 0) {
		return qfalse;
	}
	dst = (float *)malloc((size_t)n * (size_t)channels * sizeof(float));
	if (!dst) {
		return qfalse;
	}
	if (fmt == OM_FMT_MONO16 || fmt == OM_FMT_STEREO16) {
		const short *s = (const short *)data;
		for (i = 0; i < n * channels; i++) {
			dst[i] = (float)s[i] * (1.0f / 32768.0f);
		}
	} else if (fmt == OM_FMT_MONO_F32 || fmt == OM_FMT_STEREO_F32) {
		memcpy(dst, data, (size_t)n * (size_t)channels * sizeof(float));
	} else {
		const unsigned char *s = (const unsigned char *)data;
		for (i = 0; i < n * channels; i++) {
			dst[i] = (float)s[i] * (1.0f / 128.0f) - 1.0f;
		}
	}
	out->channels    = channels;
	out->frames      = n;
	out->interleaved = dst;
	return qtrue;
}

qboolean OmPcm_Decode(
	omSampleFormat_t fmt, const void *data, int sizeBytes, int blockAlign, int maxFrames, omPcmDecode_t *out
)
{
	int samples;

	memset(out, 0, sizeof(*out));
	if (!data || sizeBytes <= 0) {
		return qfalse;
	}
	switch (fmt) {
	case OM_FMT_MONO8:
	case OM_FMT_MONO16:
	case OM_FMT_STEREO8:
	case OM_FMT_STEREO16:
	case OM_FMT_MONO_F32:
	case OM_FMT_STEREO_F32:
		return Om_DecodePcm(fmt, data, sizeBytes, out);
	case OM_FMT_MONO_IMA4:
	case OM_FMT_STEREO_IMA4:
		samples = blockAlign > 1 ? blockAlign : 65;
		return Om_DecodeIma(
			(const unsigned char *)data, sizeBytes, fmt == OM_FMT_STEREO_IMA4 ? 2 : 1, samples, maxFrames, out
		);
	default:
		return qfalse;
	}
}

void OmPcm_Free(omPcmDecode_t *pcm)
{
	if (!pcm) {
		return;
	}
	free(pcm->interleaved);
	pcm->interleaved = NULL;
	pcm->frames      = 0;
	pcm->channels    = 0;
}
