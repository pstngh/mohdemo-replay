/*
===========================================================================
Copyright (C) 2015-2024 the OpenMoHAA team

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
// tr_light.c

#include "tr_local.h"

#define DLIGHT_AT_RADIUS 16
// at the edge of a dlight's influence, this amount of light will be added

#define DLIGHT_MINIMUM_RADIUS 16

// never calculate a range less than this to prevent huge light numbers

/*
=============================================================================

DYNAMIC LIGHTS ON LIGHTMAPS (r_fastdlights 0)

GL1 relit the lightmap of each surface a light reached, texel by texel
(R_DlightSample: color * 375 * ( 1 - d / radius )^2 added to the lightmap
data at the texel's sample point, the sum shifted by tr.overbrightShift and
divided by its largest component when above 255), into atlas textures
uploaded every frame, and drew the lit surfaces with them. The fragment
shader now does that per pixel on the filtered lightmap (RB_LightmapDlights,
vk_set_lightmap_dlights), so the lit surfaces need no atlas, no uploads and
no batch of their own, and stay on the static geometry. The functions below
give each surface the lights GL1 lit it with: the same tests (the face's
plane and r_dlightBacks, the bounds of patches and terrain, as the GL1 code
the bits come from), and the light must reach a sample point of its
lightmap texels (with a margin of 0.1 % of the radius plus a unit: a light
too many in a surface's mask adds nothing beyond its radius).

=============================================================================
*/

#define DLIGHT_REACH(radius) ((radius) * 1.001f + 1.f)

/*
===============
R_DistanceSquaredToBox
===============
*/
static float R_DistanceSquaredToBox(const vec3_t p, const vec3_t mins, const vec3_t maxs)
{
    float d, dist;
    int   i;

    dist = 0;
    for (i = 0; i < 3; i++) {
        if (p[i] < mins[i]) {
            d = mins[i] - p[i];
        } else if (p[i] > maxs[i]) {
            d = p[i] - maxs[i];
        } else {
            continue;
        }
        dist += d * d;
    }

    return dist;
}

/*
===============
R_DistanceSquaredToSegment

From p to o + s * v, s in [0, len]
===============
*/
static double R_DistanceSquaredToSegment(const double *p, const double *o, const double *v, double len)
{
    double d[3], s, vv;
    int    i;

    for (i = 0; i < 3; i++) {
        d[i] = p[i] - o[i];
    }
    vv = v[0] * v[0] + v[1] * v[1] + v[2] * v[2];
    s  = vv > 0 ? (d[0] * v[0] + d[1] * v[1] + d[2] * v[2]) / vv : 0;
    s  = s < 0 ? 0 : (s > len ? len : s);
    for (i = 0; i < 3; i++) {
        d[i] -= s * v[i];
    }

    return d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
}

/*
===============
R_DlightReachesFace

Whether a light reaches a texel of the face's lightmap: GL1 sampled the
texel (s, t) at lmOrigin + s * lmVecs[0] + t * lmVecs[1], its center (the
face's vertexes are within the texel centers)
===============
*/
static qboolean R_DlightReachesFace(const srfSurfaceFace_t *srf, const vec3_t origin, float radius)
{
    double o[3], v[2][3], p[3], corner[3], d[3];
    double vv00, vv01, vv11, dv0, dv1, det, s, t, dist, reach;
    double sMax, tMax;
    int    i;

    sMax  = srf->lmWidth - 1;
    tMax  = srf->lmHeight - 1;
    reach = DLIGHT_REACH(radius);

    for (i = 0; i < 3; i++) {
        o[i]    = srf->lmOrigin[i];
        v[0][i] = srf->lmVecs[0][i];
        v[1][i] = srf->lmVecs[1][i];
        p[i]    = origin[i];
        d[i]    = p[i] - o[i];
    }

    // the nearest point of the parallelogram: inside it, or on an edge
    vv00 = v[0][0] * v[0][0] + v[0][1] * v[0][1] + v[0][2] * v[0][2];
    vv01 = v[0][0] * v[1][0] + v[0][1] * v[1][1] + v[0][2] * v[1][2];
    vv11 = v[1][0] * v[1][0] + v[1][1] * v[1][1] + v[1][2] * v[1][2];
    dv0  = d[0] * v[0][0] + d[1] * v[0][1] + d[2] * v[0][2];
    dv1  = d[0] * v[1][0] + d[1] * v[1][1] + d[2] * v[1][2];
    det  = vv00 * vv11 - vv01 * vv01;

    if (det > 1e-12 * vv00 * vv11) {
        s = (dv0 * vv11 - dv1 * vv01) / det;
        t = (dv1 * vv00 - dv0 * vv01) / det;
        if (s >= 0 && s <= sMax && t >= 0 && t <= tMax) {
            for (i = 0; i < 3; i++) {
                d[i] -= s * v[0][i] + t * v[1][i];
            }
            return d[0] * d[0] + d[1] * d[1] + d[2] * d[2] <= reach * reach;
        }
    }

    dist = R_DistanceSquaredToSegment(p, o, v[0], sMax);
    dist = Q_min(dist, R_DistanceSquaredToSegment(p, o, v[1], tMax));
    for (i = 0; i < 3; i++) {
        corner[i] = o[i] + sMax * v[0][i] + tMax * v[1][i];
        v[0][i]   = -v[0][i];
        v[1][i]   = -v[1][i];
    }
    dist = Q_min(dist, R_DistanceSquaredToSegment(p, corner, v[0], sMax));
    dist = Q_min(dist, R_DistanceSquaredToSegment(p, corner, v[1], tMax));

    return dist <= reach * reach;
}

/*
===============
R_RealDlightPatch
===============
*/
dlightMask_t R_RealDlightPatch(srfGridMesh_t *srf, dlightMask_t dlightBits)
{
    dlight_t    *dl;
    dlightMask_t mask;
    int          i;

    mask = 0;

    if (srf->lmHeight && srf->lmWidth) {
        for (i = 0; i < tr.refdef.num_dlights; i++) {
            if (!(dlightBits & DLIGHT_BIT(i))) {
                continue;
            }

            dl = &tr.refdef.dlights[i];
            if (dl->origin[0] + dl->radius < srf->meshBounds[0][0] || dl->origin[0] - dl->radius > srf->meshBounds[1][0]
                || dl->origin[1] + dl->radius < srf->meshBounds[0][1] || dl->origin[1] - dl->radius > srf->meshBounds[1][1]
                || dl->origin[2] + dl->radius < srf->meshBounds[0][2]
                || dl->origin[2] - dl->radius > srf->meshBounds[1][2]) {
                continue;
            }

            // the sample points are within the vertexes' bounds
            if (R_DistanceSquaredToBox(dl->transformed, srf->meshBounds[0], srf->meshBounds[1])
                > Square(DLIGHT_REACH(dl->radius))) {
                continue;
            }

            mask |= DLIGHT_BIT(i);
        }
    }

    if (mask) {
        tr.pc.c_dlightSurfaces++;
    }
    srf->dlightMap = mask;
    return mask;
}

/*
===============
R_RealDlightFace
===============
*/
dlightMask_t R_RealDlightFace(srfSurfaceFace_t *srf, dlightMask_t dlightBits)
{
    dlight_t    *dl;
    float        d;
    dlightMask_t mask;
    int          i;

    mask = 0;

    if (srf->lmHeight && srf->lmWidth) {
        for (i = 0; i < tr.refdef.num_dlights; i++) {
            if (!(dlightBits & DLIGHT_BIT(i))) {
                continue;
            }

            dl = &tr.refdef.dlights[i];

            d = DotProduct(dl->transformed, srf->plane.normal) - srf->plane.dist;

            if ((d < 0.f && !r_dlightBacks->integer) || (d > dl->radius || d < -dl->radius)) {
                // dlight doesn't reach the plane
                continue;
            }

            // nor a texel: their bounds first, most faces whose plane a
            // light reaches are far from it
            if (R_DistanceSquaredToBox(dl->transformed, srf->lmBounds[0], srf->lmBounds[1])
                    > Square(DLIGHT_REACH(dl->radius))
                || !R_DlightReachesFace(srf, dl->transformed, dl->radius)) {
                continue;
            }

            mask |= DLIGHT_BIT(i);
        }
    }

    if (mask) {
        tr.pc.c_dlightSurfaces++;
    }
    srf->dlightMap = mask;
    return mask;
}

/*
===============
R_RealDlightTerrain
===============
*/
dlightMask_t R_RealDlightTerrain(cTerraPatchUnpacked_t *srf, dlightMask_t dlightBits)
{
    dlight_t    *dl;
    vec3_t       mins, maxs;
    float        delta, dist;
    dlightMask_t mask;
    int          i;

    mask = 0;

    // the sample points of the lightmap (heights 0 to zmax above z0)
    VectorSet(mins, srf->x0, srf->y0, srf->z0);
    VectorSet(maxs, srf->x0 + 512.f, srf->y0 + 512.f, srf->z0 + srf->zmax);

    for (i = 0; i < tr.refdef.num_dlights; i++) {
        dl = &tr.refdef.dlights[i];

        if (dl->radius < srf->x0 - dl->transformed[0] || -512.f - dl->radius > srf->x0 - dl->transformed[0]
            || -512.f - dl->radius > srf->y0 - dl->transformed[1] || dl->radius < srf->y0 - dl->transformed[1]) {
            continue;
        }

        if (dl->radius > 128.f) {
            dist = 0.f;

            delta = dl->transformed[0] - srf->x0;
            if (delta <= 0.f) {
                dist += delta * delta;
            } else {
                delta -= 512.f;
                if (delta > 0.f) {
                    dist += delta * delta;
                }
            }

            delta = dl->transformed[1] - srf->y0;
            if (delta <= 0.f) {
                dist += delta * delta;
            } else {
                delta -= 512.f;
                if (delta > 0.f) {
                    dist += delta * delta;
                }
            }

            delta = dl->transformed[2] - srf->z0;
            if (delta <= 0.f) {
                dist += delta * delta;
            } else {
                delta -= srf->zmax;
                if (delta > 0.f) {
                    dist += delta * delta;
                }
            }

            if (dl->radius * dl->radius < dist) {
                continue;
            }
        }

        if (R_DistanceSquaredToBox(dl->origin, mins, maxs) > Square(DLIGHT_REACH(dl->radius))) {
            continue;
        }

        mask |= DLIGHT_BIT(i);
    }

    if (mask) {
        tr.pc.c_dlightSurfaces++;
    }
    srf->drawinfo.dlightMap = mask;
    return mask;
}

/*
===============
R_DlightBoundsForNode
===============
*/
static void R_DlightBoundsForNode(mnode_t *node)
{
    int i;

    ClearBounds(node->dlightBounds[0], node->dlightBounds[1]);

    if (node->contents == -1) {
        for (i = 0; i < 2; i++) {
            mnode_t *child = node->children[i];

            R_DlightBoundsForNode(child);
            if (child->dlightBounds[0][0] <= child->dlightBounds[1][0]) {
                AddPointToBounds(child->dlightBounds[0], node->dlightBounds[0], node->dlightBounds[1]);
                AddPointToBounds(child->dlightBounds[1], node->dlightBounds[0], node->dlightBounds[1]);
            }
        }
        return;
    }

    for (i = 0; i < node->nummarksurfaces; i++) {
        const msurface_t *surf = node->firstmarksurface[i];

        if (!surf->shader || surf->shader->lightmapIndex < 0) {
            continue;
        }

        if (*surf->data == SF_FACE) {
            const srfSurfaceFace_t *face = (const srfSurfaceFace_t *)surf->data;

            if (face->lmWidth && face->lmHeight) {
                AddPointToBounds(face->lmBounds[0], node->dlightBounds[0], node->dlightBounds[1]);
                AddPointToBounds(face->lmBounds[1], node->dlightBounds[0], node->dlightBounds[1]);
            }
        } else if (*surf->data == SF_GRID) {
            const srfGridMesh_t *grid = (const srfGridMesh_t *)surf->data;

            if (grid->lmWidth && grid->lmHeight) {
                AddPointToBounds(grid->meshBounds[0], node->dlightBounds[0], node->dlightBounds[1]);
                AddPointToBounds(grid->meshBounds[1], node->dlightBounds[0], node->dlightBounds[1]);
            }
        }
    }
}

/*
===============
R_SetupDlightBounds

For each node of the world's BSP tree, the bounds of what the lights can
reach in its subtree: the lightmap texels of the faces and the vertexes of
the patches with a lightmap (R_RealDlightFace, R_RealDlightPatch). Called
once the world is loaded
===============
*/
void R_SetupDlightBounds(void)
{
    if (tr.world && tr.world->nodes) {
        R_DlightBoundsForNode(tr.world->nodes);
    }
}

/*
===============
R_CullDlightsToNode

The lights of dlightBits that reach something in the node's subtree
(R_RecursiveWorldNode, r_fastdlights 0): a light that misses its bounds
reaches none of its surfaces' texels, so the surfaces get the same lights.
GL1 gave every visible surface every light to test
===============
*/
dlightMask_t R_CullDlightsToNode(const mnode_t *node, dlightMask_t dlightBits)
{
    int i;

    for (i = 0; i < tr.refdef.num_dlights; i++) {
        const dlight_t *dl = &tr.refdef.dlights[i];

        if ((dlightBits & DLIGHT_BIT(i))
            && R_DistanceSquaredToBox(dl->transformed, node->dlightBounds[0], node->dlightBounds[1])
                   > Square(DLIGHT_REACH(dl->radius))) {
            dlightBits &= ~DLIGHT_BIT(i);
        }
    }

    return dlightBits;
}

/*
===============
R_DlightSortKey

The dlight bits of a surface's sort key, from what R_CheckDlightSurface or
R_CheckDlightTerrain returned. r_fastdlights 1: GL1's, 1 for the surfaces
ProjectDlightTexture lights. r_fastdlights 0: the lights are per pixel, so
the surfaces sort as without them
===============
*/
int R_DlightSortKey(dlightMask_t dlightBits)
{
    return (r_fastdlights->integer && dlightBits) ? 1 : 0;
}

/*
===============
RB_TransformDlights

The lights in the space of the current entity (backEnd.ori) for the
surfaces that follow; their list for the shaders is stored on first use
(RB_LightmapDlights), the world's once per view
===============
*/
void RB_TransformDlights(qboolean world)
{
    R_TransformDlights(backEnd.refdef.num_dlights, backEnd.refdef.dlights, &backEnd.ori);
    backEnd.dlightList = world ? backEnd.worldDlightList : -1;
}

/*
===============
RB_LightmapDlights

The lights of the batch (tess.lightmapDlights) on the lightmap of texture
unit tmu for the following draws (vk_set_lightmap_dlights); -1 turns them
off. The colors are GL1's: color * 375 on lightmap bytes, shifted by
tr.overbrightShift
===============
*/
void RB_LightmapDlights(int tmu)
{
    if (tmu < 0 || !tess.lightmapDlights) {
        vk_set_lightmap_dlights(-1, -1, 0, qfalse);
        return;
    }

    if (backEnd.dlightList < 0) {
        vkDlight_t lights[MAX_SCENE_DLIGHTS];
        int        i;

        for (i = 0; i < backEnd.refdef.num_dlights; i++) {
            const dlight_t *dl = &backEnd.refdef.dlights[i];

            VectorCopy(dl->transformed, lights[i].origin);
            lights[i].radius = dl->radius;
            lights[i].scale  = tr.overbrightMult / 255.f;
            VectorScale(dl->color, 375.f * lights[i].scale, lights[i].color);
        }

        backEnd.dlightList = vk_add_dlights(backEnd.refdef.num_dlights, lights);
        if (backEnd.currentEntity == &tr.worldEntity) {
            backEnd.worldDlightList = backEnd.dlightList;
        }
    }

    vk_set_lightmap_dlights(
        tmu, backEnd.dlightList, tess.lightmapDlights, !r_dlightBacks->integer && tess.lightmapDlightFaces
    );
}

/*
===============
R_TransformDlights

Transforms the origins of an array of dlights.
Used by both the front end (for DlightBmodel) and
the back end (before doing the lighting calculation)
===============
*/
void R_TransformDlights(int count, dlight_t *dl, orientationr_t *ori)
{
    int    i;
    vec3_t temp;

    for (i = 0; i < count; i++, dl++) {
        VectorSubtract(dl->origin, ori->origin, temp);
        dl->transformed[0] = DotProduct(temp, ori->axis[0]);
        dl->transformed[1] = DotProduct(temp, ori->axis[1]);
        dl->transformed[2] = DotProduct(temp, ori->axis[2]);
    }
}

/*
=============
R_DlightBmodel

Determine which dynamic lights may effect this bmodel: their mask, which
R_AddBrushModelSurfaces gives the surfaces to test. ProjectDlightTexture
(r_fastdlights 1) starts from it for the surfaces it doesn't test, the
triangle surfaces
=============
*/
dlightMask_t R_DlightBmodel(bmodel_t *bmodel)
{
    int          i, j;
    dlight_t    *dl;
    dlightMask_t mask, bits;
    msurface_t  *surf;

    // transform all the lights
    R_TransformDlights(tr.refdef.num_dlights, tr.refdef.dlights, &tr.ori);

    mask = 0;
    for (i = 0; i < tr.refdef.num_dlights; i++) {
        dl = &tr.refdef.dlights[i];

        // see if the point is close enough to the bounds to matter
        for (j = 0; j < 3; j++) {
            if (dl->transformed[j] - bmodel->bounds[1][j] > dl->radius) {
                break;
            }
            if (bmodel->bounds[0][j] - dl->transformed[j] > dl->radius) {
                break;
            }
        }
        if (j < 3) {
            continue;
        }

        // we need to check this light
        mask |= DLIGHT_BIT(i);
    }

    tr.currentEntity->needDlights = (mask != 0);

    // set the dlight bits in all the surfaces
    bits = r_fastdlights->integer ? mask : 0;
    for (i = 0; i < bmodel->numSurfaces; i++) {
        surf = bmodel->firstSurface + i;

        if (*surf->data == SF_FACE) {
            ((srfSurfaceFace_t *)surf->data)->dlightBits = bits;
        } else if (*surf->data == SF_GRID) {
            ((srfGridMesh_t *)surf->data)->dlightBits = bits;
        } else if (*surf->data == SF_TRIANGLES) {
            ((srfTriangles_t *)surf->data)->dlightBits = bits;
        }
    }

    return mask;
}

/*
===============
R_GetLightGridPalettedColor
===============
*/
static byte *R_GetLightGridPalettedColor(int iColor)
{
    return &tr.world->lightGridPalette[iColor * 3];
}

/*
=================================================================

LIGHT GRID LOOKUPS

R_GetLightingGridValue blends the light grid's 8 samples around a point:
the samples of its cell's 4 columns at the cell's row and the row above.
The grid stores each column run-length encoded, so every lookup decoded 4
columns from their start. What depends on the world alone is kept, in two
tables allocated with it (R_InitLightGridCaches, zeroed: empty):

- the samples of a cell (lightGridCell_t): the palette indexes its 4
  columns hold at the two rows, decoded at the cell's first lookup;
- whole lookups by the point's exact bits (lightGridPoint_t): cgame lights
  every particle of smoke and debris with a lookup a frame
  (R_GetLightingForSmoke), and debris mostly lies still.

The blend runs on the same samples in the same order as before, so the
results are the same to the bit. From JayRewd's b26d8a67, which kept the
decoded colors of 1024 cells and the smoke's lookups by origin.

=================================================================
*/

#define LIGHTGRID_CELL_BITS		10	// 1024 cells, 16 KB
#define LIGHTGRID_POINT_BITS	10	// 1024 points, 32 KB

// one column of a cell: the palette indexes of its samples at the cell's
// row and the row above (0: no sample), or one sample for both rows, which
// the blend weighs with the sum of both weights
typedef struct {
    byte sample[2];
    byte both;
} lightGridColumn_t;

typedef struct lightGridCell_s {
    unsigned          key; // cell index + 1, 0: empty
    lightGridColumn_t column[4];
} lightGridCell_t;

typedef struct lightGridPoint_s {
    unsigned pos[3]; // the bits of the point
    unsigned used;
    vec3_t   light;
    unsigned pad;
} lightGridPoint_t;

/*
===============
R_InitLightGridCaches

Called once the world's light grid is loaded
===============
*/
void R_InitLightGridCaches(world_t *w)
{
    w->lightGridCells  = NULL;
    w->lightGridPoints = NULL;

    // the cell's index is its key
    if ((unsigned long long)w->lightGridBounds[0] * w->lightGridBounds[1] * w->lightGridBounds[2] < 0xFFFFFFFFu) {
        w->lightGridCells = ri.Hunk_Alloc(sizeof(lightGridCell_t) << LIGHTGRID_CELL_BITS, h_dontcare);
    }
    w->lightGridPoints = ri.Hunk_Alloc(sizeof(lightGridPoint_t) << LIGHTGRID_POINT_BITS, h_dontcare);
}

/*
===============
R_DecodeLightGridColumn

The samples of a column at row iRowPos and the one above, as GL1's
R_GetLightingGridValue walked it
===============
*/
static void R_DecodeLightGridColumn(const byte *pCurData, int iRowPos, lightGridColumn_t *column)
{
    int iData;
    int iLen;
    int iCurData;

    iData = 0;
    iLen  = 0;

    while (1) {
        while (1) {
            iCurData = (char)pCurData[iData];
            iData++;
            if (iCurData >= 0) {
                break;
            }

            iLen = -iCurData;
            if (iLen > iRowPos) {
                iData += iRowPos;
                column->sample[0] = pCurData[iData];

                iData++;
                if (iLen - 1 == iRowPos) {
                    iData++;
                }

                column->sample[1] = pCurData[iData];
                column->both      = qfalse;
                return;
            }

            iRowPos -= iLen;
            iData += iLen;
        }

        iLen = iCurData + 2;
        if (iLen - 1 >= iRowPos) {
            break;
        }

        iRowPos -= iLen;
        iData++;
    }

    if (iLen - 1 > iRowPos) {
        column->sample[0] = pCurData[iData];
        column->sample[1] = 0;
        column->both      = qtrue;
    } else {
        column->sample[0] = pCurData[iData];
        column->sample[1] = pCurData[iData + 2];
        column->both      = qfalse;
    }
}

/*
===============
R_LightGridCell

The samples of the cell at iGridPos, from the cache or decoded into it
(into *uncached without a cache)
===============
*/
static const lightGridCell_t *R_LightGridCell(const int iGridPos[3], lightGridCell_t *uncached)
{
    const world_t   *w = tr.world;
    lightGridCell_t *cell;
    unsigned         key;
    int              iArrayXStep, iBaseOffset, iHigh;

    key = ((unsigned)iGridPos[0] * w->lightGridBounds[1] + iGridPos[1]) * w->lightGridBounds[2] + iGridPos[2] + 1;
    if (w->lightGridCells) {
        cell = &w->lightGridCells[(key * 2654435761u) >> (32 - LIGHTGRID_CELL_BITS)];
        if (cell->key == key) {
            return cell;
        }
    } else {
        cell = uncached;
    }

    iArrayXStep = w->lightGridBounds[1];
    iBaseOffset = w->lightGridBounds[0] + iGridPos[1] + iArrayXStep * iGridPos[0];
    iHigh       = w->lightGridOffsets[iGridPos[0]] << 8;
    R_DecodeLightGridColumn(&w->lightGridData[w->lightGridOffsets[iBaseOffset] + iHigh], iGridPos[2], &cell->column[0]);
    R_DecodeLightGridColumn(
        &w->lightGridData[w->lightGridOffsets[iBaseOffset + 1] + iHigh], iGridPos[2], &cell->column[1]
    );
    iHigh = w->lightGridOffsets[iGridPos[0] + 1] << 8;
    R_DecodeLightGridColumn(
        &w->lightGridData[w->lightGridOffsets[iBaseOffset + iArrayXStep] + iHigh], iGridPos[2], &cell->column[2]
    );
    R_DecodeLightGridColumn(
        &w->lightGridData[w->lightGridOffsets[iBaseOffset + iArrayXStep + 1] + iHigh], iGridPos[2], &cell->column[3]
    );
    cell->key = key;
    return cell;
}

/*
===============
R_LightGridValue
===============
*/
static void R_LightGridValue(const vec3_t vPos, vec3_t vLight)
{
    const lightGridCell_t   *cell;
    const lightGridColumn_t *column;
    lightGridCell_t          uncached;
    byte                    *pColor;
    int                      i;
    int                      iGridPos[3];
    float                    fV;
    float                    fFrac[3];
    float                    fOMFrac[3];
    float                    fWeight, fWeight2;
    float                    fTotalFactor;
    vec3_t                   vLightOrigin;

    VectorSubtract(vPos, tr.world->lightGridMins, vLightOrigin);

    for (i = 0; i < 3; i++) {
        fV = vLightOrigin[i] * tr.world->lightGridOOSize[i];
        // floor
        iGridPos[i] = (int)fV;
        if (fV < iGridPos[i]) {
            iGridPos[i]--;
        }
        fFrac[i]   = fV - iGridPos[i];
        fOMFrac[i] = 1.0 - fFrac[i];

        if (iGridPos[i] < 0) {
            iGridPos[i] = 0;
        } else if (iGridPos[i] > tr.world->lightGridBounds[i] - 2) {
            iGridPos[i] = tr.world->lightGridBounds[i] - 2;
        }
    }

    cell = R_LightGridCell(iGridPos, &uncached);

    fTotalFactor = 0;
    VectorClear(vLight);

    for (i = 0; i < 4; i++) {
        switch (i) {
        case 0:
            fWeight  = fOMFrac[0] * fOMFrac[1] * fOMFrac[2];
            fWeight2 = fOMFrac[0] * fOMFrac[1] * fFrac[2];
            break;
        case 1:
            fWeight  = fOMFrac[0] * fFrac[1] * fOMFrac[2];
            fWeight2 = fOMFrac[0] * fFrac[1] * fFrac[2];
            break;
        case 2:
            fWeight  = fFrac[0] * fOMFrac[1] * fOMFrac[2];
            fWeight2 = fFrac[0] * fOMFrac[1] * fFrac[2];
            break;
        case 3:
        default:
            fWeight  = fFrac[0] * fFrac[1] * fOMFrac[2];
            fWeight2 = fFrac[0] * fFrac[1] * fFrac[2];
            break;
        }

        column = &cell->column[i];
        if (column->both) {
            if (column->sample[0]) {
                pColor = R_GetLightGridPalettedColor(column->sample[0]);
                VectorMA(vLight, fWeight + fWeight2, pColor, vLight);
                fTotalFactor += fWeight + fWeight2;
            }
            continue;
        }

        if (column->sample[0]) {
            pColor = R_GetLightGridPalettedColor(column->sample[0]);
            VectorMA(vLight, fWeight, pColor, vLight);
            fTotalFactor += fWeight;
        }
        if (column->sample[1]) {
            pColor = R_GetLightGridPalettedColor(column->sample[1]);
            VectorMA(vLight, fWeight2, pColor, vLight);
            fTotalFactor += fWeight2;
        }
    }

    if (fTotalFactor > 0.0 && fTotalFactor < 0.99) {
        VectorScale(vLight, 1.0 / fTotalFactor, vLight);
    }

    if (fTotalFactor) {
        if (vLight[0] > 255.0 || vLight[1] > 255.0 || vLight[2] > 255.0) {
            float t;
            // normalize color values
            t = 255.0 / Q_max(vLight[0], Q_max(vLight[1], vLight[2]));
            VectorScale(vLight, t, vLight);
        }
    } else {
        vLight[0] = vLight[1] = vLight[2] = tr.identityLightByte;
    }
}

/*
===============
R_GetLightingGridValue
===============
*/
void R_GetLightingGridValue(const vec3_t vPos, vec3_t vLight)
{
    lightGridPoint_t *point;
    unsigned          pos[3];
    unsigned          h;

    if (!tr.world || !tr.world->lightGridData || !tr.world->lightGridOffsets) {
        vLight[0] = vLight[1] = vLight[2] = tr.identityLightByte;
        return;
    }

    Com_Memcpy(pos, vPos, sizeof(pos));
    h     = ((pos[0] * 0x9E3779B1u ^ pos[1]) * 0x85EBCA77u ^ pos[2]) * 0xC2B2AE3Du;
    point = &tr.world->lightGridPoints[h >> (32 - LIGHTGRID_POINT_BITS)];
    if (point->used && point->pos[0] == pos[0] && point->pos[1] == pos[1] && point->pos[2] == pos[2]) {
        VectorCopy(point->light, vLight);
        return;
    }

    R_LightGridValue(vPos, vLight);

    point->pos[0] = pos[0];
    point->pos[1] = pos[1];
    point->pos[2] = pos[2];
    point->used   = qtrue;
    VectorCopy(vLight, point->light);
}

/*
===============
R_GetLightingGridValueFast
===============
*/
void R_GetLightingGridValueFast(const vec3_t vPos, vec3_t vLight)
{
    byte      *pColor;
    int        i;
    int        iRowPos;
    int        iGridPos[3];
    int        iArrayXStep;
    float      fV[3];
    vec3_t     vLightOrigin;
    int        iData;
    int        iLen;
    int        iBaseOffset;
    byte      *pCurData;
    int        iOrder;
    int        iToggle;
    int        iSample[8];
    float      fDist[3];
    int        j, k;
    static int iSearch[8][8] = {
        {0, 4, 2, 6, 1, 5, 3, 7},
        {0, 2, 4, 6, 1, 3, 5, 7},
        {0, 0, 0, 0, 0, 0, 0, 0},
        {0, 2, 1, 3, 4, 6, 5, 7},
        {0, 4, 1, 5, 2, 6, 3, 7},
        {0, 0, 0, 0, 0, 0, 0, 0},
        {0, 1, 4, 5, 2, 3, 6, 7},
        {0, 1, 2, 3, 4, 5, 6, 7}
    };

    if (!tr.world->lightGridData || !tr.world->lightGridOffsets) {
        vLight[0] = vLight[1] = vLight[2] = tr.identityLightByte;
        return;
    }

    iOrder  = 0;
    iToggle = 0;

    for (i = 0; i < 3; i++) {
        fV[i] =
            (tr.world->lightGridSize[i] * 0.5 + vPos[i] - tr.world->lightGridMins[i]) * tr.world->lightGridOOSize[i];
        iGridPos[i] = floor(fV[i]);
        fV[i] -= iGridPos[i];

        if (iGridPos[i] < 0) {
            iGridPos[i] = 0;
            fV[i]       = 0.0;
        } else if (iGridPos[i] > tr.world->lightGridBounds[i] - 2) {
            iGridPos[i] = tr.world->lightGridBounds[i] - 2;
            fV[i]       = 1.0;
        }

        fDist[i] = 0.5 - fV[i];
        if (fDist[i] < 0) {
            fDist[i] = -fDist[i];
            iToggle |= 1 << i;
        }
    }

    if (fDist[1] >= fDist[0]) {
        iOrder = 1;
    }
    if (fDist[2] >= fDist[0]) {
        iOrder |= 2;
    }
    if (fDist[2] >= fDist[1]) {
        iOrder |= 4;
    }

    for (i = 0; i < 8; i++) {
        iSample[i] = -1;
    }

    iArrayXStep = tr.world->lightGridBounds[1];
    iBaseOffset = iGridPos[1] + tr.world->lightGridBounds[0] + iGridPos[0] * tr.world->lightGridBounds[1];

    for (i = 0; i < 8; i++) {
        j = iSearch[iOrder][i] ^ iToggle;

        if (iSample[j] < 0) {
            switch (j & 3) {
            case 0:
                pCurData =
                    &tr.world->lightGridData
                         [tr.world->lightGridOffsets[iGridPos[0]] * 256 + tr.world->lightGridOffsets[iBaseOffset]];
                break;
            case 1:
                pCurData =
                    &tr.world->lightGridData
                         [tr.world->lightGridOffsets[iGridPos[0]] * 256 + tr.world->lightGridOffsets[iBaseOffset + 1]];
                break;
            case 2:
                pCurData = &tr.world->lightGridData
                                [tr.world->lightGridOffsets[iGridPos[0] + 1] * 256
                                 + tr.world->lightGridOffsets[iBaseOffset + iArrayXStep]];
                break;
            case 3:
            default:
                pCurData = &tr.world->lightGridData
                                [tr.world->lightGridOffsets[iGridPos[0] + 1] * 256
                                 + tr.world->lightGridOffsets[iBaseOffset + iArrayXStep + 1]];
                break;
            }

            k = j & ~1;

            iRowPos = iGridPos[2];
            iData   = 0;

            while (1) {
                qboolean bShouldBreak = qfalse;

                for (; (char)pCurData[iData] >= 0; iData += 2, iRowPos -= iLen) {
                    iLen = (char)pCurData[iData] + 2;

                    if (iLen - 1 < iRowPos) {
                        continue;
                    }

                    if (iLen - 1 > iRowPos) {
                        iSample[k + 1] = pCurData[iData + 1];
                        iSample[k]     = pCurData[iData + 1];
                    } else {
                        iSample[k]     = pCurData[iData + 1];
                        iSample[k + 1] = pCurData[iData + 3];
                    }

                    bShouldBreak = qtrue;
                    break;
                }

                if (bShouldBreak) {
                    break;
                }

                if (-(char)pCurData[iData] > iRowPos) {
                    iSample[k] = pCurData[iRowPos + iData + 1];

                    if ((-(char)pCurData[iData] - 1) == iRowPos) {
                        iSample[k + 1] = pCurData[iRowPos + iData + 3];
                    } else {
                        iSample[k + 1] = pCurData[iRowPos + iData + 2];
                    }
                    break;
                }

                iRowPos -= -(char)pCurData[iData];
                iData += -(char)pCurData[iData] + 1;
            }
        }

        if (iSample[j] > 0) {
            pColor    = R_GetLightGridPalettedColor(iSample[j]);
            vLight[0] = pColor[0];
            vLight[1] = pColor[1];
            vLight[2] = pColor[2];
            return;
        }
    }

    vLight[0] = vLight[1] = vLight[2] = tr.identityLightByte;
}

/*
===============
R_GetLightingForDecal
===============
*/
void R_GetLightingForDecal(vec3_t vLight, const vec3_t vFacing, const vec3_t vOrigin)
{
    float fMax;

    R_GetLightingGridValue(vOrigin, vLight);

    if (!tr.overbrightShift) {
        return;
    }

    VectorScale(vLight, tr.overbrightMult, vLight);

    if (vLight[0] > 255.0 || vLight[1] > 255.0 || vLight[2] > 255.0) {
        float scale = 255.0 / Q_max(vLight[0], Q_max(vLight[1], vLight[2]));
        VectorScale(vLight, scale, vLight);
    }
}

/*
===============
R_GetLightingForSmoke
===============
*/
void R_GetLightingForSmoke(vec3_t vLight, const vec3_t vOrigin)
{
    int       i;
    dlight_t *dl;
    float     power;
    vec3_t    dir;
    float     d;

    if (r_smoothsmokelight->integer) {
        R_GetLightingGridValue(vOrigin, vLight);
    } else {
        R_GetLightingGridValueFast(vOrigin, vLight);
    }

    for (i = 0; i < backEnd.refdef.num_dlights; i++) {
        dl = &backEnd.refdef.dlights[i];
        VectorSubtract(dl->origin, vOrigin, dir);
        d = VectorLengthSquared(dir);

        power = dl->radius * dl->radius;
        if (power >= d) {
            d = dl->radius * 7500.0 / d;
            VectorMA(vLight, d, dl->color, vLight);
        }
    }

    if (tr.overbrightShift) {
        vLight[0] = tr.overbrightMult * vLight[0];
        vLight[1] = tr.overbrightMult * vLight[1];
        vLight[2] = tr.overbrightMult * vLight[2];
    }

    // normalize
    if (vLight[0] > 255.0 || vLight[1] > 255.0 || vLight[2] > 255.0) {
        float scale = 255.0 / Q_max(vLight[0], Q_max(vLight[1], vLight[2]));
        VectorScale(vLight, scale, vLight);
    }

    vLight[0] /= 255.0;
    vLight[1] /= 255.0;
    vLight[2] /= 255.0;
}

/*
===============
RB_GetEntityGridLighting
===============
*/
static int RB_GetEntityGridLighting()
{
    int       iColor;
    int       i;
    dlight_t *dl;
    float     power;
    vec3_t    vLight;
    vec3_t    dir;
    float     d;
    float    *lightOrigin;

    lightOrigin = backEnd.currentSphere->traceOrigin;
    if (!(backEnd.refdef.rdflags & RDF_NOWORLDMODEL) && tr.world->lightGridData) {
        R_GetLightingGridValue(backEnd.currentSphere->traceOrigin, vLight);
    } else {
        vLight[0] = vLight[1] = vLight[2] = tr.identityLight * 150.0;
    }

    for (i = 0; i < backEnd.refdef.num_dlights; i++) {
        dl = &backEnd.refdef.dlights[i];
        VectorSubtract(dl->origin, lightOrigin, dir);
        d = VectorLengthSquared(dir);

        power = dl->radius * dl->radius;
        if (power >= d) {
            d = dl->radius * 7500.0 / d;
            VectorMA(vLight, d, dl->color, vLight);
        }
    }

    if (tr.overbrightShift) {
        vLight[0] = tr.overbrightMult * vLight[0];
        vLight[1] = tr.overbrightMult * vLight[1];
        vLight[2] = tr.overbrightMult * vLight[2];
    }

    // normalize
    if (vLight[0] > 255.0 || vLight[1] > 255.0 || vLight[2] > 255.0) {
        float scale = 255.0 / Q_max(vLight[0], Q_max(vLight[1], vLight[2]));
        VectorScale(vLight, scale, vLight);
    }

    // clamp ambient
    for (i = 0; i < 3; i++) {
        if (vLight[i] > tr.identityLightByte) {
            vLight[i] = tr.identityLightByte;
        }
    }

    // save out the byte packet version
    ((byte *)&iColor)[0] = myftol(vLight[0]);
    ((byte *)&iColor)[1] = myftol(vLight[1]);
    ((byte *)&iColor)[2] = myftol(vLight[2]);
    ((byte *)&iColor)[3] = 0xff;

    return iColor;

#if 0
	int				i;
	dlight_t* dl;
	float			power;
	vec3_t			dir;
	float			d;
	vec3_t			lightDir;
	vec3_t			lightOrigin;
	int				ambientlightInt = 0;
	trRefEntity_t	*ent = backEnd.currentEntity;
	trRefdef_t		*refdef = &backEnd.refdef;

	//
	// trace a sample point down to find ambient light
	//
	if (ent->e.renderfx & RF_LIGHTING_ORIGIN) {
		// seperate lightOrigins are needed so an object that is
		// sinking into the ground can still be lit, and so
		// multi-part models can be lit identically
		VectorCopy(ent->e.lightingOrigin, lightOrigin);
	}
	else {
		VectorCopy(ent->e.origin, lightOrigin);
	}

	// if NOWORLDMODEL, only use dynamic lights (menu system, etc)
	if (!(refdef->rdflags & RDF_NOWORLDMODEL)
		&& tr.world->lightGridData) {
		R_SetupEntityLightingGrid(ent);
	}
	else {
		ent->ambientLight[0] = ent->ambientLight[1] =
			ent->ambientLight[2] = tr.identityLight * 150;
		ent->directedLight[0] = ent->directedLight[1] =
			ent->directedLight[2] = tr.identityLight * 150;
		VectorCopy(tr.sunDirection, ent->lightDir);
	}

	// bonus items and view weapons have a fixed minimum add
	if (1 /* ent->e.renderfx & RF_MINLIGHT */) {
		// give everything a minimum light add
		ent->ambientLight[0] += tr.identityLight * 32;
		ent->ambientLight[1] += tr.identityLight * 32;
		ent->ambientLight[2] += tr.identityLight * 32;
	}

	//
	// modify the light by dynamic lights
	//
	d = VectorLength(ent->directedLight);
	VectorScale(ent->lightDir, d, lightDir);

	for (i = 0; i < refdef->num_dlights; i++) {
		dl = &refdef->dlights[i];
		VectorSubtract(dl->origin, lightOrigin, dir);
		d = VectorNormalize(dir);

		power = DLIGHT_AT_RADIUS * (dl->radius * dl->radius);
		if (d < DLIGHT_MINIMUM_RADIUS) {
			d = DLIGHT_MINIMUM_RADIUS;
		}
		d = power / (d * d);

		VectorMA(ent->directedLight, d, dl->color, ent->directedLight);
		VectorMA(lightDir, d, dir, lightDir);
	}

	// clamp ambient
	for (i = 0; i < 3; i++) {
		if (ent->ambientLight[i] > tr.identityLightByte) {
			ent->ambientLight[i] = tr.identityLightByte;
		}
	}

	// save out the byte packet version
	((byte*)&ambientlightInt)[0] = myftol(ent->ambientLight[0]);
	((byte*)&ambientlightInt)[1] = myftol(ent->ambientLight[1]);
	((byte*)&ambientlightInt)[2] = myftol(ent->ambientLight[2]);
	((byte*)&ambientlightInt)[3] = 0xff;

	// transform the direction to local space
	VectorNormalize(lightDir);
	ent->lightDir[0] = DotProduct(lightDir, ent->e.axis[0]);
	ent->lightDir[1] = DotProduct(lightDir, ent->e.axis[1]);
	ent->lightDir[2] = DotProduct(lightDir, ent->e.axis[2]);

	return ambientlightInt;
#endif
}

/*
===============
RB_SetupEntityGridLighting
===============
*/
void RB_SetupEntityGridLighting()
{
    trRefEntity_t *ent;
    int            iColor;

    if (backEnd.currentEntity->bLightGridCalculated) {
        return;
    }

    for (ent = backEnd.currentEntity; ent->e.parentEntity != ENTITYNUM_NONE;
         ent = &backEnd.refdef.entities[ent->e.parentEntity]) {
        trRefEntity_t *newref = &backEnd.refdef.entities[ent->e.parentEntity];
        if (newref == ent) {
            assert(!"backEnd.refdef.entities[ent->e.parentEntity] refers to itself\n");
            iColor = newref->iGridLighting;
            break;
        }

        if (newref->bLightGridCalculated) {
            iColor = newref->iGridLighting;
            break;
        }
    }

    if (ent->e.parentEntity == ENTITYNUM_NONE) {
        iColor = RB_GetEntityGridLighting();
    }

    ent = backEnd.currentEntity;
    for (;;) {
        ent->bLightGridCalculated = qtrue;
        ent->iGridLighting        = iColor;
        if (ent->e.parentEntity == ENTITYNUM_NONE) {
            break;
        }

        if (ent == &backEnd.refdef.entities[ent->e.parentEntity]) {
            assert(!"backEnd.refdef.entities[ent->e.parentEntity] refers to itself\n");
            break;
        }

        ent = &backEnd.refdef.entities[ent->e.parentEntity];
    }
}

/*
===============
RB_SetupStaticModelGridLighting
===============
*/
void RB_SetupStaticModelGridLighting(trRefdef_t *refdef, cStaticModelUnpacked_t *ent, const vec3_t lightOrigin)
{
    int       iColor;
    int       i;
    dlight_t *dl;
    float     power;
    vec3_t    vLight;
    vec3_t    dir;
    float     d;

    if (ent->bLightGridCalculated) {
        return;
    }
    ent->bLightGridCalculated = qtrue;

    if (!(refdef->rdflags & RDF_NOWORLDMODEL) && tr.world->lightGridData) {
        R_GetLightingGridValue(lightOrigin, vLight);
    } else {
        vLight[0] = vLight[1] = vLight[2] = tr.identityLight * 150.0;
    }

    for (i = 0; i < refdef->num_dlights; i++) {
        dl = &refdef->dlights[i];
        VectorSubtract(dl->origin, lightOrigin, dir);
        d = VectorLengthSquared(dir);

        power = dl->radius * dl->radius;
        if (power >= d) {
            d = dl->radius * 7500.0 / d;
            VectorMA(vLight, d, dl->color, vLight);
        }
    }

    if (tr.overbrightShift) {
        vLight[0] = tr.overbrightMult * vLight[0];
        vLight[1] = tr.overbrightMult * vLight[1];
        vLight[2] = tr.overbrightMult * vLight[2];
    }

    // normalize
    if (vLight[0] > 255.0 || vLight[1] > 255.0 || vLight[2] > 255.0) {
        float scale = 255.0 / Q_max(vLight[0], Q_max(vLight[1], vLight[2]));
        VectorScale(vLight, scale, vLight);
    }

    // clamp ambient
    for (i = 0; i < 3; i++) {
        if (vLight[i] > tr.identityLightByte) {
            vLight[i] = tr.identityLightByte;
        }
    }

    // save out the byte packet version
    ((byte *)&ent->iGridLighting)[0] = myftol(vLight[0]);
    ((byte *)&ent->iGridLighting)[1] = myftol(vLight[1]);
    ((byte *)&ent->iGridLighting)[2] = myftol(vLight[2]);
    ((byte *)&ent->iGridLighting)[3] = 0xff;
}

/*
=============================================================================

LIGHT SAMPLING

=============================================================================
*/

extern cvar_t *r_ambientScale;
extern cvar_t *r_directedScale;

/*
===============
LogLight
===============
*/
static void LogLight(trRefEntity_t *ent)
{
    int max1, max2;

    if (!(ent->e.renderfx & RF_FIRST_PERSON)) {
        return;
    }

    max1 = ent->ambientLight[0];
    if (ent->ambientLight[1] > max1) {
        max1 = ent->ambientLight[1];
    } else if (ent->ambientLight[2] > max1) {
        max1 = ent->ambientLight[2];
    }

    max2 = ent->directedLight[0];
    if (ent->directedLight[1] > max2) {
        max2 = ent->directedLight[1];
    } else if (ent->directedLight[2] > max2) {
        max2 = ent->directedLight[2];
    }

    ri.Printf(PRINT_ALL, "amb:%i  dir:%i\n", max1, max2);
}

int R_LightForPoint(vec3_t point, vec3_t ambientLight, vec3_t directedLight, vec3_t lightDir) {
    // Stub
    return 0;
}
