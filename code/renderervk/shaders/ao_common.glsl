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

// ao_common.glsl -- what the passes of the ambient occlusion share (r_ao,
// VK_TemporalAo in vk_temporal.c): one descriptor set and one push constant
// block for all of them, each pass uses some of the bindings.
//  - ao_prefilter.comp: the depth buffer to view distances, 5 levels
//  - gtao_main.comp: the occlusion (GTAO), noisy
//  - ao_temporal.comp: blended with the last frames' result and blurred
//    (AoBlur) into the factor the apply pass multiplies the scene with
//    (AoResult, ao_apply.frag), or the first of two blurs
//  - ao_denoise.comp: the second of two blurs
// View distances are positive, the view weapon's negative (it is in its own
// depth range and only occludes itself), the sky at the far distance.

#extension GL_GOOGLE_include_directive : require
#include "temporal_common.glsl"

#define AO_LEVELS	5	// of the view distances (VK_TEMPORAL_AO_LEVELS)

layout( set = 0, binding = 0 ) uniform sampler2D u_depth;		// the depth buffer
layout( set = 0, binding = 1 ) uniform sampler2D u_normal;
layout( set = 0, binding = 2 ) uniform sampler2D u_velocity;
layout( set = 0, binding = 3 ) uniform sampler2D u_aoDepth;		// this frame's view distances, every level
layout( set = 0, binding = 4 ) uniform sampler2D u_prevAoDepth;	// the last frame's
layout( set = 0, binding = 5 ) uniform sampler2D u_raw;			// gtao_main.comp's result: one frame's visibility
layout( set = 0, binding = 6 ) uniform sampler2D u_history;		// the last frame's accumulated visibility, frames
layout( set = 0, binding = 7, r32f ) uniform writeonly image2D o_depth0;
layout( set = 0, binding = 8, r32f ) uniform writeonly image2D o_depth1;
layout( set = 0, binding = 9, r32f ) uniform writeonly image2D o_depth2;
layout( set = 0, binding = 10, r32f ) uniform writeonly image2D o_depth3;
layout( set = 0, binding = 11, r32f ) uniform writeonly image2D o_depth4;
layout( set = 0, binding = 12, r8 ) uniform writeonly image2D o_raw;
layout( set = 0, binding = 13, rg16f ) uniform writeonly image2D o_history;	// this frame's accumulated visibility, frames
layout( set = 0, binding = 14, rg16f ) uniform writeonly image2D o_blur;		// the first blur's result with two blurs
layout( set = 0, binding = 15, rgba8 ) uniform writeonly image2D o_result;	// what ao_apply.frag blends (8 bits: less than a step of the frame)
layout( set = 0, binding = 16 ) uniform sampler2D u_curHistory;	// o_history, sampled (the blur reads it)
layout( set = 0, binding = 17 ) uniform sampler2D u_blur;			// o_blur, sampled

// vkAoPush_t
layout( push_constant ) uniform Push {
	TemporalView	view;
	vec4	ao;			// the radius (world units), the falloff's share of it, the thin occluder compensation, r_aoStrength (a power)
	vec4	ao2;		// the multi-bounce albedo (0: none), 1: the history is the frame before's, the most frames blended, the blur's center weight
	vec4	fogColor;	// rgb, 1 when the view is fogged
	vec4	fogParams;	// the fog factor's scale and offset (of the view distance), 1: the draws' normals, 1: the last view distances are the frame before's
	ivec4	ints;		// slices, steps per side, the noise's frame index, the blur pass (AO_PASS_*)
} pc;

// the blurs (pc.ints.w): ao_temporal.comp's to o_blur (two blurs) or to the
// result (one), ao_denoise.comp's o_blur to the result
const int AO_PASS_HISTORY_TO_BLUR = 0;
const int AO_PASS_HISTORY_TO_RESULT = 1;
const int AO_PASS_BLUR_TO_RESULT = 2;

// the 3 x 3 around a texel p of level 0 in 4 gathers of one component:
// corners at p, p + (1, 0), p + (0, 1), p + (1, 1) (in texels). Gather order
// is (x0, y1), (x1, y1), (x1, y0), (x0, y0): g[0] = (L, C, T, TL),
// g[1] = (C, R, TR, T), g[2] = (BL, B, C, L), g[3] = (B, BR, R, C)
#define AO_GATHER3X3( tex, p, comp, g ) { \
	vec2 inv_ = 1.0 / vec2( textureSize( tex, 0 ) ); \
	g[0] = textureGather( tex, vec2( p ) * inv_, comp ); \
	g[1] = textureGather( tex, vec2( p + ivec2( 1, 0 ) ) * inv_, comp ); \
	g[2] = textureGather( tex, vec2( p + ivec2( 0, 1 ) ) * inv_, comp ); \
	g[3] = textureGather( tex, vec2( p + ivec2( 1, 1 ) ) * inv_, comp ); }

// the sky and what is farther at AoFar. The view distances are 32-bit
// floats: in half floats (XeGTAO's) a wall close to the eye or seen at a
// grazing angle stepped from pixel to pixel, each step a ledge that
// occluded the next: the wall went dark in stripes
float AoFar() {
	return min( pc.view.depth.w, 60000.0 );
}

bool AoIsFar( float d ) {
	return d >= AoFar() * 0.999;
}

// XeGTAO's edges of a pixel of view distance z to its neighbours (left,
// right, top, bottom): 1 where the neighbour's distance continues the
// surface (the slope across the pixel taken out), 0 where it's another
// surface; continuous. XeGTAO packs them in 2 bits each: a camera moved by a
// ten-thousandth of a unit (a player pushing into a wall) rounds every
// depth anew, edges near a step flipped by a third, and the blur's weights
// with them: the occlusion wobbled in bands
vec4 AoEdges( float z, float l, float r, float t, float b ) {
	vec4 e = vec4( l, r, t, b ) - z;
	float slopeLR = ( e.y - e.x ) * 0.5;
	float slopeTB = ( e.w - e.z ) * 0.5;
	vec4 adjusted = e + vec4( slopeLR, -slopeLR, slopeTB, -slopeTB );

	e = min( abs( e ), abs( adjusted ) );
	return clamp( 1.25 - e / ( abs( z ) * 0.011 + 1e-4 ), 0.0, 1.0 );
}

// XeGTAO's blur (Intel, MIT license) of the pixel C: the 3 x 3 around it,
// each neighbour weighted by the depth edges between them both ways
// (AoEdges, from the view distances of the 3 x 3 and the 4 pixels two away,
// LL, RR, TT, BB), so nothing crosses from one surface to another; the center
// weighs pc.ao2.w (the first of two blurs a fifth of it), times 1 + a
// quarter of the frames blended past the first: a converged pixel keeps its
// detail. v: TL, T, TR, L, C, R, BL, B, BR; z: the same, then LL, RR, TT, BB
float AoBlur( float v[9], float z[13], float frames, bool final ) {
	vec4 eC = AoEdges( z[4], z[3], z[5], z[1], z[7] );
	vec4 eL = AoEdges( z[3], z[9], z[4], z[0], z[6] );
	vec4 eR = AoEdges( z[5], z[4], z[10], z[2], z[8] );
	vec4 eT = AoEdges( z[1], z[0], z[2], z[11], z[4] );
	vec4 eB = AoEdges( z[7], z[6], z[8], z[4], z[12] );
	const float diag = 0.85 * 0.5;

	// both ways
	eC *= vec4( eL.y, eR.x, eT.w, eB.z );
	float wTL = diag * ( eC.x * eL.z + eC.z * eT.x );
	float wTR = diag * ( eC.z * eT.y + eC.y * eR.z );
	float wBL = diag * ( eC.w * eB.x + eC.x * eL.w );
	float wBR = diag * ( eC.y * eR.w + eC.w * eB.y );
	float center = pc.ao2.w * ( final ? 1.0 : 0.2 ) * ( 1.0 + 0.25 * max( frames - 1.0, 0.0 ) );
	float sum = v[4] * center + v[3] * eC.x + v[5] * eC.y + v[1] * eC.z + v[7] * eC.w
		+ v[0] * wTL + v[2] * wTR + v[6] * wBL + v[8] * wBR;

	return sum / ( center + dot( eC, vec4( 1.0 ) ) + wTL + wTR + wBL + wBR );
}

// Jimenez et al. 2016, the multi-bounce fit
float AoMultiBounce( float v, float albedo ) {
	float a = 2.0404 * albedo - 0.3324;
	float b = -4.7951 * albedo + 0.6417;
	float c = 2.7552 * albedo + 0.6903;

	return max( v, ( ( v * a + b ) * v + c ) * v );
}

// what ao_apply.frag blends into the scene for the blurred visibility v at
// view distance sd: v to the power r_aoStrength, the multi-bounce fit, in
// the scene's gamma-encoded values (2.2: the game draws them so), undone
// through the fog: the scene is C f + fog (1 - f), the occlusion belongs on
// C alone; with a the factor, rgb = fog (1 - f)(1 - a), blended ONE,
// SRC_ALPHA: dst a + rgb
vec4 AoResult( float v, float sd ) {
	float d = abs( sd );

	if ( AoIsFar( d ) ) {
		return vec4( 0.0, 0.0, 0.0, 1.0 );
	}
	// in linear light, then the scene's encoding
	v = pow( clamp( v, 0.0, 1.0 ), pc.ao.w );
	if ( pc.ao2.x > 0.0 ) {
		v = AoMultiBounce( v, pc.ao2.x );
	}
	float a = pow( clamp( v, 0.0, 1.0 ), 1.0 / 2.2 );
	vec3 fog = vec3( 0.0 );

	if ( pc.fogColor.w != 0.0 ) {
		float f = clamp( d * pc.fogParams.x + pc.fogParams.y, 0.0, 1.0 );

		fog = pc.fogColor.rgb * ( 1.0 - f ) * ( 1.0 - a );
	}
	return vec4( fog, a );
}
