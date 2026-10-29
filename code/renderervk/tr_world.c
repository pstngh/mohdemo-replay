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
#include "tr_local.h"



/*
=================
R_CullTriSurf

Returns true if the grid is completely culled away.
Also sets the clipped hint bit in tess
=================
*/
static qboolean	R_CullTriSurf( srfTriangles_t *cv ) {
	int 	boxCull;

	boxCull = R_CullLocalBox( cv->bounds );

	if ( boxCull == CULL_OUT ) {
		return qtrue;
	}
	return qfalse;
}

/*
=================
R_CullGrid

Returns true if the grid is completely culled away.
Also sets the clipped hint bit in tess
=================
*/
static qboolean	R_CullGrid( srfGridMesh_t *cv ) {
	int 	boxCull;
	int 	sphereCull;

	if ( r_nocurves->integer ) {
		return qtrue;
	}

	if (!tr.refdef.render_terrain) {
		return qtrue;
	}

	if ( tr.currentEntityNum != ENTITYNUM_WORLD ) {
		sphereCull = R_CullLocalPointAndRadius( cv->localOrigin, cv->meshRadius );
	} else {
		sphereCull = R_CullPointAndRadius( cv->localOrigin, cv->meshRadius );
	}
	boxCull = CULL_OUT;
	
	// check for trivial reject
	if ( sphereCull == CULL_OUT )
	{
		tr.pc.c_sphere_cull_patch_out++;
		return qtrue;
	}
	// check bounding box if necessary
	else if ( sphereCull == CULL_CLIP )
	{
		tr.pc.c_sphere_cull_patch_clip++;

		boxCull = R_CullLocalBox( cv->meshBounds );

		if ( boxCull == CULL_OUT ) 
		{
			tr.pc.c_box_cull_patch_out++;
			return qtrue;
		}
		else if ( boxCull == CULL_IN )
		{
			tr.pc.c_box_cull_patch_in++;
		}
		else
		{
			tr.pc.c_box_cull_patch_clip++;
		}
	}
	else
	{
		tr.pc.c_sphere_cull_patch_in++;
	}

	return qfalse;
}


/*
================
R_CullSurface

Tries to back face cull surfaces before they are lighted or
added to the sorting list.

This will also allow mirrors on both sides of a model without recursion.
================
*/
static qboolean	R_CullSurface( surfaceType_t *surface, shader_t *shader ) {
	srfSurfaceFace_t *sface;
	float			d;

	if ( r_nocull->integer ) {
		return qfalse;
	}

	if ( *surface == SF_GRID ) {
		return R_CullGrid( (srfGridMesh_t *)surface );
	}

	if ( *surface == SF_TRIANGLES ) {
		return R_CullTriSurf( (srfTriangles_t *)surface );
	}

	if ( *surface != SF_FACE ) {
		return qfalse;
	}

	if ( shader->cullType == CT_TWO_SIDED ) {
		return qfalse;
	}

	// face culling
	if ( !r_facePlaneCull->integer ) {
		return qfalse;
	}

	sface = ( srfSurfaceFace_t * ) surface;
	d = DotProduct (tr.ori.viewOrigin, sface->plane.normal);

	// don't cull exactly on the plane, because there are levels of rounding
	// through the BSP, ICD, and hardware that may cause pixel gaps if an
	// epsilon isn't allowed here 
	if ( shader->cullType == CT_FRONT_SIDED ) {
		if ( d < sface->plane.dist - 8 ) {
			return qtrue;
		}
	} else {
		if ( d > sface->plane.dist + 8 ) {
			return qtrue;
		}
	}

	return qfalse;
}

/*
======================
R_FastDlightFace
======================
*/
static dlightMask_t R_FastDlightFace( srfSurfaceFace_t *face, dlightMask_t dlightBits ) {
	float		d;
	int			i;
	dlight_t	*dl;

	for ( i = 0 ; i < tr.refdef.num_dlights ; i++ ) {
		if ( ! ( dlightBits & DLIGHT_BIT( i ) ) ) {
			continue;
		}
		dl = &tr.refdef.dlights[i];
		d = DotProduct( dl->transformed, face->plane.normal ) - face->plane.dist;
		if ( d < -dl->radius || d > dl->radius ) {
			// dlight doesn't reach the plane
			dlightBits &= ~DLIGHT_BIT( i );
		}
	}

	if ( !dlightBits ) {
		tr.pc.c_dlightSurfacesCulled++;
	}

	face->dlightBits = dlightBits;
	return dlightBits;
}

/*
======================
R_FastDlightGrid
======================
*/
static dlightMask_t R_FastDlightGrid( srfGridMesh_t *grid, dlightMask_t dlightBits ) {
	int			i;
	dlight_t	*dl;

	for ( i = 0 ; i < tr.refdef.num_dlights ; i++ ) {
		if ( ! ( dlightBits & DLIGHT_BIT( i ) ) ) {
			continue;
		}
		dl = &tr.refdef.dlights[i];
		if ( dl->origin[0] - dl->radius > grid->meshBounds[1][0]
			|| dl->origin[0] + dl->radius < grid->meshBounds[0][0]
			|| dl->origin[1] - dl->radius > grid->meshBounds[1][1]
			|| dl->origin[1] + dl->radius < grid->meshBounds[0][1]
			|| dl->origin[2] - dl->radius > grid->meshBounds[1][2]
			|| dl->origin[2] + dl->radius < grid->meshBounds[0][2] ) {
			// dlight doesn't reach the bounds
			dlightBits &= ~DLIGHT_BIT( i );
		}
	}

	if ( !dlightBits ) {
		tr.pc.c_dlightSurfacesCulled++;
	}

	grid->dlightBits = dlightBits;
	return dlightBits;
}

/*
======================
R_FastDlightTerrain
======================
*/
static dlightMask_t R_FastDlightTerrain(cTerraPatchUnpacked_t* srf, dlightMask_t dlightBits) {
    dlight_t* dl;
    float* origin;
    int i;

    for (i = 0; i < tr.refdef.num_dlights; i++) {
        dl = &tr.refdef.dlights[i];
        origin = dl->transformed;

        if (srf->x0 - origin[0] > dl->radius
            || srf->x0 - origin[0] < -512.0 - dl->radius
            || srf->y0 - origin[1] > dl->radius
            || srf->y0 - origin[1] < -512.0 - dl->radius
            || srf->z0 - origin[2] > dl->radius
            || srf->z0 - origin[2] < -srf->zmax - dl->radius
            ) {
            // dlight doesn't reach the bounds
            dlightBits &= ~DLIGHT_BIT(i);
        }
    }

    if (!dlightBits) {
        tr.pc.c_dlightSurfacesCulled++;
    }

    srf->drawinfo.dlightBits = dlightBits;
    return dlightBits;
}

/*
======================
R_DlightTrisurf
======================
*/
static dlightMask_t R_DlightTrisurf( srfTriangles_t *surf, dlightMask_t dlightBits ) {
	// FIXME: more dlight culling to trisurfs...
	surf->dlightBits = dlightBits;
	return dlightBits;
#if 0
	int			i;
	dlight_t	*dl;

	for ( i = 0 ; i < tr.refdef.num_dlights ; i++ ) {
		if ( ! ( dlightBits & DLIGHT_BIT( i ) ) ) {
			continue;
		}
		dl = &tr.refdef.dlights[i];
		if ( dl->origin[0] - dl->radius > grid->meshBounds[1][0]
			|| dl->origin[0] + dl->radius < grid->meshBounds[0][0]
			|| dl->origin[1] - dl->radius > grid->meshBounds[1][1]
			|| dl->origin[1] + dl->radius < grid->meshBounds[0][1]
			|| dl->origin[2] - dl->radius > grid->meshBounds[1][2]
			|| dl->origin[2] + dl->radius < grid->meshBounds[0][2] ) {
			// dlight doesn't reach the bounds
			dlightBits &= ~DLIGHT_BIT( i );
		}
	}

	if ( !dlightBits ) {
		tr.pc.c_dlightSurfacesCulled++;
	}

	grid->dlightBits = dlightBits;
	return dlightBits;
#endif
}

/*
====================
R_DlightSurface

The given surface is going to be drawn, and it touches a leaf
that is touched by one or more dlights, so try to throw out
more dlights if possible.
====================
*/
static dlightMask_t R_DlightSurface( msurface_t *surf, dlightMask_t dlightBits ) {
	dlightMask_t dlightMap;

	dlightMap = 0;

	if ( *surf->data == SF_FACE ) {
		if (!r_fastdlights->integer) {
			if (surf->shader->lightmapIndex >= 0) {
				dlightMap = R_RealDlightFace((srfSurfaceFace_t*)surf->data, dlightBits);
			} else {
				((srfSurfaceFace_t*)surf->data)->dlightMap = 0;
				dlightMap = 0;
			}
			return dlightMap;
        } else {
			dlightBits = R_FastDlightFace((srfSurfaceFace_t*)surf->data, dlightBits);
		}
	} else if ( *surf->data == SF_GRID ) {
		if (!r_fastdlights->integer) {
			if (surf->shader->lightmapIndex >= 0) {
				dlightMap = R_RealDlightPatch((srfGridMesh_t*)surf->data, dlightBits);
			} else {
				((srfGridMesh_t*)surf->data)->dlightMap = 0;
				dlightMap = 0;
            }
            return dlightMap;
        } else {
            dlightBits = R_FastDlightGrid((srfGridMesh_t*)surf->data, dlightBits);
		}
	} else {
		return dlightMap;
	}

	if (dlightBits) {
		tr.pc.c_dlightSurfaces++;
		return 1;
	}

	return dlightMap;
}

/*
======================
R_DlightTerrain
======================
*/
dlightMask_t R_DlightTerrain(cTerraPatchUnpacked_t* surf, dlightMask_t dlightBits)
{
	dlightMask_t dlightMap;

    dlightMap = 0;

    if (r_fastdlights->integer)
    {
        if (R_FastDlightTerrain(surf, dlightBits)) {
            ++tr.pc.c_dlightSurfaces;
			dlightMap = 1;
        }
    } else if (surf->shader->lightmapIndex < 0) {
        surf->drawinfo.dlightMap = 0;
    } else {
		dlightMap = R_RealDlightTerrain(surf, dlightBits);
    }

    return dlightMap;
}

/*
======================
R_CheckDlightSurface
======================
*/
static dlightMask_t R_CheckDlightSurface(msurface_t* surf, dlightMask_t dlightBits)
{
    if (dlightBits && surf->frameCount != tr.frameCount)
    {
        surf->frameCount = tr.frameCount;
        return R_DlightSurface(surf, dlightBits);
    }

	if (dlightBits)
	{
		//
		// Added in 2.0
		//  Return the existing dlight map
		if (*surf->data == SF_FACE)
		{
			return ((srfSurfaceFace_t*)surf->data)->dlightMap;
		}
		else if (*surf->data == SF_GRID)
		{
			return ((srfGridMesh_t*)surf->data)->dlightMap;
		}
	}
	else
	{
		if (*surf->data == SF_FACE)
		{
			((srfSurfaceFace_t*)surf->data)->dlightMap = 0;
			((srfSurfaceFace_t*)surf->data)->dlightBits = 0;
		}
		else if (*surf->data == SF_GRID)
		{
			((srfGridMesh_t*)surf->data)->dlightMap = 0;
			((srfGridMesh_t*)surf->data)->dlightBits = 0;
		}
	}

    return 0;
}

/*
======================
R_CheckDlightTerrain
======================
*/
dlightMask_t R_CheckDlightTerrain(cTerraPatchUnpacked_t* surf, dlightMask_t dlightBits)
{
	if (surf->frameCount == tr.frameCount) {
		return surf->drawinfo.dlightMap;
	}

	surf->frameCount = tr.frameCount;
	if (dlightBits) {
		return R_DlightTerrain(surf, dlightBits);
	}

	surf->drawinfo.dlightMap = 0;
	surf->drawinfo.dlightBits = 0;
	return 0;
}

/*
=================
R_CullFaceToView

Whether a world face lies wholly behind one of the view's side planes, by
more than a unit: the GPU clips all of it away and it draws no pixel. GL1
culls faces by their plane only (R_CullSurface) and leaves the others to
the GPU's clipping. planeBits are the frustum planes its leaf isn't wholly
in front of (the others can't cull a face of the leaf), none where faces
outside the view still change what is drawn (R_FaceViewPlanes,
R_AddWorldLeaf). Only for the faces of msurface_t::viewCull (ParseFace):
not those whose vertexes move (deforms), the sky (its polygons give the
extents of the sky box, RB_ClipSkyPolygons), portals and the sky portal
(they start views of their own). The bounds are those of the face's points
rounded outwards
=================
*/
static qboolean R_CullFaceToView( const msurface_t *surf, int planeBits ) {
	int i;

	for ( i = 0; i < 4; i++ ) {
		const cplane_t *frust = &tr.viewParms.frustum[i];
		vec3_t corner;

		if ( !( planeBits & ( 1 << i ) ) ) {
			continue;
		}

		// the corner of the bounds farthest in front of the plane
		corner[0] = surf->bounds[( frust->signbits & 1 ) ? 0 : 1][0];
		corner[1] = surf->bounds[( frust->signbits & 2 ) ? 0 : 1][1];
		corner[2] = surf->bounds[( frust->signbits & 4 ) ? 0 : 1][2];
		if ( DotProduct( corner, frust->normal ) - frust->dist < -1.0f ) {
			return qtrue;
		}
	}

	return qfalse;
}

/*
=================
R_CullWorldFace

R_CullSurface for a face, on the copy of its plane in the msurface_t, then
against the view's frustum (R_CullFaceToView, planeBits 0 for brush models:
none)
=================
*/
static qboolean R_CullWorldFace( const msurface_t *surf, int planeBits ) {
	const shader_t *shader = surf->shader;

	if ( r_nocull->integer ) {
		return qfalse;
	}

	// GL1's face culling: don't cull exactly on the plane, because there are
	// levels of rounding through the BSP, ICD, and hardware that may cause
	// pixel gaps if an epsilon isn't allowed here
	if ( shader->cullType != CT_TWO_SIDED && r_facePlaneCull->integer ) {
		float d = DotProduct( tr.ori.viewOrigin, surf->plane );

		if ( shader->cullType == CT_FRONT_SIDED ) {
			if ( d < surf->plane[3] - 8 ) {
				return qtrue;
			}
		} else {
			if ( d > surf->plane[3] + 8 ) {
				return qtrue;
			}
		}
	}

	return ( planeBits & 15 ) && surf->viewCull && R_CullFaceToView( surf, planeBits );
}

/*
=================
R_FaceViewPlanes

The frustum planes against which the faces of the world may be culled in
this view (R_CullFaceToView): none with r_shownormals, which draws
tess.normal, which faces only write when their shader needs normals, so
that the others show what earlier batches left there (R_AddWorldLeaf also
keeps the faces of the leaves dynamic lights reach)
=================
*/
static int R_FaceViewPlanes( void ) {
	return r_shownormals->integer ? 0 : 15;
}

/*
======================
R_AddWorldSurface

planeBits: the frustum planes the world's leaf isn't wholly in front of, 0
for brush models
======================
*/
static void R_AddWorldSurface( msurface_t *surf, dlightMask_t dlightBits, int planeBits ) {
	//if ( surf->viewCount == tr.viewCount ) {
	//	return;		// already in this view
	//}

	surf->viewCount = tr.viewCount;
	// FIXME: bmodel fog?

	// try to cull before dlighting or adding
	if ( surf->isFace ) {
		if ( R_CullWorldFace( surf, planeBits ) ) {
			return;
		}
	} else if ( R_CullSurface( surf->data, surf->shader ) ) {
		return;
	}

	// check for dlighting
	dlightBits = R_CheckDlightSurface(surf, dlightBits);
	if (surf->shader && surf->shader->isPortalSky) {
		// Sky portal
		R_Sky_AddSurf(surf);
		return;
	}

	R_AddDrawSurf( surf->data, surf->shader, R_DlightSortKey( dlightBits ) );
}

/*
=============================================================

	BRUSH MODELS

=============================================================
*/

/*
=================
R_AddBrushModelSurfaces

The surfaces get the lights that reach the model (R_DlightBmodel) as the
world's get those of their leaves: per pixel on the lightmap with
r_fastdlights 0, ProjectDlightTexture with 1. GL1 passed needDlights (1)
as the lights to test, so only light 0 lit their lightmaps (or was
projected with r_fastdlights 1), and with r_fastdlights 0 the surfaces it
didn't reach got ProjectDlightTexture from all the model's lights
=================
*/
void R_AddBrushModelSurfaces ( trRefEntity_t *ent ) {
	bmodel_t	*bmodel;
	int			clip;
	model_t		*pModel;
	int			i;
	dlightMask_t	dlightBits;

	pModel = R_GetModelByHandle( ent->e.hModel );

	bmodel = pModel->d.bmodel;

	clip = R_CullLocalBox( bmodel->bounds );
	if ( clip == CULL_OUT ) {
		return;
	}
	
	dlightBits = R_DlightBmodel( bmodel );

	for ( i = 0 ; i < bmodel->numSurfaces ; i++ ) {
		R_AddWorldSurface( bmodel->firstSurface + i, dlightBits, 0 );
	}
}


/*
=============================================================

	WORLD MODEL

=============================================================
*/

void R_GetInlineModelBounds(int iIndex, vec3_t vMins, vec3_t vMaxs)
{
	bmodel_t* bmodel;

	bmodel = &tr.world->bmodels[iIndex];
	VectorCopy(bmodel->bounds[0], vMins);
	VectorCopy(bmodel->bounds[1], vMaxs);
}


/*
=============================================================

	WORLD LEAVES

GL1 marked the leaves in the PVS and their ancestors (R_MarkLeaves, a pass
over all the nodes when the view's cluster changed), then its walk of the
world's BSP tree (R_RecursiveWorldNode) went down from the root through the
marked nodes, testing each node's box against the frustum planes its parent
wasn't wholly in front of, and added the surfaces of the leaves it reached,
children[0] first. Here R_MarkLeaves lists the leaves in the PVS in that
order from a compact copy of what it tests (R_SetupWorldLeaves), and each
view tests the leaves on their own boxes, after a test of the box of up to
WORLD_LEAF_GROUP consecutive ones, without visiting the nodes: the same
leaves in the same order with the same planes left, because a leaf's box
lies within the boxes of all its ancestors (the BSP files store whole units
rounded outwards), so a node GL1 culled or found wholly in front of a
plane, the leaf is too, and the same for a group's box. The few leaves
whose box sticks out of an ancestor's (by a unit) are tested along their
path from the root like GL1 did (R_CullLeafAlongPath).

=============================================================
*/

#define WORLD_LEAF_GROUP	8

static void R_GroupVisibleLeaves( const byte *vis );

/*
================
R_CollectWalkLeaves
================
*/
static void R_CollectWalkLeaves( mnode_t *node ) {
	while ( node->contents == CONTENTS_NODE ) {
		R_CollectWalkLeaves( node->children[0] );
		node = node->children[1];
	}
	tr.world->walkLeaves[tr.world->numWalkLeaves++] = node;
}

/*
================
R_SetupWorldLeaves

The leaves the BSP walk reaches, in its order (the brush models' leaves
aren't reached), with their boxes, what R_MarkLeaves tests and whether each
one's box lies within its ancestors' boxes. Called once the world is loaded
================
*/
void R_SetupWorldLeaves( void ) {
	world_t *w = tr.world;
	int numLeaves, maxDepth, i, j;

	w->numWalkLeaves = 0;
	w->numVisLeaves = 0;
	w->numLeafGroups = 0;
	if ( !w->nodes || !w->numnodes ) {
		return;
	}

	numLeaves = w->numnodes - w->numDecisionNodes;
	w->walkLeaves = ri.Hunk_Alloc( numLeaves * sizeof( *w->walkLeaves ), h_dontcare );
	w->walkLeafInfo = ri.Hunk_Alloc( numLeaves * sizeof( *w->walkLeafInfo ), h_dontcare );
	w->walkLeafBounds = ri.Hunk_Alloc( numLeaves * sizeof( *w->walkLeafBounds ), h_dontcare );
	w->visLeaves = ri.Hunk_Alloc( numLeaves * sizeof( *w->visLeaves ), h_dontcare );
	w->leafGroups = ri.Hunk_Alloc( numLeaves * sizeof( *w->leafGroups ), h_dontcare );
	R_CollectWalkLeaves( w->nodes );

	maxDepth = 0;
	for ( i = 0; i < w->numWalkLeaves; i++ ) {
		mnode_t *leaf = w->walkLeaves[i], *node;
		worldWalkLeaf_t *info = &w->walkLeafInfo[i];
		int depth = 1;

		info->cluster = leaf->cluster;
		info->area = leaf->area;
		VectorCopy( leaf->mins, w->walkLeafBounds[i][0] );
		VectorCopy( leaf->maxs, w->walkLeafBounds[i][1] );
		info->inAncestors = qtrue;
		for ( node = leaf->parent; node; node = node->parent, depth++ ) {
			for ( j = 0; j < 3; j++ ) {
				if ( leaf->mins[j] < node->mins[j] || leaf->maxs[j] > node->maxs[j] ) {
					info->inAncestors = qfalse;
				}
			}
		}
		if ( depth > maxDepth ) {
			maxDepth = depth;
		}
	}
	w->leafPath = ri.Hunk_Alloc( maxDepth * sizeof( *w->leafPath ), h_dontcare );

	if ( !tr.visCount ) {
		// GL1's nodes, loaded with visframe 0, all counted as marked
		// until R_MarkLeaves marked a cluster (a first view without one)
		R_GroupVisibleLeaves( NULL );
	}
}

/*
================
R_GroupVisibleLeaves

The leaves in the cluster's PVS (vis; NULL: every leaf that isn't solid,
r_novis or no cluster) and not cut off by the area mask, GL1's test of
R_MarkLeaves, in the BSP walk's order, in groups of up to WORLD_LEAF_GROUP
consecutive ones with the box of their boxes; a leaf not within its
ancestors' boxes gets a group of its own without leaves (numLeaves 0)
================
*/
static void R_GroupVisibleLeaves( const byte *vis ) {
	world_t *w = tr.world;
	worldLeafGroup_t *g = NULL;
	int i;

	w->numVisLeaves = 0;
	w->numLeafGroups = 0;
	for ( i = 0; i < w->numWalkLeaves; i++ ) {
		const worldWalkLeaf_t *info = &w->walkLeafInfo[i];
		const vec3_t *bounds;

		if ( vis ) {
			int cluster = info->cluster;

			if ( cluster < 0 || cluster >= w->numClusters ) {
				continue;
			}

			// check general pvs
			if ( !( vis[cluster >> 3] & ( 1 << ( cluster & 7 ) ) ) ) {
				continue;
			}

			// check for door connection
			if ( tr.refdef.areamask[info->area >> 3] & ( 1 << ( info->area & 7 ) ) ) {
				continue;	// not visible
			}
		} else if ( w->walkLeaves[i]->contents == CONTENTS_SOLID ) {
			continue;
		}

		if ( !info->inAncestors ) {
			worldLeafGroup_t *alone = &w->leafGroups[w->numLeafGroups++];

			alone->first = w->numVisLeaves;
			alone->numLeaves = 0;
			w->visLeaves[w->numVisLeaves++] = i;
			g = NULL;
			continue;
		}

		bounds = w->walkLeafBounds[i];
		if ( !g || g->numLeaves == WORLD_LEAF_GROUP ) {
			g = &w->leafGroups[w->numLeafGroups++];
			g->first = w->numVisLeaves;
			g->numLeaves = 0;
			VectorCopy( bounds[0], g->mins );
			VectorCopy( bounds[1], g->maxs );
		} else {
			AddPointToBounds( bounds[0], g->mins, g->maxs );
			AddPointToBounds( bounds[1], g->mins, g->maxs );
		}
		w->visLeaves[w->numVisLeaves++] = i;
		g->numLeaves++;
	}
}

/*
================
R_CullBoxToFrustum

GL1's test of a node's box (R_RecursiveWorldNode): qtrue when the box is
wholly behind one of the frustum planes of *planeBits; the planes it is
wholly in front of are taken out of *planeBits. The far plane (16) only
culls where GL1 let it
================
*/
static qboolean R_CullBoxToFrustum( const vec3_t mins, const vec3_t maxs, int *planeBits ) {
	const float *box[2];
	int i;

	box[0] = mins;
	box[1] = maxs;
	for ( i = 0; i < 5; i++ ) {
		const cplane_t *p = &tr.viewParms.frustum[i];
		int s = p->signbits;
		float dist1, dist2;

		if ( !( *planeBits & ( 1 << i ) ) ) {
			continue;
		}

		// Changed in 2.0
		//  Allow culling only if the view is a portal sky
		if ( i == 4 && !( tr.viewParms.farplane_cull < FARPLANE_CULL_PORTALSKY || tr.viewParms.isPortalSky ) ) {
			continue;
		}

		if ( p->type < 3 ) {
			int r = BoxOnPlaneSide( mins, maxs, (cplane_t *)p );

			if ( r == 2 ) {
				return qtrue;	// culled
			}
			if ( r == 1 ) {
				*planeBits &= ~( 1 << i );	// all descendants will also be in front
			}
			continue;
		}

		// BoxOnPlaneSide's general case inline, the same products in the
		// same order: dist1 from the corner farthest in front of the plane,
		// dist2 from the one farthest behind it (dist2 <= dist1)
		dist1 = p->normal[0] * box[!( s & 1 )][0] + p->normal[1] * box[!( s & 2 )][1] + p->normal[2] * box[!( s & 4 )][2];
		if ( dist1 < p->dist ) {
			return qtrue;	// culled
		}
		dist2 = p->normal[0] * box[( s & 1 ) != 0][0] + p->normal[1] * box[( s & 2 ) != 0][1] + p->normal[2] * box[( s & 4 ) != 0][2];
		if ( dist2 >= p->dist ) {
			*planeBits &= ~( 1 << i );	// all descendants will also be in front
		}
	}

	return qfalse;
}

/*
================
R_CullLeafAlongPath

GL1's tests of a leaf and its ancestors, from the root down
================
*/
static qboolean R_CullLeafAlongPath( mnode_t *leaf, int *planeBits ) {
	mnode_t **path = tr.world->leafPath;
	int n = 0;

	for ( ; leaf; leaf = leaf->parent ) {
		path[n++] = leaf;
	}
	while ( n-- ) {
		if ( R_CullBoxToFrustum( path[n]->mins, path[n]->maxs, planeBits ) ) {
			return qtrue;
		}
	}

	return qfalse;
}

/*
================
R_AddWorldLeaf

Adds what a leaf holds that is potentially visible, the leaf body of GL1's
R_RecursiveWorldNode; facePlanes: R_FaceViewPlanes. The dynamic lights of dlightBits that reach nothing in
it are left out first (GL1 gave all of them): its ancestors' bounds of what
lights can reach contain its own, so the leaf's test alone leaves the same
lights as the tests of every node down to it did
================
*/
static void R_AddWorldLeaf( mnode_t *node, int planeBits, dlightMask_t dlightBits, int facePlanes ) {
	int			c;
	msurface_t	*surf, **mark;

	if ( dlightBits && !r_fastdlights->integer ) {
		dlightBits = R_CullDlightsToNode( node, dlightBits );
	}

	// no face is culled against the view where dynamic lights reach: the
	// lights of a batch are those of all its surfaces, so a face outside the
	// view changes those of the faces and grids batched with it
	// (ProjectDlightTexture with r_fastdlights 1, where every leaf gets
	// every light, and the side of the triangles a light must be on per
	// pixel, tess.lightmapDlightFaces); the faces of the leaves no light
	// reaches add none
	if ( dlightBits ) {
		facePlanes = 0;
	}

	tr.pc.c_leafs++;

	// add to z buffer bounds
	if ( node->mins[0] < tr.viewParms.visBounds[0][0] ) {
		tr.viewParms.visBounds[0][0] = node->mins[0];
	}
	if ( node->mins[1] < tr.viewParms.visBounds[0][1] ) {
		tr.viewParms.visBounds[0][1] = node->mins[1];
	}
	if ( node->mins[2] < tr.viewParms.visBounds[0][2] ) {
		tr.viewParms.visBounds[0][2] = node->mins[2];
	}

	if ( node->maxs[0] > tr.viewParms.visBounds[1][0] ) {
		tr.viewParms.visBounds[1][0] = node->maxs[0];
	}
	if ( node->maxs[1] > tr.viewParms.visBounds[1][1] ) {
		tr.viewParms.visBounds[1][1] = node->maxs[1];
	}
	if ( node->maxs[2] > tr.viewParms.visBounds[1][2] ) {
		tr.viewParms.visBounds[1][2] = node->maxs[2];
	}

	tr.portalsky.cntNode = node;

	if (r_drawbrushes->integer) {
		// add the individual surfaces
		mark = node->firstmarksurface;
		c = node->nummarksurfaces;
		while (c--) {
			// the surface may have already been added if it
			// spans multiple leafs
			surf = *mark;
			if (surf->viewCount != tr.viewCount) {
				R_AddWorldSurface(surf, dlightBits, planeBits & facePlanes);
			}
			mark++;
		}
	}

	if (r_drawterrain->integer && tr.refdef.render_terrain && !tr.viewParms.isPortalSky)
	{
		int i;

		for (i = 0; i < node->numTerraPatches; i++) {
			R_MarkTerrainPatch(tr.world->visTerraPatches[node->firstTerraPatch + i]);
		}
	}

	if (r_drawstaticdecals->integer) {
		if (node->pFirstMarkFragment) {
			R_AddPermanentMarkFragmentSurfaces(node->pFirstMarkFragment, node->iNumMarkFragment);
		}
	}

	if (r_drawstaticmodels->integer) {
		int i;

		for (i = 0; i < node->numStaticModels; i++) {
			tr.world->visStaticModels[node->firstStaticModel + i]->visCount = tr.visCount;
		}
	}
}

/*
================
R_AddWorldLeaves

The potentially visible leaves that the frustum doesn't cull, in the order
of GL1's BSP walk (see WORLD LEAVES)
================
*/
static void R_AddWorldLeaves( int planeBits, dlightMask_t dlightBits ) {
	const world_t *w = tr.world;
	int facePlanes = R_FaceViewPlanes();
	int i, j;

	for ( i = 0; i < w->numLeafGroups; i++ ) {
		const worldLeafGroup_t *g = &w->leafGroups[i];
		const int *leaves = w->visLeaves + g->first;
		int groupBits = planeBits;

		if ( !g->numLeaves ) {
			if ( r_nocull->integer || !R_CullLeafAlongPath( w->walkLeaves[leaves[0]], &groupBits ) ) {
				R_AddWorldLeaf( w->walkLeaves[leaves[0]], groupBits, dlightBits, facePlanes );
			}
			continue;
		}

		if ( !r_nocull->integer && R_CullBoxToFrustum( g->mins, g->maxs, &groupBits ) ) {
			continue;
		}

		if ( g->numLeaves == 1 ) {
			// the group's box is the leaf's
			R_AddWorldLeaf( w->walkLeaves[leaves[0]], groupBits, dlightBits, facePlanes );
			continue;
		}

		for ( j = 0; j < g->numLeaves; j++ ) {
			const vec3_t *bounds = w->walkLeafBounds[leaves[j]];
			int leafBits = groupBits;

			if ( !r_nocull->integer && R_CullBoxToFrustum( bounds[0], bounds[1], &leafBits ) ) {
				continue;
			}
			R_AddWorldLeaf( w->walkLeaves[leaves[j]], leafBits, dlightBits, facePlanes );
		}
	}
}

int R_SphereInLeafs(const vec3_t p, float r, mnode_t** nodes, int nMaxNodes) {
	mnode_t* nodestack[1024];
	int iNodeStackPos;
	mnode_t* pCurNode;
	int nFoundNodes;

	iNodeStackPos = 0;
	pCurNode = tr.world->nodes;
	nFoundNodes = 0;

	while (1)
    {
        cplane_t* plane;
		float d;

		while (pCurNode->contents == -1)
		{
			plane = pCurNode->plane;
			if (plane->type >= PLANE_NON_AXIAL) {
				d = DotProduct(p, plane->normal) - plane->dist;
			} else {
				d = p[plane->type] - plane->dist;
			}

            if (d < r) {
                if (d > -r) {
                    nodestack[iNodeStackPos++] = pCurNode->children[0];
                }
                pCurNode = pCurNode->children[1];
            } else {
                pCurNode = pCurNode->children[0];
			}
		}

		nodes[nFoundNodes++] = pCurNode;
		if (!iNodeStackPos || nFoundNodes == nMaxNodes) {
			break;
		}

		iNodeStackPos--;
        pCurNode = nodestack[iNodeStackPos];
	}

	return nFoundNodes;
}
/*
===============
R_PointInLeaf
===============
*/
mnode_t *R_PointInLeaf( const vec3_t p ) {
	mnode_t		*node;
	float		d;
	cplane_t	*plane;
	
	if ( !tr.world ) {
		ri.Error (ERR_DROP, "R_PointInLeaf: bad model");
	}
	
	node = tr.world->nodes;
	while( 1 ) {
		if (node->contents != -1) {
			break;
		}
		plane = node->plane;
		d = DotProduct (p,plane->normal) - plane->dist;
		if (d > 0) {
			node = node->children[0];
		} else {
			node = node->children[1];
		}
	}
	
	return node;
}

/*
==============
R_ClusterPVS
==============
*/
static const byte *R_ClusterPVS (int cluster) {
	if (!tr.world || !tr.world->vis || cluster < 0 || cluster >= tr.world->numClusters ) {
		return tr.world->novis;
	}

	return tr.world->vis + cluster * tr.world->clusterBytes;
}

/*
=================
R_inPVS
=================
*/
qboolean R_inPVS( const vec3_t p1, const vec3_t p2 ) {
	mnode_t *leaf;
	byte	*vis;

	leaf = R_PointInLeaf( p1 );
	vis = ri.CM_ClusterPVS( leaf->cluster );
	leaf = R_PointInLeaf( p2 );

	if ( !(vis[leaf->cluster>>3] & (1<<(leaf->cluster&7))) ) {
		return qfalse;
	}
	return qtrue;
}

/*
===============
R_MarkLeaves

Lists the leaves in the PVS for the current cluster (R_GroupVisibleLeaves).
GL1 marked them and their ancestors for its BSP walk in a pass over every
node, 0.23 ms in obj_team4 whenever the view's cluster changes (2 % of the
frames of a match there)
===============
*/
static void R_MarkLeaves (void) {
	mnode_t	*leaf;
	int		cluster;

	// lockpvs lets designers walk around to determine the
	// extent of the current pvs
	if ( r_lockpvs->integer ) {
		return;
	}

	// current viewcluster
	leaf = R_PointInLeaf( tr.viewParms.pvsOrigin );
	cluster = leaf->cluster;

	// if the cluster is the same and the area visibility matrix
	// hasn't changed, we don't need to mark everything again

	// if r_showcluster was just turned on, remark everything 
	if ( tr.viewCluster == cluster && !tr.refdef.areamaskModified 
		&& !r_showcluster->modified ) {
		return;
	}

	if ( r_showcluster->modified || r_showcluster->integer ) {
		r_showcluster->modified = qfalse;
		if ( r_showcluster->integer ) {
			ri.Printf( PRINT_ALL, "cluster:%i  area:%i\n", cluster, leaf->area );
		}
	}

	tr.visCount++;
	tr.viewCluster = cluster;

	if ( r_novis->integer || tr.viewCluster == -1 ) {
		R_GroupVisibleLeaves( NULL );
		return;
	}

	R_GroupVisibleLeaves( R_ClusterPVS( tr.viewCluster ) );
}

/*
=============
R_AddWorldSurfaces
=============
*/
void R_AddWorldSurfaces (void) {
	if (!r_drawworld->integer) {
		return;
	}

	if (tr.refdef.rdflags & RDF_NOWORLDMODEL) {
		return;
	}

	tr.currentEntityNum = ENTITYNUM_WORLD;
	tr.shiftedEntityNum = tr.currentEntityNum << QSORT_ENTITYNUM_SHIFT;

	if (r_drawterrain->integer && tr.refdef.render_terrain && !tr.viewParms.isPortalSky) {
		R_TerrainPrepareFrame();
	}

	// determine which leaves are in the PVS / areamask
	R_MarkLeaves();

	// clear out the visible min/max
	ClearBounds(tr.viewParms.visBounds[0], tr.viewParms.visBounds[1]);

	// perform frustum culling and add all the potentially visible surfaces
	// (RE_RenderScene kept at most r_maxDlights lights)
	R_TransformDlights(tr.refdef.num_dlights, tr.refdef.dlights, &tr.viewParms.world);
	R_AddWorldLeaves(tr.viewParms.fog.extrafrustums ? 31 : 15, DLIGHTS_ALL(tr.refdef.num_dlights));

	if (r_drawterrain->integer && tr.refdef.render_terrain && !tr.viewParms.isPortalSky) {
		R_AddTerrainSurfaces();
	}
	if (r_drawstaticmodels->integer) {
		R_AddStaticModelSurfaces();
	}

	if (g_bInfostaticmodels) {
		g_bInfostaticmodels = 0;
		R_PrintInfoStaticModels();
	}

	R_UpdateLevelMarksSystem();
}
