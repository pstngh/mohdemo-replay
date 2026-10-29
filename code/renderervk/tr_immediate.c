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

// tr_immediate.c -- immediate mode emulation
//
// Debug visualizations and a few 2D helpers build their geometry one vertex
// at a time. Vertices are collected here and converted to indexed triangles
// or lines for vk_draw(). Each vertex takes the current color (glState.color)
// and the last texture coordinate; texture unit 0 is used when enabled.
// Quads and polygons drawn with GLS_POLYMODE_LINE become their outlines: only
// the edges of the polygon are drawn, not the triangulation.

#include "tr_local.h"

#define IMM_MAX_VERTEXES	4092	// a multiple of 2, 3 and 4
#define IMM_MAX_INDEXES		( IMM_MAX_VERTEXES * 3 )

static struct {
	qboolean	active;
	immPrim_t	prim;
	int			numVertexes;
	float		currentSt[2];
	float		xyz[IMM_MAX_VERTEXES][3];
	float		colors[IMM_MAX_VERTEXES][4];
	byte		colorBytes[IMM_MAX_VERTEXES][4];
	float		st[IMM_MAX_VERTEXES][2];
	glIndex_t	indexes[IMM_MAX_INDEXES];
} imm;

/*
==============
RB_ImmVertexesPerPrimitive

Returns how many vertexes make a whole primitive for primitive types that
can be split across several draws, 0 for the others
==============
*/
static int RB_ImmVertexesPerPrimitive( immPrim_t prim ) {
	switch ( prim ) {
	case IMM_TRIANGLES:
		return 3;
	case IMM_QUADS:
		return 4;
	case IMM_LINES:
		return 2;
	default:
		return 0;
	}
}

/*
==============
RB_ImmOutlineIndexes

Builds the edges of the collected quads or polygon as lines, returns the
number of indexes
==============
*/
static int RB_ImmOutlineIndexes( int numVertexes ) {
	int numIndexes;
	int i, j;

	numIndexes = 0;

	if ( imm.prim == IMM_QUADS ) {
		for ( i = 0; i + 3 < numVertexes; i += 4 ) {
			for ( j = 0; j < 4; j++ ) {
				imm.indexes[numIndexes++] = i + j;
				imm.indexes[numIndexes++] = i + ( ( j + 1 ) & 3 );
			}
		}
	} else if ( numVertexes >= 3 ) {
		for ( i = 0; i < numVertexes; i++ ) {
			imm.indexes[numIndexes++] = i;
			imm.indexes[numIndexes++] = ( i + 1 ) % numVertexes;
		}
	}

	return numIndexes;
}

/*
==============
RB_ImmTriangleIndexes

Converts the collected vertexes to triangle or line indexes, returns the
number of indexes
==============
*/
static int RB_ImmTriangleIndexes( int numVertexes ) {
	int numIndexes;
	int i;

	numIndexes = 0;

	switch ( imm.prim ) {
	case IMM_TRIANGLES:
	case IMM_LINES:
		for ( i = 0; i < numVertexes; i++ ) {
			imm.indexes[numIndexes++] = i;
		}
		break;
	case IMM_TRIANGLE_STRIP:
		for ( i = 0; i < numVertexes - 2; i++ ) {
			// keep the winding of odd triangles
			if ( i & 1 ) {
				imm.indexes[numIndexes++] = i + 1;
				imm.indexes[numIndexes++] = i;
			} else {
				imm.indexes[numIndexes++] = i;
				imm.indexes[numIndexes++] = i + 1;
			}
			imm.indexes[numIndexes++] = i + 2;
		}
		break;
	case IMM_QUADS:
		for ( i = 0; i + 3 < numVertexes; i += 4 ) {
			imm.indexes[numIndexes++] = i;
			imm.indexes[numIndexes++] = i + 1;
			imm.indexes[numIndexes++] = i + 2;
			imm.indexes[numIndexes++] = i;
			imm.indexes[numIndexes++] = i + 2;
			imm.indexes[numIndexes++] = i + 3;
		}
		break;
	case IMM_POLYGON:
		for ( i = 1; i < numVertexes - 1; i++ ) {
			imm.indexes[numIndexes++] = 0;
			imm.indexes[numIndexes++] = i;
			imm.indexes[numIndexes++] = i + 1;
		}
		break;
	case IMM_LINE_LOOP:
		if ( numVertexes >= 2 ) {
			for ( i = 0; i < numVertexes - 1; i++ ) {
				imm.indexes[numIndexes++] = i;
				imm.indexes[numIndexes++] = i + 1;
			}
			imm.indexes[numIndexes++] = numVertexes - 1;
			imm.indexes[numIndexes++] = 0;
		}
		break;
	}

	return numIndexes;
}

/*
==============
RB_ImmFlush

Draws the collected vertexes
==============
*/
static void RB_ImmFlush( void ) {
	drawGeometry_t	geometry;
	primType_t		prim;
	int				numVertexes;
	int				numIndexes;
	int				i;

	numVertexes = imm.numVertexes;

	// drop incomplete primitives
	if ( imm.prim == IMM_TRIANGLES ) {
		numVertexes -= numVertexes % 3;
	} else if ( imm.prim == IMM_LINES ) {
		numVertexes &= ~1;
	}

	if ( ( glState.glStateBits & GLS_POLYMODE_LINE ) && ( imm.prim == IMM_QUADS || imm.prim == IMM_POLYGON ) ) {
		prim = PRIM_LINES;
		numIndexes = RB_ImmOutlineIndexes( numVertexes );
	} else {
		prim = ( imm.prim == IMM_LINES || imm.prim == IMM_LINE_LOOP ) ? PRIM_LINES : PRIM_TRIANGLES;
		numIndexes = RB_ImmTriangleIndexes( numVertexes );
	}

	imm.numVertexes = 0;

	if ( !numIndexes ) {
		return;
	}

	Com_Memset( &geometry, 0, sizeof( geometry ) );
	geometry.numVertexes = numVertexes;
	geometry.xyz = imm.xyz[0];
	geometry.xyzStride = sizeof( imm.xyz[0] );

	// a single color is passed as a float constant to keep it exact,
	// several colors as per-vertex bytes
	for ( i = 1; i < numVertexes; i++ ) {
		if ( memcmp( imm.colors[i], imm.colors[0], sizeof( imm.colors[0] ) ) ) {
			break;
		}
	}
	if ( i == numVertexes ) {
		Com_Memcpy( geometry.color, imm.colors[0], sizeof( geometry.color ) );
	} else {
		for ( i = 0; i < numVertexes; i++ ) {
			int j;

			for ( j = 0; j < 4; j++ ) {
				imm.colorBytes[i][j] = (byte)( imm.colors[i][j] * 255.0f + 0.5f );
			}
		}
		geometry.colors = imm.colorBytes[0];
	}

	if ( glState.texEnabled[0] ) {
		geometry.st[0] = imm.st[0];
		geometry.stStride[0] = sizeof( imm.st[0] );
	}

	vk_draw( prim, &geometry, numIndexes, imm.indexes );
}

/*
==============
RB_ImmBegin
==============
*/
void RB_ImmBegin( immPrim_t prim ) {
	if ( imm.active ) {
		ri.Printf( PRINT_DEVELOPER, "RB_ImmBegin: already active\n" );
	}

	imm.active = qtrue;
	imm.prim = prim;
	imm.numVertexes = 0;
}

/*
==============
RB_ImmEnd
==============
*/
void RB_ImmEnd( void ) {
	if ( !imm.active ) {
		ri.Printf( PRINT_DEVELOPER, "RB_ImmEnd: not active\n" );
		return;
	}

	RB_ImmFlush();
	imm.active = qfalse;
}

/*
==============
RB_ImmTexCoord2f
==============
*/
void RB_ImmTexCoord2f( float s, float t ) {
	imm.currentSt[0] = s;
	imm.currentSt[1] = t;
}

/*
==============
RB_ImmTexCoord2fv
==============
*/
void RB_ImmTexCoord2fv( const float *st ) {
	RB_ImmTexCoord2f( st[0], st[1] );
}

/*
==============
RB_ImmVertex3f
==============
*/
void RB_ImmVertex3f( float x, float y, float z ) {
	int vertexesPerPrimitive;
	int n;

	if ( !imm.active ) {
		return;
	}

	if ( imm.numVertexes == IMM_MAX_VERTEXES ) {
		vertexesPerPrimitive = RB_ImmVertexesPerPrimitive( imm.prim );
		if ( !vertexesPerPrimitive ) {
			ri.Printf( PRINT_DEVELOPER, "RB_ImmVertex3f: too many vertexes\n" );
			return;
		}

		// IMM_MAX_VERTEXES is a multiple of every splittable primitive
		// size, so the buffer only holds whole primitives
		RB_ImmFlush();
	}

	n = imm.numVertexes++;
	imm.xyz[n][0] = x;
	imm.xyz[n][1] = y;
	imm.xyz[n][2] = z;
	Vector4Copy( glState.color, imm.colors[n] );
	imm.st[n][0] = imm.currentSt[0];
	imm.st[n][1] = imm.currentSt[1];
}

/*
==============
RB_ImmVertex2f
==============
*/
void RB_ImmVertex2f( float x, float y ) {
	RB_ImmVertex3f( x, y, 0 );
}

/*
==============
RB_ImmVertex3fv
==============
*/
void RB_ImmVertex3fv( const float *xyz ) {
	RB_ImmVertex3f( xyz[0], xyz[1], xyz[2] );
}
