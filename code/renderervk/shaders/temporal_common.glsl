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

// temporal_common.glsl -- what the temporal path's passes and the passes
// that read its buffers share (r_temporal, vk_temporal.c): the normals
// buffer's encoding, view distances from the depth buffer, and what the
// buffers leave to their readers:
//  - the motion of what doesn't move: only the draws that move (entities
//    with a last frame, skeletal models) write the motion vectors, flag
//    TEMPORAL_FLAG_MOTION; elsewhere the motion is the camera's
//    (TemporalCameraMotion, from the view distance)
//  - the normals the draws didn't give (flag TEMPORAL_FLAG_NORMAL clear):
//    TemporalDerivedNormal, from the view distances around the pixel
// Include it with #extension GL_GOOGLE_include_directive.

const uint TEMPORAL_FLAG_NORMAL = 1u;	// a draw wrote the pixel's normal
const uint TEMPORAL_FLAG_MOTION = 2u;	// and its motion vector (it moves)

// the class in the normals buffer's 2 alpha bits (VK_TEMPORAL_CLASS_*)
const int TEMPORAL_CLASS_NONE = 0;		// no draw that hides what is behind it
const int TEMPORAL_CLASS_SURFACE = 1;
const int TEMPORAL_CLASS_WEAPON = 2;	// the view weapon (depth hack range)
const int TEMPORAL_CLASS_SKY = 3;

// the primary view, as vkTemporalView_t (vk_local.h)
struct TemporalView {
	ivec4	rect;		// x, y (from the top-left), width, height in framebuffer pixels
	vec4	proj;		// projection[0], [5], [8], [9] (unjittered)
	vec4	depth;		// projection[10], [14], the depth floor (vk_depth_floor), the far distance
	vec4	params;		// the jitter in NDC (xy), 1 when the view has a last frame, the depth hack range's end
	mat4	reproject;	// the last frame's projection * the camera's motion: GL eye space to last clip space
};

uint TemporalFlags( vec4 n ) {
	return uint( n.b * 1023.0 + 0.5 );
}

int TemporalClass( vec4 n ) {
	return int( n.a * 3.0 + 0.5 );
}

vec2 TemporalOctEncode( vec3 n ) {
	n /= abs( n.x ) + abs( n.y ) + abs( n.z );
	if ( n.z < 0.0 ) {
		n.xy = ( 1.0 - abs( n.yx ) ) * vec2( n.x >= 0.0 ? 1.0 : -1.0, n.y >= 0.0 ? 1.0 : -1.0 );
	}
	return n.xy * 0.5 + 0.5;
}

vec3 TemporalOctDecode( vec2 e ) {
	vec2 f = e * 2.0 - 1.0;
	vec3 n = vec3( f, 1.0 - abs( f.x ) - abs( f.y ) );
	float t = max( -n.z, 0.0 );

	n.xy += vec2( n.x >= 0.0 ? -t : t, n.y >= 0.0 ? -t : t );
	return normalize( n );
}

// the view distance of a window depth whose range [lo, hi] stands for GL's
// [-1, 1]; the far distance for the sky (1)
float TemporalRangeDistance( TemporalView v, float wz, float lo, float hi ) {
	float ndc = ( wz - lo ) / ( hi - lo ) * 2.0 - 1.0;
	float denom = ndc + v.depth.x;

	if ( wz >= 1.0 || abs( denom ) < 1e-9 ) {
		return v.depth.w;
	}
	return clamp( v.depth.y / denom, 0.0, v.depth.w );
}

// the view distance of a pixel of the depth buffer: the world's range
// [floor, 1], or the view weapon's [0, hackMax] (what is nearer than the
// floor with r_depthClamp, or its class without: depth alone can't tell)
float TemporalViewDistance( TemporalView v, float wz, bool weapon ) {
	return weapon || wz < v.depth.z ? TemporalRangeDistance( v, wz, 0.0, v.params.w ) : TemporalRangeDistance( v, wz, v.depth.z, 1.0 );
}

// the NDC of a framebuffer position (pixel centers at .5) without the
// jitter: where the surface it shows is in the unjittered projection
vec2 TemporalPixelNdc( TemporalView v, vec2 pixel ) {
	vec2 ndc = ( pixel - vec2( v.rect.xy ) ) / vec2( v.rect.zw ) * 2.0 - 1.0;

	// framebuffer y goes down
	ndc.y = -ndc.y;
	return ndc - v.params.xy;
}

// the GL eye space position (x right, y up, looking down -z) at view
// distance d of a framebuffer position
vec3 TemporalViewPosition( TemporalView v, vec2 pixel, float d ) {
	vec2 ndc = TemporalPixelNdc( v, pixel );

	return vec3( ( ndc.x + v.proj.z ) * d / v.proj.x, ( ndc.y + v.proj.w ) * d / v.proj.y, -d );
}

// the motion of a surface that doesn't move at view distance d (the far
// distance: the sky, a direction), in the motion vectors' units: this
// frame's position minus the last frame's, in units of the viewport (x
// right, y down); 0 without a last frame
vec2 TemporalCameraMotion( TemporalView v, vec2 pixel, float d ) {
	bool far = d >= v.depth.w * 0.999;
	vec4 prevClip;

	if ( v.params.z == 0.0 ) {
		return vec2( 0.0 );
	}
	prevClip = v.reproject * ( far ? vec4( TemporalViewPosition( v, pixel, 1.0 ), 0.0 ) : vec4( TemporalViewPosition( v, pixel, d ), 1.0 ) );
	if ( prevClip.w <= 1e-6 ) {
		return vec2( 0.0 );
	}
	return ( TemporalPixelNdc( v, pixel ) - prevClip.xy / prevClip.w ) * vec2( 0.5, -0.5 );
}

// the view space normal of the surface at a framebuffer position from the
// view distances there (d) and step pixels left, right, up and down: of the
// neighbours on each axis the one nearer in depth, so a normal at an edge
// belongs to its own surface; facing the eye
vec3 TemporalDerivedNormal( TemporalView v, vec2 pixel, float step, float d, float dl, float dr, float du, float dd ) {
	vec3 c = TemporalViewPosition( v, pixel, d );
	vec3 ex = abs( dl - d ) < abs( dr - d ) ? c - TemporalViewPosition( v, pixel - vec2( step, 0.0 ), dl )
		: TemporalViewPosition( v, pixel + vec2( step, 0.0 ), dr ) - c;
	vec3 ey = abs( du - d ) < abs( dd - d ) ? c - TemporalViewPosition( v, pixel - vec2( 0.0, step ), du )
		: TemporalViewPosition( v, pixel + vec2( 0.0, step ), dd ) - c;
	vec3 n = cross( ey, ex );
	float len = dot( n, n );

	if ( len < 1e-20 ) {
		return vec3( 0.0, 0.0, 1.0 );
	}
	n *= inversesqrt( len );
	return dot( n, c ) > 0.0 ? -n : n;
}
