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
along with Quake III Arena source code; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
===========================================================================
*/
// tr_draw.c -- drawing

#include "tr_local.h"

vec4_t r_colorWhite = { 1.0, 1.0, 1.0, 1.0 };

cvar_t *r_batch2D;

/*
====================================================================

2D BATCHES

The UI and cgame make each 2D pic, string and triangle a surface of its own:
a stage iteration and a draw each. A 2D draw whose shader computes the colors
and texture coordinates of each vertex from that vertex alone
(R_2DShaderBatchable) stays open in tess instead, and the 2D draws that come
next with the same shader, from the same kind of call, join it; it is drawn
once something else is about to run in the renderer (R_End2DBatch). The same
triangles in the same order with the same state: the same pixels. What ends
a batch:
- a draw with another shader, one that isn't batched, a full tess;
- whatever runs the back end or records GPU work outside these draws: it goes
  through R_IssuePendingRenderCommands or R_IssueRenderCommands (Set2DWindow,
  the boxes and lines, registration, the front end's debug drawing, the 3D
  view and the end of the frame);
- what changes the batch's input without them: Draw_SetColor (the 2D color),
  Set2DInitialShaderTime and RE_SetRenderTime (the shader time), RE_Scissor,
  RE_RefreshShaderNoMip (a shader's image), RE_UploadCinematic (an image's
  contents), loading a font's shader, and RE_BeginFrame.
r_batch2D: 1 batches, 2 leaves out DrawBox's boxes that change no pixel
(alpha 0), 0 draws everything as each call comes.

====================================================================
*/

static struct {
	qboolean	batching;	// the surface in tess is a batch
	qboolean	open;		// ... that the next 2D draw may join
	r2dSource_t	source;
} r2d;

/*
================
R_2DShaderBatchable

Whether consecutive 2D draws of this shader can be one surface: one stage
with one texture, no deform, the generic stage iterator, and texture
coordinates and colors that ComputeTexCoords and ComputeColors compute for
each vertex from that vertex alone or from what stays the same while a batch
is open (the 2D color, the shader time): nothing from the entity or the
view, nothing left in tess.svars by an earlier surface (no vertex colors for
glyphs, R_DrawString_sgl gives them none), every color channel written
================
*/
static qboolean R_2DShaderBatchable( const shader_t *shader, r2dSource_t source ) {
	const shaderStage_t		*stage = shader->unfoggedStages[0];
	const textureBundle_t	*bundle;
	qboolean				vertexColors = source == R2D_STREAM;
	qboolean				rgbAlpha;	// the rgbGen writes alpha too
	int						i;

	if ( shader->numUnfoggedPasses != 1 || !stage || !stage->active || shader->numDeforms || shader->isSky
		|| shader->optimalStageIteratorFunc != RB_StageIteratorGeneric ) {
		return qfalse;
	}

	bundle = &stage->bundle[0];
	if ( stage->bundle[1].image[0] || bundle->isLightmap || bundle->vertexLightmap ) {
		return qfalse;
	}
	if ( bundle->tcGen != TCGEN_TEXTURE && bundle->tcGen != TCGEN_IDENTITY && bundle->tcGen != TCGEN_VECTOR ) {
		return qfalse;
	}
	for ( i = 0; i < bundle->numTexMods; i++ ) {
		switch ( bundle->texMods[i].type ) {
		case TMOD_TURBULENT:
		case TMOD_SCROLL:
		case TMOD_SCALE:
		case TMOD_STRETCH:
		case TMOD_TRANSFORM:
			break;
		default:
			return qfalse;
		}
	}

	switch ( stage->rgbGen ) {
	case CGEN_IDENTITY_LIGHTING:
	case CGEN_IDENTITY:
	case CGEN_CONSTANT:
	case CGEN_GLOBAL_COLOR:
	case CGEN_WAVEFORM:
	case CGEN_MULTIPLY_BY_WAVEFORM:
		rgbAlpha = qtrue;
		break;
	case CGEN_EXACT_VERTEX:
	case CGEN_VERTEX:
		if ( !vertexColors ) {
			return qfalse;
		}
		rgbAlpha = qtrue;
		break;
	case CGEN_ONE_MINUS_VERTEX:
		if ( !vertexColors ) {
			return qfalse;
		}
		rgbAlpha = qfalse;
		break;
	default:
		return qfalse;
	}

	switch ( stage->alphaGen ) {
	case AGEN_IDENTITY:		// 255, or what the rgbGen wrote (identity, vertex)
	case AGEN_CONSTANT:
	case AGEN_GLOBAL_ALPHA:
	case AGEN_WAVEFORM:
		return qtrue;
	case AGEN_VERTEX:
	case AGEN_ONE_MINUS_VERTEX:
		return vertexColors;
	case AGEN_SKIP:
		return rgbAlpha;
	default:
		return qfalse;
	}
}

/*
================
R_End2DBatch

Draws the open 2D batch
================
*/
void R_End2DBatch( void ) {
	if ( !r2d.open ) {
		return;
	}
	r2d.open = qfalse;
	r2d.batching = qfalse;
	RB_EndSurface();
}

/*
================
R_Forget2DBatch

Drops the open 2D batch without drawing it: at shutdown, after
R_IssuePendingRenderCommands drew it if the renderer was registered
================
*/
void R_Forget2DBatch( void ) {
	r2d.open = qfalse;
	r2d.batching = qfalse;
}

/*
================
R_IssuePending2DCommands

R_IssuePendingRenderCommands for a 2D draw that may join the open batch: the
queued commands, if any, run after the batch is drawn
================
*/
void R_IssuePending2DCommands( void ) {
	if ( tr.registered && backEndData->commands.used ) {
		R_IssuePendingRenderCommands();
	}
}

/*
================
R_Begin2D

Starts the surface of a 2D draw, or keeps the open batch when the draw can
join it: the same shader and source, room for its vertexes and indexes
================
*/
void R_Begin2D( shader_t *shader, r2dSource_t source, int numVertexes, int numIndexes ) {
	if ( r2d.open ) {
		if ( tess.shader == shader && r2d.source == source && tess.numVertexes + numVertexes < SHADER_MAX_VERTEXES
			&& tess.numIndexes + numIndexes < SHADER_MAX_INDEXES ) {
			return;
		}
		R_End2DBatch();
	}

	RB_BeginSurface( shader );
	r2d.source = source;
	r2d.batching = ( r_batch2D->integer & 1 ) && R_2DShaderBatchable( shader, source );
}

/*
================
R_End2D

Ends a 2D draw: its surface is drawn, or stays open when it is a batch
================
*/
void R_End2D( void ) {
	if ( r2d.batching ) {
		r2d.open = qtrue;
		return;
	}
	RB_EndSurface();
}

/*
================
R_End2DStream

Ends a 2D draw made with RB_Vertex2f and friends: the triangle strip
RB_StreamEnd makes of its vertexes, from its first one
================
*/
static void R_End2DStream( int firstVertex ) {
	int numVertexes;
	int i;

	numVertexes = tess.numVertexes - firstVertex;
	if ( numVertexes <= 2 ) {
		tess.numVertexes = firstVertex;
		R_End2D();
		return;
	}

	for ( i = 0; i < numVertexes - 2; i++ ) {
		tess.indexes[tess.numIndexes + 0] = firstVertex + ( i & 1 ) + i;
		tess.indexes[tess.numIndexes + 1] = firstVertex + i - ( ( i & 1 ) - 1 );
		tess.indexes[tess.numIndexes + 2] = firstVertex + i + 2;
		tess.numIndexes += 3;
	}

	R_End2D();
}

/*
================
Draw_SetColor
================
*/
void Draw_SetColor(const vec4_t rgba) {
#if 1
	byte color[4];

	if (!rgba) {
		rgba = r_colorWhite;
	}

	color[0] = (byte)(rgba[0] * tr.identityLightByte);
	color[1] = (byte)(rgba[1] * tr.identityLightByte);
	color[2] = (byte)(rgba[2] * tr.identityLightByte);
	color[3] = (byte)(rgba[3] * 255.0);
	if (memcmp(color, backEnd.color2D, sizeof(color))) {
		// the open batch is drawn with the color it was made with
		R_End2DBatch();
		Com_Memcpy(backEnd.color2D, color, sizeof(color));
	}
	RB_SetColor4ubv(backEnd.color2D);
#else
	RE_SetColor(rgba);
#endif
}

/*
================
Draw_StretchPic
================
*/
void Draw_StretchPic(float x, float y, float w, float h, float s1, float t1, float s2, float t2, qhandle_t hShader) {
#if 1
	shader_t* shader;
	int firstVertex;

	R_IssuePending2DCommands();

	if (hShader) {
		shader = R_GetShaderByHandle(hShader);
	}
	else {
		shader = tr.defaultShader;
	}

	if (w <= 0) {
		w = shader->unfoggedStages[0]->bundle[0].image[0]->width;
		h = shader->unfoggedStages[0]->bundle[0].image[0]->height;
	}

	// draw the pic
	RB_Color4f(backEnd.color2D[0], backEnd.color2D[1], backEnd.color2D[2], backEnd.color2D[3]);
	R_Begin2D(shader, R2D_STREAM, 4, 6);
	firstVertex = tess.numVertexes;

	RB_Texcoord2f(s1, t1);
	RB_Vertex2f(x, y);

	RB_Texcoord2f(s2, t1);
	RB_Vertex2f(x + w, y);

	RB_Texcoord2f(s1, t2);
	RB_Vertex2f(x, y + h);

	RB_Texcoord2f(s2, t2);
	RB_Vertex2f(x + w, y + h);

	R_End2DStream(firstVertex);
#else
	RE_StretchPic(x, y, w, h, s1, t1, s2, t2, hShader);
#endif
}

/*
================
Draw_StretchPic2
================
*/
void Draw_StretchPic2(float x, float y, float w, float h, float s1, float t1, float s2, float t2, float sx, float sy, qhandle_t hShader) {
	shader_t* shader;
	float halfWidth, halfHeight;
	float scaledWidth1, scaledHeight1;
	float scaledWidth2, scaledHeight2;
	int firstVertex;

	R_IssuePending2DCommands();

	if (hShader) {
		shader = R_GetShaderByHandle(hShader);
	}
	else {
		shader = tr.defaultShader;
	}

	if (w <= 0) {
		w = shader->unfoggedStages[0]->bundle[0].image[0]->width;
		h = shader->unfoggedStages[0]->bundle[0].image[0]->height;
	}

	halfWidth = w * 0.5f;
	halfHeight = h * 0.5f;
	scaledWidth1 = halfWidth * sy;
	scaledHeight1 = halfHeight * sx;
	scaledWidth2 = halfWidth * sx;
	scaledHeight2 = halfHeight * sy;

	// draw the pic
	RB_Color4f(backEnd.color2D[0], backEnd.color2D[1], backEnd.color2D[2], backEnd.color2D[3]);
	R_Begin2D(shader, R2D_STREAM, 4, 6);
	firstVertex = tess.numVertexes;

	RB_Texcoord2f(s1, t1);
	RB_Vertex3f(x + halfWidth - (scaledWidth2 + -scaledHeight2), y + halfWidth - scaledHeight1 - scaledWidth1, 0);

	RB_Texcoord2f(t2, t1);
	RB_Vertex3f(scaledWidth2 - -scaledHeight2 + x + halfWidth, scaledWidth1 - scaledHeight1 + y + halfWidth, 0);

	RB_Texcoord2f(s1, s2);
	RB_Vertex3f(x+ halfWidth - (scaledWidth2 + scaledHeight2), scaledHeight1 - scaledWidth1 + y + halfWidth, 0);

	RB_Texcoord2f(t2, s2);
	RB_Vertex3f(scaledWidth2 - scaledHeight2 + x + halfWidth, scaledWidth1 + scaledHeight1 + y + halfWidth, 0);

	R_End2DStream(firstVertex);
}


/*
================
Draw_TilePic
================
*/
void Draw_TilePic(float x, float y, float w, float h, qhandle_t hShader) {
	shader_t* shader;
	float		picw, pich;
	int			firstVertex;

	R_IssuePending2DCommands();

	if (hShader) {
		shader = R_GetShaderByHandle(hShader);
	}
	else {
		shader = tr.defaultShader;
	}

	if (w <= 0) {
		w = shader->unfoggedStages[0]->bundle[0].image[0]->width;
		h = shader->unfoggedStages[0]->bundle[0].image[0]->height;
	}

	picw = shader->unfoggedStages[0]->bundle[0].image[0]->uploadWidth;
	pich = shader->unfoggedStages[0]->bundle[0].image[0]->uploadHeight;

	// draw the pic
	RB_Color4f(backEnd.color2D[0], backEnd.color2D[1], backEnd.color2D[2], backEnd.color2D[3]);

	R_Begin2D(shader, R2D_STREAM, 4, 6);
	firstVertex = tess.numVertexes;

	RB_Texcoord2f(x / picw, y / pich);
	RB_Vertex2f(x, y);

	RB_Texcoord2f((x + w) / picw, y / pich);
	RB_Vertex2f(x + w, y);

	RB_Texcoord2f(x / picw, (y + h) / pich);
	RB_Vertex2f(x, y + h);

	RB_Texcoord2f((x + w) / picw, (y + h) / pich);
	RB_Vertex2f(x + w, y + h);

	R_End2DStream(firstVertex);
}

/*
================
Draw_TilePicOffset
================
*/
void Draw_TilePicOffset(float x, float y, float w, float h, qhandle_t hShader, int offsetX, int offsetY) {
	shader_t* shader;
	float		picw, pich;
	int			firstVertex;

	R_IssuePending2DCommands();

	if (hShader) {
		shader = R_GetShaderByHandle(hShader);
	}
	else {
		shader = tr.defaultShader;
	}

	if (w <= 0) {
		w = shader->unfoggedStages[0]->bundle[0].image[0]->width;
		h = shader->unfoggedStages[0]->bundle[0].image[0]->height;
	}

	picw = shader->unfoggedStages[0]->bundle[0].image[0]->uploadWidth;
	pich = shader->unfoggedStages[0]->bundle[0].image[0]->uploadHeight;

	// draw the pic
	RB_Color4f(backEnd.color2D[0], backEnd.color2D[1], backEnd.color2D[2], backEnd.color2D[3]);

	R_Begin2D(shader, R2D_STREAM, 4, 6);
	firstVertex = tess.numVertexes;

	RB_Texcoord2f(x / picw, y / pich);
	RB_Vertex2f(x + offsetX, y + offsetY);

	RB_Texcoord2f((x + w) / picw, y / pich);
	RB_Vertex2f(x + offsetX + w, y + offsetY);

	RB_Texcoord2f(x / picw, (y + h) / pich);
	RB_Vertex2f(x + offsetX, y + offsetY + h);

	RB_Texcoord2f((x + w) / picw, (y + h) / pich);
	RB_Vertex2f(x + offsetX + w, y + offsetY + h);

	R_End2DStream(firstVertex);
}

/*
================
Draw_TrianglePic
================
*/
void Draw_TrianglePic(const vec2_t vPoints[3], const vec2_t vTexCoords[3], qhandle_t hShader) {
	int			i;
	shader_t* shader;
	int			firstVertex;

	R_IssuePending2DCommands();

	if (hShader) {
		shader = R_GetShaderByHandle(hShader);
	}
	else {
		shader = tr.defaultShader;
	}

	// draw the pic
	RB_Color4f(backEnd.color2D[0], backEnd.color2D[1], backEnd.color2D[2], backEnd.color2D[3]);

	R_Begin2D(shader, R2D_STREAM, 3, 3);
	firstVertex = tess.numVertexes;

	for (i = 0; i < 3; i++) {
		RB_Texcoord2f(vTexCoords[i][0], vTexCoords[i][1]);
		RB_Vertex2f(vPoints[i][0], vPoints[i][1]);
	}

	R_End2DStream(firstVertex);
}

/*
================
RE_DrawBackground_TexSubImage
================
*/
void RE_DrawBackground_TexSubImage(int cols, int rows, int bgr, byte* data) {
	int		w, h;
	int		i;
	byte	*rgba;

	w = glConfig.vidWidth;
	h = glConfig.vidHeight;

	R_IssuePendingRenderCommands();
	vk_finish();

	// expand the 24-bit RGB or BGR data to RGBA
	rgba = ri.Hunk_AllocateTempMemory(cols * rows * 4);
	for (i = 0; i < cols * rows; i++) {
		if (bgr) {
			rgba[i * 4 + 0] = data[i * 3 + 2];
			rgba[i * 4 + 2] = data[i * 3 + 0];
		} else {
			rgba[i * 4 + 0] = data[i * 3 + 0];
			rgba[i * 4 + 2] = data[i * 3 + 2];
		}
		rgba[i * 4 + 1] = data[i * 3 + 1];
		rgba[i * 4 + 3] = 255;
	}

	R_Bind(tr.scratchImage);
	R_UploadScratchImage(tr.scratchImage, cols, rows, rgba, qtrue);
	ri.Hunk_FreeTempMemory(rgba);

	// bypasses the tracked state, like the original
	vk_set_cull(CULL_NONE);
	vk_set_state(glState.glStateBits | GLS_DEPTHTEST_DISABLE);
	R_EnableTexture(qtrue);

	RB_ImmBegin(IMM_QUADS);

	RB_ImmTexCoord2f(0.5 / (float)cols, ((float)rows - 0.5) / rows);
	RB_ImmVertex2f(0, 0);

	RB_ImmTexCoord2f(((float)cols - 0.5) / cols, ((float)rows - 0.5) / rows);
	RB_ImmVertex2f(w, 0);

	RB_ImmTexCoord2f(((float)cols - 0.5) / cols, 0.5 / (float)rows);
	RB_ImmVertex2f(w, h);

	RB_ImmTexCoord2f(0.5 / (float)rows, 0.5 / (float)rows);
	RB_ImmVertex2f(0, h);

	RB_ImmEnd();
}

/*
================
RE_DrawBackground_DrawPixels

There is no pixel drawing path, the image is drawn as a textured quad
================
*/
void RE_DrawBackground_DrawPixels(int cols, int rows, int bgr, byte* data) {
	RE_DrawBackground_TexSubImage(cols, rows, bgr, data);
}

/*
================
AddBox
================
*/
void AddBox(float x, float y, float w, float h) {
	R_IssuePendingRenderCommands();

	RB_SetColor4ubv(backEnd.color2D);
	R_EnableTexture(qfalse);
	R_State(GLS_DEPTHTEST_DISABLE | GLS_SRCBLEND_ONE | GLS_DSTBLEND_ONE);

	RB_ImmBegin(IMM_QUADS);

	RB_ImmVertex2f(x, y);
	RB_ImmVertex2f(x + w, y);
	RB_ImmVertex2f(x + w, y + h);
	RB_ImmVertex2f(x, y + h);

	RB_ImmEnd();

	R_EnableTexture(qtrue);
}

/*
================
DrawBox
================
*/
void DrawBox(float x, float y, float w, float h) {
	R_IssuePendingRenderCommands();

	RB_SetColor4ubv(backEnd.color2D);
	R_EnableTexture(qfalse);
	R_State(GLS_DEPTHTEST_DISABLE | GLS_DSTBLEND_ONE_MINUS_SRC_ALPHA | GLS_SRCBLEND_SRC_ALPHA);

	// with alpha 0 the box blends to what is there (the UI's widgets draw
	// their backgrounds and borders this way, most of them transparent: 37 of
	// 40 boxes a frame in matches); r_measureOverdraw counts its fragments
	if (!(r_batch2D->integer & 2) || backEnd.color2D[3] || r_measureOverdraw->integer) {
		RB_ImmBegin(IMM_QUADS);

		RB_ImmVertex2f(x, y);
		RB_ImmVertex2f(x + w, y);
		RB_ImmVertex2f(x + w, y + h);
		RB_ImmVertex2f(x, y + h);

		RB_ImmEnd();
	}

	R_EnableTexture(qtrue);
}

/*
================
DrawBoxes

DrawBox for count boxes (x, y, width, height) in one draw, in the color
given (taken as Draw_SetColor takes it); the 2D color stays (the crosshair)
================
*/
void DrawBoxes(const float (*boxes)[4], int count, const float *color) {
	byte	rgba[4];
	int		i;

	R_IssuePendingRenderCommands();

	rgba[0] = (byte)(color[0] * tr.identityLightByte);
	rgba[1] = (byte)(color[1] * tr.identityLightByte);
	rgba[2] = (byte)(color[2] * tr.identityLightByte);
	rgba[3] = (byte)(color[3] * 255.0);
	RB_SetColor4ubv(rgba);
	R_EnableTexture(qfalse);
	R_State(GLS_DEPTHTEST_DISABLE | GLS_DSTBLEND_ONE_MINUS_SRC_ALPHA | GLS_SRCBLEND_SRC_ALPHA);

	RB_ImmBegin(IMM_QUADS);

	for (i = 0; i < count; i++) {
		const float *b = boxes[i];

		RB_ImmVertex2f(b[0], b[1]);
		RB_ImmVertex2f(b[0] + b[2], b[1]);
		RB_ImmVertex2f(b[0] + b[2], b[1] + b[3]);
		RB_ImmVertex2f(b[0], b[1] + b[3]);
	}

	RB_ImmEnd();

	RB_SetColor4ubv(backEnd.color2D);
	R_EnableTexture(qtrue);
}

/*
================
DrawLineLoop
================
*/
void DrawLineLoop(const vec2_t* points, int count, int stipple_factor, int stipple_mask) {
	int		i;

	R_IssuePendingRenderCommands();

	R_EnableTexture(qfalse);

	if (stipple_factor) {
		vk_set_line_stipple(stipple_factor, stipple_mask);
	}

	RB_ImmBegin(IMM_LINE_LOOP);

	for (i = 0; i < count; i++) {
		RB_ImmVertex2f(points[i][0], points[i][1]);
	}

	RB_ImmEnd();

	R_EnableTexture(qtrue);

	if (stipple_factor) {
		vk_set_line_stipple(0, 0xFFFF);
	}
}

/*
================
Set2DWindow
================
*/
void Set2DWindow(int x, int y, int w, int h, float left, float right, float bottom, float top, float n, float f) {
	float	matrix[16];

	R_IssuePendingRenderCommands();
	// the temporal path's scene goes into the frame before the 2D drawing
	RB_TemporalEndScene();
	RB_SetViewport(x, y, w, h);
	RB_SetScissor(x, y, w, h);
	RB_OrthoMatrix(matrix, left, right, bottom, top, n, f);
	RB_SetProjection(matrix);

	RB_LoadIdentity(matrix);
	RB_SetModelview(matrix);
	R_State(GLS_DEPTHTEST_DISABLE | GLS_DSTBLEND_ONE_MINUS_SRC_ALPHA | GLS_SRCBLEND_SRC_ALPHA);
	vk_set_cull(CULL_NONE);
	vk_set_clip_plane(NULL);
	// Make sure to disable the fog to avoid messing up with the UI
	vk_set_fog(qfalse);
	// R_State can enable it again for 2D stages; with a start of 0 the
	// fog has no effect at the 2D depth
	RB_SetFogRange(0, glState.fogEnd);

	if (r_reset_tc_array->integer) {
		RB_EnableTexCoordArray(qfalse);
	}

	if (!backEnd.in2D)
	{
		backEnd.refdef.time = ri.Milliseconds();
		backEnd.in2D = qtrue;
		backEnd.refdef.floatTime = backEnd.refdef.time / 1000.0;
        backEnd.shaderStartTime = 0; 
	}
}

/*
================
RE_Scissor
================
*/
void RE_Scissor(int x, int y, int width, int height) {
	R_End2DBatch();
	RB_SetScissor(x, y, width, height);
}

/*
================
Set2DInitialShaderTime
================
*/
void Set2DInitialShaderTime(float startTime)
{
	if (backEnd.in2D)
	{
		if (backEnd.shaderStartTime != startTime) {
			R_End2DBatch();
		}
		backEnd.shaderStartTime = startTime;
	}
}
