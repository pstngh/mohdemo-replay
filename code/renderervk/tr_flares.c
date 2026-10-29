/*
===========================================================================
Copyright (C) 1999-2005 Id Software, Inc.

This file is part of Quake III Arena source code.

Quake III Arena source code is free software; you can redistribute it
and/or modify it under the terms of the GNU General Public License as
published by the Free Software Foundation; either version 2 of the License,
or (at your option) any later version.

Quake III Arena source code is distributed in the hope that it will be
useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Foobar; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
===========================================================================
*/
// tr_flares.c

#include "tr_local.h"

/*
=============================================================================

LIGHT FLARES

A light flare is an effect that takes place inside the eye when bright light
sources are visible.  The size of the flare reletive to the screen is nearly
constant, irrespective of distance, but the intensity should be proportional to the
projected area of the light source.

A surface that has been flagged as having a light flare will calculate the depth
buffer value that it's midpoint should have when the surface is added.

After all opaque surfaces have been rendered, the depth buffer is read back for
each flare in view.  If the point has not been obscured by a closer surface, the
flare should be drawn.

Surfaces that have a repeated texture should never be flagged as flaring, because
there will only be a single flare added at the midpoint of the polygon.

To prevent abrupt popping, the intensity of the flare is interpolated up and
down as it changes visibility.  This involves scene to scene state, unlike almost
all other aspects of the renderer, and is complicated by the fact that a single
frame may have multiple scenes.

RB_RenderFlares() will be called once per view (twice in a mirrored scene, potentially
up to five or more times in a frame with 3D status bar icons).

=============================================================================
*/


// the depth under a flare is read without waiting for the GPU
// (vk_queue_depth_reads), so each flare keeps the reads still in flight
#define FLARE_DEPTH_READS	4

typedef struct {
	unsigned int	id;				// vk_queue_depth_reads id
	float			eyeZ;			// eye depth of the flare when the read was queued
	float			proj10, proj11, proj14;	// projection terms of that view
} flareDepthRead_t;

// flare states maintain visibility over multiple frames for fading
// layers: view, mirror, menu
typedef struct flare_s {
	struct		flare_s	*next;		// for active chain

	int			addedFrame;

	qboolean	inPortal;				// true if in a portal view of the scene
	int			frameSceneNum;
	void		*surface;
	int			fogNum;

	int			fadeTime;

	qboolean	visible;			// state of last test
	float		drawIntensity;		// may be non 0 even if !visible due to fading

	int			windowX, windowY;
	float		eyeZ;

	vec3_t		color;

	int					numDepthReads;	// oldest first
	flareDepthRead_t	depthReads[FLARE_DEPTH_READS];
} flare_t;

#define		MAX_FLARES		128

flare_t		r_flareStructs[MAX_FLARES];
flare_t		*r_activeFlares, *r_inactiveFlares;

/*
==================
R_ClearFlares
==================
*/
void R_ClearFlares( void ) {
	int		i;

	Com_Memset( r_flareStructs, 0, sizeof( r_flareStructs ) );
	r_activeFlares = NULL;
	r_inactiveFlares = NULL;

	for ( i = 0 ; i < MAX_FLARES ; i++ ) {
		r_flareStructs[i].next = r_inactiveFlares;
		r_inactiveFlares = &r_flareStructs[i];
	}
}


/*
==================
RB_AddFlare

This is called at surface tesselation time
==================
*/
void RB_AddFlare( void *surface, int fogNum, vec3_t point, vec3_t color, vec3_t normal ) {
	int				i;
	flare_t			*f, *oldest;
	vec3_t			local;
	float			d;
	vec4_t			eye, clip, normalized, window;

	backEnd.pc.c_flareAdds++;

	// if the point is off the screen, don't bother adding it
	// calculate screen coordinates and depth
	R_TransformModelToClip( point, backEnd.ori.modelMatrix, 
		backEnd.viewParms.projectionMatrix, eye, clip );

	// check to see if the point is completely off screen
	for ( i = 0 ; i < 3 ; i++ ) {
		if ( clip[i] >= clip[3] || clip[i] <= -clip[3] ) {
			return;
		}
	}

	R_TransformClipToWindow( clip, &backEnd.viewParms, normalized, window );

	if ( window[0] < 0 || window[0] >= backEnd.viewParms.viewportWidth
		|| window[1] < 0 || window[1] >= backEnd.viewParms.viewportHeight ) {
		return;	// shouldn't happen, since we check the clip[] above, except for FP rounding
	}

	// see if a flare with a matching surface, scene, and view exists
	oldest = r_flareStructs;
	for ( f = r_activeFlares ; f ; f = f->next ) {
		if ( f->surface == surface && f->frameSceneNum == backEnd.viewParms.frameSceneNum
			&& f->inPortal == backEnd.viewParms.isPortal ) {
			break;
		}
	}

	// allocate a new one
	if (!f ) {
		if ( !r_inactiveFlares ) {
			// the list is completely full
			return;
		}
		f = r_inactiveFlares;
		r_inactiveFlares = r_inactiveFlares->next;
		f->next = r_activeFlares;
		r_activeFlares = f;

		f->surface = surface;
		f->frameSceneNum = backEnd.viewParms.frameSceneNum;
		f->inPortal = backEnd.viewParms.isPortal;
		f->addedFrame = -1;
	}

	if ( f->addedFrame != backEnd.viewParms.frameCount - 1 ) {
		f->visible = qfalse;
		f->fadeTime = backEnd.refdef.time - 2000;
		f->numDepthReads = 0;
	}

	f->addedFrame = backEnd.viewParms.frameCount;
	f->fogNum = fogNum;

	VectorCopy( color, f->color );

	// fade the intensity of the flare down as the
	// light surface turns away from the viewer
	if ( normal ) {
		VectorSubtract( backEnd.viewParms.ori.origin, point, local );
		VectorNormalizeFast( local );
		d = DotProduct( local, normal );
		VectorScale( f->color, d, f->color ); 
	}

	// save info needed to test
	f->windowX = backEnd.viewParms.viewportX + window[0];
	f->windowY = backEnd.viewParms.viewportY + window[1];

	f->eyeZ = eye[2];
}

/*
==================
RB_AddDlightFlares
==================
*/
void RB_AddDlightFlares( void ) {
	dlight_t		*l;
	int				i;

	if ( !r_flares->integer ) {
		return;
	}

	l = backEnd.refdef.dlights;
	for (i=0 ; i<backEnd.refdef.num_dlights ; i++, l++) {
		RB_AddFlare( (void *)l, 0, l->origin, l->color, NULL );
	}
}

/*
===============================================================================

FLARE BACK END

===============================================================================
*/

/*
==================
RB_TestFlare

GL1 read the depth under the flare with glReadPixels, which waits for the
GPU. The read is now queued without waiting (RB_RenderFlares) and its
result arrives a fixed number of frames later (2 with the Vulkan backend):
the test uses the newest completed read, with the eye depth and projection
of the frame that queued it. Until a read completes the flare keeps its
visibility, so it fades in and out like in GL1, a couple of frames later.
==================
*/
void RB_TestFlare( flare_t *f ) {
	float			depth;
	qboolean		visible;
	float			fade;
	float			screenZ;
	depthReadStatus_t	status;

	backEnd.pc.c_flareTests++;

	visible = f->visible;
	while ( f->numDepthReads ) {
		const flareDepthRead_t *r = &f->depthReads[0];

		status = vk_get_depth_read( r->id, &depth );
		if ( status == DEPTHREAD_PENDING ) {
			break;
		}
		if ( status == DEPTHREAD_DONE ) {
			// the full depth range starts at vk_depth_floor
			depth = Com_Clamp( 0.0f, 1.0f, ( depth - vk_depth_floor() ) / ( 1.0f - vk_depth_floor() ) );
			screenZ = r->proj14 / ( ( 2*depth - 1 ) * r->proj11 - r->proj10 );
			visible = ( -r->eyeZ - -screenZ ) < 24;
		}
		f->numDepthReads--;
		memmove( f->depthReads, f->depthReads + 1, f->numDepthReads * sizeof( f->depthReads[0] ) );
	}

	if ( visible ) {
		if ( !f->visible ) {
			f->visible = qtrue;
			f->fadeTime = backEnd.refdef.time - 1;
		}
		fade = ( ( backEnd.refdef.time - f->fadeTime ) /1000.0f ) * r_flareFade->value;
	} else {
		if ( f->visible ) {
			f->visible = qfalse;
			f->fadeTime = backEnd.refdef.time - 1;
		}
		fade = 1.0f - ( ( backEnd.refdef.time - f->fadeTime ) / 1000.0f ) * r_flareFade->value;
	}

	if ( fade < 0 ) {
		fade = 0;
	}
	if ( fade > 1 ) {
		fade = 1;
	}

	f->drawIntensity = fade;
}


/*
==================
RB_RenderFlare
==================
*/
void RB_RenderFlare( flare_t *f ) {
	float			size;
	vec3_t			color;
	int				iColor[3];

	backEnd.pc.c_flareRenders++;

	VectorScale( f->color, f->drawIntensity*tr.identityLight, color );
	iColor[0] = color[0] * 255;
	iColor[1] = color[1] * 255;
	iColor[2] = color[2] * 255;

	size = backEnd.viewParms.viewportWidth * ( r_flareSize->value/640.0f + 8 / -f->eyeZ );

	RB_BeginSurface( tr.flareShader );

	// FIXME: use quadstamp?
	tess.xyz[tess.numVertexes][0] = f->windowX - size;
	tess.xyz[tess.numVertexes][1] = f->windowY - size;
	tess.texCoords[tess.numVertexes][0][0] = 0;
	tess.texCoords[tess.numVertexes][0][1] = 0;
	tess.vertexColors[tess.numVertexes][0] = iColor[0];
	tess.vertexColors[tess.numVertexes][1] = iColor[1];
	tess.vertexColors[tess.numVertexes][2] = iColor[2];
	tess.vertexColors[tess.numVertexes][3] = 255;
	tess.numVertexes++;

	tess.xyz[tess.numVertexes][0] = f->windowX - size;
	tess.xyz[tess.numVertexes][1] = f->windowY + size;
	tess.texCoords[tess.numVertexes][0][0] = 0;
	tess.texCoords[tess.numVertexes][0][1] = 1;
	tess.vertexColors[tess.numVertexes][0] = iColor[0];
	tess.vertexColors[tess.numVertexes][1] = iColor[1];
	tess.vertexColors[tess.numVertexes][2] = iColor[2];
	tess.vertexColors[tess.numVertexes][3] = 255;
	tess.numVertexes++;

	tess.xyz[tess.numVertexes][0] = f->windowX + size;
	tess.xyz[tess.numVertexes][1] = f->windowY + size;
	tess.texCoords[tess.numVertexes][0][0] = 1;
	tess.texCoords[tess.numVertexes][0][1] = 1;
	tess.vertexColors[tess.numVertexes][0] = iColor[0];
	tess.vertexColors[tess.numVertexes][1] = iColor[1];
	tess.vertexColors[tess.numVertexes][2] = iColor[2];
	tess.vertexColors[tess.numVertexes][3] = 255;
	tess.numVertexes++;

	tess.xyz[tess.numVertexes][0] = f->windowX + size;
	tess.xyz[tess.numVertexes][1] = f->windowY - size;
	tess.texCoords[tess.numVertexes][0][0] = 1;
	tess.texCoords[tess.numVertexes][0][1] = 0;
	tess.vertexColors[tess.numVertexes][0] = iColor[0];
	tess.vertexColors[tess.numVertexes][1] = iColor[1];
	tess.vertexColors[tess.numVertexes][2] = iColor[2];
	tess.vertexColors[tess.numVertexes][3] = 255;
	tess.numVertexes++;

	tess.indexes[tess.numIndexes++] = 0;
	tess.indexes[tess.numIndexes++] = 1;
	tess.indexes[tess.numIndexes++] = 2;
	tess.indexes[tess.numIndexes++] = 0;
	tess.indexes[tess.numIndexes++] = 2;
	tess.indexes[tess.numIndexes++] = 3;

	RB_EndSurface();
}

/*
==================
RB_RenderFlares

Because flares are simulating an occular effect, they should be drawn after
everything (all views) in the entire frame has been drawn.

Because of the way portals use the depth buffer to mark off areas, the
needed information would be lost after each view, so we are forced to draw
flares after each view.

The resulting artifact is that flares in mirrors or portals don't dim properly
when occluded by something in the main view, and portal flares that should
extend past the portal edge will be overwritten.
==================
*/
void RB_RenderFlares (void) {
	flare_t		*f;
	flare_t		**prev;
	qboolean	draw;
	float		savedModelview[16];
	float		savedProjection[16];
	float		matrix[16];
	flare_t		*tested[MAX_FLARES];
	int			xy[MAX_FLARES * 2];
	unsigned int ids[MAX_FLARES];
	int			i, numTested;

	if ( !r_flares->integer ) {
		return;
	}

//	RB_AddDlightFlares();

	// test each flare in this view against the depth read earlier
	draw = qfalse;
	numTested = 0;
	prev = &r_activeFlares;
	while ( ( f = *prev ) != NULL ) {
		// throw out any flares that weren't added last frame
		if ( f->addedFrame < backEnd.viewParms.frameCount - 1 ) {
			*prev = f->next;
			f->next = r_inactiveFlares;
			r_inactiveFlares = f;
			continue;
		}

		// don't draw any here that aren't from this scene / portal
		f->drawIntensity = 0;
		if ( f->frameSceneNum == backEnd.viewParms.frameSceneNum
			&& f->inPortal == backEnd.viewParms.isPortal ) {
			RB_TestFlare( f );
			if ( f->drawIntensity ) {
				draw = qtrue;
			}
			// GL1 removed a flare that had faded out from the chain here;
			// it now stays while it is added, since its next depth read is
			// queued below (a new flare is invisible until a read completes)
			tested[numTested++] = f;
		}

		prev = &f->next;
	}

	// read the depth under each tested flare, for the tests of later frames
	for ( i = 0; i < numTested; i++ ) {
		xy[i * 2 + 0] = tested[i]->windowX;
		xy[i * 2 + 1] = tested[i]->windowY;
	}
	vk_queue_depth_reads( numTested, xy, ids );
	for ( i = 0; i < numTested; i++ ) {
		flareDepthRead_t *r;

		f = tested[i];
		if ( f->numDepthReads == FLARE_DEPTH_READS ) {
			f->numDepthReads--;
			memmove( f->depthReads, f->depthReads + 1, f->numDepthReads * sizeof( f->depthReads[0] ) );
		}
		r = &f->depthReads[f->numDepthReads++];
		r->id = ids[i];
		r->eyeZ = f->eyeZ;
		r->proj10 = backEnd.viewParms.projectionMatrix[10];
		r->proj11 = backEnd.viewParms.projectionMatrix[11];
		r->proj14 = backEnd.viewParms.projectionMatrix[14];
	}

	if ( !draw ) {
		return;		// none visible
	}

	if ( backEnd.viewParms.isPortal ) {
		vk_set_clip_plane( NULL );
	}

	Com_Memcpy( savedModelview, glState.modelview, sizeof( savedModelview ) );
	Com_Memcpy( savedProjection, glState.projection, sizeof( savedProjection ) );
	RB_LoadIdentity( matrix );
	RB_SetModelview( matrix );
	RB_OrthoMatrix( matrix, backEnd.viewParms.viewportX, backEnd.viewParms.viewportX + backEnd.viewParms.viewportWidth,
			  backEnd.viewParms.viewportY, backEnd.viewParms.viewportY + backEnd.viewParms.viewportHeight,
			  -99999, 99999 );
	RB_SetProjection( matrix );

	for ( f = r_activeFlares ; f ; f = f->next ) {
		if ( f->frameSceneNum == backEnd.viewParms.frameSceneNum
			&& f->inPortal == backEnd.viewParms.isPortal
			&& f->drawIntensity ) {
			RB_RenderFlare( f );
		}
	}

	RB_SetProjection( savedProjection );
	RB_SetModelview( savedModelview );
}

