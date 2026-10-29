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

// tr_temporal.c -- what the temporal path (r_temporal, vk_temporal.c)
// needs from the back end: the views of the scene and the last frame of
// the camera and of the entities
//
// Every 3D view of the world begins or continues the frame's scene
// (vk_begin_scene). The primary view (not a portal, not the sky portal)
// keeps its camera for the next frame; a view of the next frame then has a
// last frame when the camera didn't jump (r_temporalCutDist, the field of
// view). Portal views have none: no motion. The game time between the two
// frames, smoothed, sets the motion blur's exposure (vkSceneView_t.frameMsec).
//
// Entities: each frame the primary view keeps, per entity of the scene,
// its model matrix and, for skeletal models skinned on the GPU, where its
// bones and morph weights are in the frame data (tr_skin.c). An entity is
// known by its game entity number, its model and how many entities of the
// scene had both before it (attached models share the entity number); the
// next frame finds it there. Entities without a game entity
// (ENTITYNUM_NONE: temporary models, emitters) and those that jumped get
// the camera's motion only, as surfaces that don't move: the world, static
// models, sprites and polys.

#include "tr_local.h"

#define RB_TEMPORAL_TABLE		8192	// power of two, twice MAX_REFENTITIES

typedef struct {
	int			entityNumber;		// the key, with hModel and ordinal
	qhandle_t	hModel;
	int			ordinal;
	unsigned	serial;				// the frame that wrote it (vk_frame_serial), 0 for a free slot
	float		model[16];			// object to world
	vec3_t		origin;
	// the skin data of the frame (RB_TemporalRecordSkin), when skinSerial
	// is serial
	unsigned	skinSerial;
	int			numBones;
	uint32_t	skinBones, skinMorphs;
} rbTemporalEntity_t;

static struct {
	// the entities of the last two frames: tables[current] is this frame's
	rbTemporalEntity_t	tables[2][RB_TEMPORAL_TABLE];
	unsigned			tableSerial[2];
	int					current;

	// the primary view's camera of the frame cameraSerial
	unsigned			cameraSerial;
	float				view[16], projection[16];
	vec3_t				origin;
	float				fovX, fovY;
	int					time;				// backEnd.refdef.time

	// the game time between frames in milliseconds, smoothed over about 8
	// frames; 0 until there is one
	float				frameMsec;

	// this frame's primary view (frameSerial): whether it has a last
	// frame, then that frame's projection and view and the camera's motion
	unsigned			frameSerial;
	qboolean			history;
	float				prevProjection[16], prevView[16], cameraDelta[16];

	// per entity of the scene: its last frame and this frame's slot (or NULL)
	rbTemporalEntity_t	*prev[MAX_REFENTITIES];
	rbTemporalEntity_t	*cur[MAX_REFENTITIES];
} rbt;

static cvar_t	*r_temporalCutDist;

void R_TemporalInit( void ) {
	r_temporalCutDist = ri.Cvar_Get( "r_temporalCutDist", "256", 0 );
	ri.Cvar_SetDescription( r_temporalCutDist, "with r_temporal 1: a camera or entity that moves farther than this in a frame has no motion vectors (a cut, a teleport)" );
	// the backend's frame numbers start again
	Com_Memset( &rbt, 0, sizeof( rbt ) );
}

// a * b, column-major
static void R_TemporalMultiply( const float *a, const float *b, float *out ) {
	int c, r;

	for ( c = 0; c < 4; c++ ) {
		for ( r = 0; r < 4; r++ ) {
			out[c * 4 + r] = a[0 * 4 + r] * b[c * 4 + 0] + a[1 * 4 + r] * b[c * 4 + 1]
				+ a[2 * 4 + r] * b[c * 4 + 2] + a[3 * 4 + r] * b[c * 4 + 3];
		}
	}
}

// the inverse of a rotation and translation, column-major
static void R_TemporalInvertRigid( const float *m, float *out ) {
	int c, r;

	for ( c = 0; c < 3; c++ ) {
		for ( r = 0; r < 3; r++ ) {
			out[c * 4 + r] = m[r * 4 + c];
		}
		out[c * 4 + 3] = 0.0f;
	}
	for ( r = 0; r < 3; r++ ) {
		out[12 + r] = -( out[0 * 4 + r] * m[12] + out[1 * 4 + r] * m[13] + out[2 * 4 + r] * m[14] );
	}
	out[15] = 1.0f;
}

// an entity's object to world matrix (R_RotateForEntity without the view)
static void R_TemporalModelMatrix( const refEntity_t *e, float *m ) {
	int i;

	for ( i = 0; i < 3; i++ ) {
		m[i * 4 + 0] = e->axis[i][0];
		m[i * 4 + 1] = e->axis[i][1];
		m[i * 4 + 2] = e->axis[i][2];
		m[i * 4 + 3] = 0.0f;
	}
	m[12] = e->origin[0];
	m[13] = e->origin[1];
	m[14] = e->origin[2];
	m[15] = 1.0f;
}

static unsigned R_TemporalHash( int entityNumber, qhandle_t hModel, int ordinal ) {
	unsigned h = (unsigned)entityNumber * 2654435761u ^ (unsigned)hModel * 40503u ^ (unsigned)ordinal * 2246822519u;

	h ^= h >> 15;
	return h & ( RB_TEMPORAL_TABLE - 1 );
}

static rbTemporalEntity_t *R_TemporalFind( rbTemporalEntity_t *table, unsigned serial, int entityNumber, qhandle_t hModel, int ordinal ) {
	unsigned i, n;

	for ( i = R_TemporalHash( entityNumber, hModel, ordinal ), n = 0; n < RB_TEMPORAL_TABLE; i = ( i + 1 ) & ( RB_TEMPORAL_TABLE - 1 ), n++ ) {
		rbTemporalEntity_t *t = &table[i];

		if ( t->serial != serial ) {
			return NULL;
		}
		if ( t->entityNumber == entityNumber && t->hModel == hModel && t->ordinal == ordinal ) {
			return t;
		}
	}
	return NULL;
}

// a free slot of this frame's table for the key, NULL when it is full
static rbTemporalEntity_t *R_TemporalInsert( rbTemporalEntity_t *table, unsigned serial, int entityNumber, qhandle_t hModel, int ordinal ) {
	unsigned i, n;

	for ( i = R_TemporalHash( entityNumber, hModel, ordinal ), n = 0; n < RB_TEMPORAL_TABLE; i = ( i + 1 ) & ( RB_TEMPORAL_TABLE - 1 ), n++ ) {
		rbTemporalEntity_t *t = &table[i];

		if ( t->serial != serial ) {
			t->serial = serial;
			t->entityNumber = entityNumber;
			t->hModel = hModel;
			t->ordinal = ordinal;
			t->skinSerial = 0;
			return t;
		}
	}
	return NULL;
}

/*
===============
R_TemporalPrimary

The frame's primary view: its camera's last frame, and its entities' (this
frame's become the next one's)
===============
*/
static void R_TemporalPrimary( unsigned serial ) {
	const float *view = backEnd.viewParms.world.modelMatrix;
	rbTemporalEntity_t *table, *last;
	float invView[16];
	vec3_t delta;
	float cut, dt;
	int i, prevTable;

	cut = r_temporalCutDist->value;

	// the camera
	VectorSubtract( backEnd.viewParms.ori.origin, rbt.origin, delta );
	rbt.history = rbt.cameraSerial && rbt.cameraSerial == serial - 1 && VectorLength( delta ) <= cut
		&& fabs( backEnd.refdef.fov_x - rbt.fovX ) < 0.01f && fabs( backEnd.refdef.fov_y - rbt.fovY ) < 0.01f;
	if ( rbt.history ) {
		Com_Memcpy( rbt.prevProjection, rbt.projection, sizeof( rbt.prevProjection ) );
		Com_Memcpy( rbt.prevView, rbt.view, sizeof( rbt.prevView ) );
		R_TemporalInvertRigid( view, invView );
		R_TemporalMultiply( rbt.prevView, invView, rbt.cameraDelta );

		// the motion vectors cover dt; a paused game (dt 0) keeps the last
		// value, its motion is none anyway
		dt = (float)( backEnd.refdef.time - rbt.time );
		if ( dt > 0.0f ) {
			dt = Com_Clamp( 1.0f, 100.0f, dt );
			rbt.frameMsec = rbt.frameMsec > 0.0f ? rbt.frameMsec + ( dt - rbt.frameMsec ) * 0.125f : dt;
		}
	}
	rbt.time = backEnd.refdef.time;
	Com_Memcpy( rbt.view, view, sizeof( rbt.view ) );
	Com_Memcpy( rbt.projection, backEnd.viewParms.projectionMatrix, sizeof( rbt.projection ) );
	VectorCopy( backEnd.viewParms.ori.origin, rbt.origin );
	rbt.fovX = backEnd.refdef.fov_x;
	rbt.fovY = backEnd.refdef.fov_y;
	rbt.cameraSerial = serial;
	rbt.frameSerial = serial;

	// the entities: this frame's table is the one of two frames ago
	prevTable = rbt.current;
	rbt.current ^= 1;
	rbt.tableSerial[rbt.current] = serial;
	table = rbt.tables[rbt.current];
	last = rbt.tableSerial[prevTable] == serial - 1 ? rbt.tables[prevTable] : NULL;

	for ( i = 0; i < backEnd.refdef.num_entities && i < MAX_REFENTITIES; i++ ) {
		const refEntity_t *e = &backEnd.refdef.entities[i].e;
		rbTemporalEntity_t *t, *p;
		int ordinal;

		rbt.prev[i] = rbt.cur[i] = NULL;
		if ( e->reType != RT_MODEL || e->entityNumber == ENTITYNUM_NONE ) {
			continue;
		}
		for ( ordinal = 0; R_TemporalFind( table, serial, e->entityNumber, e->hModel, ordinal ); ordinal++ ) {
		}
		t = R_TemporalInsert( table, serial, e->entityNumber, e->hModel, ordinal );
		if ( !t ) {
			continue;
		}
		R_TemporalModelMatrix( e, t->model );
		VectorCopy( e->origin, t->origin );
		rbt.cur[i] = t;

		p = last ? R_TemporalFind( last, serial - 1, e->entityNumber, e->hModel, ordinal ) : NULL;
		if ( p ) {
			VectorSubtract( e->origin, p->origin, delta );
			if ( VectorLength( delta ) <= cut ) {
				rbt.prev[i] = p;
			}
		}
	}
}

/*
===============
RB_TemporalBeginView

The back end starts drawing a view (RB_DrawSurfs, RB_SpriteSurfs): a view
of the world begins or continues the scene, another one ends it
===============
*/
void RB_TemporalBeginView( void ) {
	vkSceneView_t view;
	unsigned serial;
	qboolean primary;

	backEnd.temporalScene = qfalse;
	backEnd.temporalPrimary = qfalse;
	if ( !vk_temporal_enabled() ) {
		return;
	}
	if ( backEnd.refdef.rdflags & ( RDF_NOWORLDMODEL | RDF_HYPERSPACE ) ) {
		vk_end_scene();
		return;
	}

	serial = vk_frame_serial();
	primary = !backEnd.viewParms.isPortal && !backEnd.viewParms.isPortalSky;
	if ( primary && rbt.frameSerial != serial ) {
		R_TemporalPrimary( serial );
	}

	Com_Memset( &view, 0, sizeof( view ) );
	view.viewport[0] = backEnd.viewParms.viewportX;
	view.viewport[1] = backEnd.viewParms.viewportY;
	view.viewport[2] = backEnd.viewParms.viewportWidth;
	view.viewport[3] = backEnd.viewParms.viewportHeight;
	view.projection = backEnd.viewParms.projectionMatrix;
	view.primary = primary;
	if ( primary && rbt.history ) {
		view.prevProjection = rbt.prevProjection;
		view.cameraDelta = rbt.cameraDelta;
		view.frameMsec = rbt.frameMsec;
	}
	backEnd.temporalScene = vk_begin_scene( &view );
	backEnd.temporalPrimary = backEnd.temporalScene && primary;
}

/*
===============
RB_TemporalEndScene

Before what isn't a 3D view of the world is drawn (Set2DWindow, the end of
the frame)
===============
*/
void RB_TemporalEndScene( void ) {
	backEnd.temporalScene = qfalse;
	backEnd.temporalPrimary = qfalse;
	vk_end_scene();
}

/*
===============
RB_TemporalAo

In the primary view, before its first blended surface (RB_RenderDrawSurfList):
the ambient occlusion (r_ao) of what is drawn so far, once a scene, with the
view's fog (RB_SetupFog), which it is applied under
===============
*/
void RB_TemporalAo( void ) {
	float fog[5];

	if ( !backEnd.temporalPrimary || !vk_ao_pending() ) {
		return;
	}
	if ( glState.externalSetState & GLS_FOG ) {
		fog[0] = glState.fogStart;
		fog[1] = glState.fogEnd;
		fog[2] = glState.fFogColor[0];
		fog[3] = glState.fFogColor[1];
		fog[4] = glState.fFogColor[2];
		vk_ao_scene( fog );
	} else {
		vk_ao_scene( NULL );
	}
}

static rbTemporalEntity_t *RB_TemporalCurrentEntity( rbTemporalEntity_t **list ) {
	int index;

	if ( !backEnd.temporalPrimary || !backEnd.currentEntity || backEnd.currentEntity == &tr.worldEntity || backEnd.currentStaticModel ) {
		return NULL;
	}
	index = backEnd.currentEntity - backEnd.refdef.entities;
	if ( index < 0 || index >= backEnd.refdef.num_entities || index >= MAX_REFENTITIES ) {
		return NULL;
	}
	return list[index];
}

/*
===============
RB_TemporalModelview

After the modelview of an entity, a static model or the world is set
(RB_RenderDrawSurfList): the last frame's of an entity that has one
===============
*/
void RB_TemporalModelview( void ) {
	const rbTemporalEntity_t *p;
	float prevModelview[16];

	if ( !backEnd.temporalScene ) {
		return;
	}
	p = rbt.history ? RB_TemporalCurrentEntity( rbt.prev ) : NULL;
	if ( !p ) {
		vk_set_prev_modelview( NULL );
		return;
	}
	R_TemporalMultiply( rbt.prevView, p->model, prevModelview );
	vk_set_prev_modelview( prevModelview );
}

/*
===============
RB_TemporalSkin

Where the current entity's bones and morph weights of the last frame are in
the last frame's frame data (VK_SKIN_PREV_WORDS), when it was skinned with
as many bones; VK_SKIN_NONE otherwise
===============
*/
void RB_TemporalSkin( int numBones, qboolean hasMorph, uint32_t *prevBones, uint32_t *prevMorphs ) {
	const rbTemporalEntity_t *p = RB_TemporalCurrentEntity( rbt.prev );

	*prevBones = *prevMorphs = VK_SKIN_NONE;
	if ( !p || !rbt.history || p->skinSerial != p->serial || p->numBones != numBones ) {
		return;
	}
	*prevBones = p->skinBones;
	*prevMorphs = hasMorph ? p->skinMorphs : VK_SKIN_NONE;
}

// the current entity's bones and morph weights in this frame's frame data,
// for the next frame (primary view only)
void RB_TemporalRecordSkin( int numBones, uint32_t bones, uint32_t morphs ) {
	rbTemporalEntity_t *t = RB_TemporalCurrentEntity( rbt.cur );

	if ( !t ) {
		return;
	}
	t->skinSerial = t->serial;
	t->numBones = numBones;
	t->skinBones = bones;
	t->skinMorphs = morphs;
}
