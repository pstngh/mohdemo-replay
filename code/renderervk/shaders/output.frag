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

// output.frag -- the frame into the swapchain image with HDR output
// (r_hdrOutput, vk_output.c). The frame holds the values the game draws:
// gamma encoded (2.2, an SDR monitor's), 1.0 = paper white; the 3D scene's expansion
// (temporal_composite.frag) goes above 1, up to the peak. SDR swapchain:
// clamped at 1. HDR10: linear light in nits, BT.709 to BT.2020, ST 2084.
// scRGB: linear light, BT.709, 1.0 = 80 nits.

#version 450

layout(set = 0, binding = 0) uniform sampler2D u_frame;

layout(push_constant) uniform Push {
	int		mode;		// 0 SDR, 1 HDR10, 2 scRGB (vkHdrMode_t)
	int		test;		// the calibration screen (r_hdrOutputTestPage): 1 black, 2 peak, 0 none
	float	paperWhite;	// nits of 1.0
	float	peak;		// the display's peak over paper white, linear
	vec2	invExtent;	// 1 / the swapchain image's size
	float	black;		// r_hdrBlack: the black level's toe, 0 none
	float	sdrBox;		// the HDR look's peak screen: the box, linear (VK_OutputSdrBox)
} pc;

layout(location = 0) out vec4 out_color;

const int MODE_SDR = 0;
const int MODE_PQ = 1;

// BT.709 to BT.2020 primaries (ITU-R BT.2087), linear light
const mat3 BT709_TO_BT2020 = mat3(
	vec3( 0.6274040, 0.0690970, 0.0163916 ),
	vec3( 0.3292820, 0.9195400, 0.0880132 ),
	vec3( 0.0433136, 0.0113612, 0.8955950 ) );

// ST 2084 (PQ) of luminance in nits
vec3 PQ( vec3 nits ) {
	vec3 y = pow( clamp( nits / 10000.0, 0.0, 1.0 ), vec3( 0.1593017578125 ) );

	return pow( ( 0.8359375 + 18.8515625 * y ) / ( 1.0 + 18.6875 * y ), vec3( 78.84375 ) );
}

// the box in the middle of the calibration screens' pattern (the top four
// fifths: the client's menu is below, cl_ui.cpp)
bool InBox( vec2 uv ) {
	vec2 d = abs( uv - vec2( 0.5, 0.4 ) ) * vec2( pc.invExtent.y / pc.invExtent.x, 1.0 );	// square

	return d.x < 0.1 && d.y < 0.1;
}

// r_hdrBlack: the darkest shades scaled by (m / (m + 0.005))^black, m the
// largest channel in linear light of paper white (0.005: 1 nit at 203): 1
// for the midtones (0.2: 2.4 % at 1, 1.2 % at -0.5), a crush (black > 0) or
// a lift (< 0) below about a nit
vec3 BlackLevel( vec3 lin ) {
	float m = max( lin.r, max( lin.g, lin.b ) );

	return pc.black == 0.0 || m <= 0.0 ? lin : lin * pow( m / ( m + 0.005 ), pc.black );
}

// the calibration screens (r_hdrOutputTest), in linear light of paper
// white and not clipped at the peak, so the display's own limits show; the
// top four fifths of the screen, the menu's slider and button below. 1:
// black, a box of the game's value 8/255 on black, through r_hdrBlack:
// r_hdrBlack until it is barely visible. 2: peak, a box at r_hdrPeak on the
// largest signal (10000 nits): r_hdrPeak until the display clips both
// alike and the box is gone; with the HDR look (an SDR swapchain) a bright
// surface lifted by the look on SDR white instead (VK_OutputSdrBox). -1
// where the frame shows
vec3 TestPattern( vec2 uv ) {
	if ( uv.y >= 0.8 ) {
		return vec3( -1.0 );
	}
	if ( pc.test == 1 ) {
		return InBox( uv ) ? BlackLevel( vec3( pow( 8.0 / 255.0, 2.2 ) ) ) : vec3( 0.0 );
	}
	if ( pc.mode == MODE_SDR ) {
		// the HDR look (r_hdrOutput 2): a bright surface lifted by the look
		// toward SDR white, the most it can show (white at the slider's top)
		return InBox( uv ) ? BlackLevel( vec3( pc.sdrBox ) ) : vec3( 1.0 );
	}
	return InBox( uv ) ? vec3( pc.peak ) : vec3( 10000.0 / pc.paperWhite );
}

void main() {
	vec2 uv = gl_FragCoord.xy * pc.invExtent;
	vec3 c = texture( u_frame, uv ).rgb;
	vec3 lin;

	if ( pc.mode == MODE_SDR && pc.test == 0 ) {
		out_color = vec4( clamp( c, 0.0, 1.0 ), 1.0 );
		return;
	}

	// the 3D view's expansion stops at the peak (temporal_composite.frag),
	// the HUD and the unexpanded frame stay at 1
	lin = BlackLevel( min( pow( max( c, vec3( 0.0 ) ), vec3( 2.2 ) ), vec3( pc.peak ) ) );
	if ( pc.test != 0 ) {
		vec3 t = TestPattern( uv );

		// the pattern where the menu left its black background: what it
		// draws there (the mouse cursor) stays in front
		if ( t.r >= 0.0 && max( c.r, max( c.g, c.b ) ) < 1.0 / 255.0 ) {
			lin = t;
		}
	}

	if ( pc.mode == MODE_SDR ) {
		out_color = vec4( pow( min( lin, vec3( 1.0 ) ), vec3( 1.0 / 2.2 ) ), 1.0 );
	} else if ( pc.mode == MODE_PQ ) {
		out_color = vec4( PQ( max( BT709_TO_BT2020 * lin, vec3( 0.0 ) ) * pc.paperWhite ), 1.0 );
	} else {
		out_color = vec4( lin * ( pc.paperWhite / 80.0 ), 1.0 );
	}
}
