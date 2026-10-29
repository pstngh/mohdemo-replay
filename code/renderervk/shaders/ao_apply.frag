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

// ao_apply.frag -- the ambient occlusion into the scene (r_ao,
// VK_TemporalAo): drawn in the scene's render pass after its opaque
// surfaces and before what is blended over them (the effects in front of a
// corner aren't darkened), blended ONE, SRC_ALPHA into the color only:
// dst a + rgb, ao_denoise.comp's result (the factor, and the fog's part).

#version 450

layout( set = 0, binding = 0 ) uniform sampler2D u_ao;

layout( location = 0 ) out vec4 out_color;

void main() {
	vec4 ao = texelFetch( u_ao, ivec2( gl_FragCoord.xy ), 0 );

	// nothing to change (most of the view): no blend, no write
	if ( ao.a >= 254.5 / 255.0 ) {
		discard;
	}
	out_color = ao;
}
