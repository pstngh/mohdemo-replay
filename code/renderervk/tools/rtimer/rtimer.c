/*
 * renderer_timed.so -- times the renderer the game runs, whichever it is
 *
 * Loaded with `cl_renderer timed`, it loads renderer_$MOH_TIMED_RENDERER.so
 * (default vulkan) from its own directory, passes the engine's imports
 * through and returns the real renderer's exports with the per-frame ones
 * wrapped: BeginFrame, RenderScene, EndFrame, and the 2D exports
 * (Set2DWindow, DrawStretchPic, DrawString, ...), which in OpenMoHAA run
 * the queued render commands (R_IssuePendingRenderCommands) and draw at
 * once: the UI calls Set2DWindow before the 3D view, so that is where the
 * back end starts the frame (and Vulkan waits for the frame fence). While
 * the cvar rt_record is 1 it records, per frame (BeginFrame to BeginFrame),
 * the wall clock time spent in those calls with a microsecond clock; when
 * rt_record goes back to 0 it prints one line:
 *
 *   rtimer: <frames> frames, ms per frame: interval <mean>, renderer <mean>
 *     <median> <p99>, scene <mean> <median>, endframe <mean> <median> <p99>,
 *     beginframe <mean>, interval <median> <p99> <max> <standard deviation>,
 *     2d <mean>, load <ms>, interval p99.9 <ms>
 *
 * and, when the environment variable RTIMER_FRAMES names a file, writes
 * every frame to it (microseconds): its start, its interval (to the next
 * BeginFrame) and the time in BeginFrame, RenderScene, the 2D exports,
 * EndFrame and the registration exports, the rest of the time from
 * BeginFrame to EndFrame (cgame: the UI and cgame) and from EndFrame's
 * return to the next BeginFrame (engine: sound, the frame limiter's sleep,
 * events, the local server's frame). These are the first columns of the
 * Vulkan renderer's frame log (r_vkFrameLog, vk_framelog.c), measured the
 * same way; tools/framelog.py reads both. The header gives the
 * CLOCK_MONOTONIC time of the first frame, the clock of libstacks.so's
 * samples (framelog.py --stacks puts them into frames).
 *
 * "renderer" is the sum: the time the main thread spends in the renderer
 * (front end in RenderScene, back end in EndFrame and the 2D calls, driver
 * work done on the main thread, waits for the GPU and presentation).
 * "load" is the time spent in the registration exports (BeginRegistration,
 * LoadWorld, RegisterModel/Shader/Skin/Font, EndRegistration, ...) since
 * the renderer was loaded. Unlike com_speeds (whole milliseconds, read
 * right after the frame limiter's millisecond tick, and bk only counts the
 * last batch of commands of a frame) it is exact and complete, and it
 * measures every renderer build the same way. tools/perf.py uses it when
 * renderer_timed.so is in MOH_BINDIR; build it with tools/rtimer/build.sh.
 */

#define _GNU_SOURCE		// dladdr
#include <dlfcn.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "../../../qcommon/q_shared.h"
#include "../../../renderercommon/tr_public.h"

#define MAX_FRAMES	200000

typedef struct {
	double	start;		// BeginFrame, since the first frame recorded
	float	interval;	// to the next BeginFrame
	float	begin, scene, end, twoD, load;
	float	engine;		// from EndFrame's return to the next BeginFrame, registration left out
} rtFrame_t;

static refimport_t	rt_ri;
static refexport_t	rt_real;		// the real renderer's exports
static refexport_t	rt_export;		// what the engine gets
static void			*rt_lib;
static cvar_t		*rt_record;

static rtFrame_t	*rt_frames;
static int			rt_numFrames;
static int			rt_recording;
static int			rt_inFrame;		// rt_cur holds a frame begun
static double		rt_curStart;	// its BeginFrame
static double		rt_lastEnd;		// EndFrame's return
static double		rt_exportsAtEnd;	// rt_cur's time in the exports then
static double		rt_firstStart;	// BeginFrame of the first frame recorded
static rtFrame_t	rt_cur;
static double		rt_load;		// ms in the registration exports
static int			rt_depth;		// wrapped exports being run (see RT_TIME)

static double RT_Now( void ) {
	struct timespec ts;

	clock_gettime( CLOCK_MONOTONIC, &ts );
	return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static float rt_p999;	// 99.9th percentile of the last RT_Stats

static int RT_Compare( const void *a, const void *b ) {
	float fa = *(const float *)a, fb = *(const float *)b;

	return fa < fb ? -1 : fa > fb;
}

static double RT_Exports( const rtFrame_t *f ) {
	return f->begin + f->scene + f->end + f->twoD + f->load;
}

static void RT_WriteFrames( void ) {
	const char *path = getenv( "RTIMER_FRAMES" );
	FILE *f;
	int i;

	if ( !path || !*path || !( f = fopen( path, "w" ) ) ) {
		return;
	}
	fprintf( f, "# rtimer frames 1: %d frames (code/renderervk/tools/rtimer/rtimer.c)\n", rt_numFrames );
	// libstacks.so's samples have the same clock (tools/framelog.py --stacks)
	fprintf( f, "# clock %.3f: CLOCK_MONOTONIC ms at the start of frame 0\n", rt_firstStart );
	fprintf( f, "# columns, microseconds: frame start interval begin scene 2d end load cgame engine\n" );
	for ( i = 0; i < rt_numFrames; i++ ) {
		const rtFrame_t *r = &rt_frames[i];
		double cgame = r->interval - RT_Exports( r ) - r->engine;

		fprintf( f, "%d %.0f %.0f %.0f %.0f %.0f %.0f %.0f %.0f %.0f\n", i, r->start * 1000, r->interval * 1000.0,
			r->begin * 1000.0, r->scene * 1000.0, r->twoD * 1000.0, r->end * 1000.0, r->load * 1000.0,
			( cgame > 0 ? cgame : 0 ) * 1000.0, r->engine * 1000.0 );
	}
	fclose( f );
}

static void RT_Stats( size_t offset, float *mean, float *median, float *p99, float *max, float *stddev ) {
	float *v = malloc( sizeof( float ) * rt_numFrames );
	double sum = 0, sum2 = 0;
	int i;

	for ( i = 0; i < rt_numFrames; i++ ) {
		v[i] = *(const float *)( (const char *)&rt_frames[i] + offset );
		sum += v[i];
		sum2 += (double)v[i] * v[i];
	}
	qsort( v, rt_numFrames, sizeof( float ), RT_Compare );
	*mean = sum / rt_numFrames;
	*median = v[rt_numFrames / 2];
	*p99 = v[rt_numFrames * 99 / 100];
	*max = v[rt_numFrames - 1];
	rt_p999 = v[rt_numFrames * 999 / 1000];
	*stddev = sqrt( Q_max( sum2 / rt_numFrames - *mean * *mean, 0 ) );
	free( v );
}

static void RT_Report( void ) {
	float m[6], med[6], p99[6], max[6], sd[6], p999;
	int i;

	if ( !rt_numFrames ) {
		return;
	}
	RT_WriteFrames();
	RT_Stats( offsetof( rtFrame_t, interval ), &m[0], &med[0], &p99[0], &max[0], &sd[0] );
	p999 = rt_p999;
	RT_Stats( offsetof( rtFrame_t, scene ), &m[1], &med[1], &p99[1], &max[1], &sd[1] );
	RT_Stats( offsetof( rtFrame_t, end ), &m[2], &med[2], &p99[2], &max[2], &sd[2] );
	RT_Stats( offsetof( rtFrame_t, begin ), &m[3], &med[3], &p99[3], &max[3], &sd[3] );
	RT_Stats( offsetof( rtFrame_t, twoD ), &m[5], &med[5], &p99[5], &max[5], &sd[5] );
	// then the sum, in place of beginframe
	for ( i = 0; i < rt_numFrames; i++ ) {
		rt_frames[i].begin += rt_frames[i].scene + rt_frames[i].end + rt_frames[i].twoD;
	}
	RT_Stats( offsetof( rtFrame_t, begin ), &m[4], &med[4], &p99[4], &max[4], &sd[4] );

	rt_ri.Printf( PRINT_ALL, "rtimer: %d frames, ms per frame: interval %.4f, renderer %.4f %.4f %.4f, scene %.4f %.4f, endframe %.4f %.4f %.4f, beginframe %.4f,"
		" interval %.4f %.4f %.4f %.4f, 2d %.4f, load %.1f, interval p99.9 %.4f\n",
		rt_numFrames, m[0], m[4], med[4], p99[4], m[1], med[1], m[2], med[2], p99[2], m[3], med[0], p99[0], max[0], sd[0],
		m[5], rt_load, p999 );
	rt_numFrames = 0;
}

// the engine unloads renderer_timed.so after Shutdown; the real renderer
// goes with it, so that a vid_restart starts it from scratch as usual
static void RT_Shutdown( qboolean destroyWindow ) {
	rt_real.Shutdown( destroyWindow );
	if ( destroyWindow && rt_lib ) {
		dlclose( rt_lib );
		rt_lib = NULL;
	}
}

static void RT_BeginFrame( stereoFrame_t stereoFrame ) {
	double t0 = RT_Now(), t1;

	// the frame that just ended; registration between frames (map loads)
	// isn't engine time
	if ( rt_inFrame ) {
		double engine = t0 - rt_lastEnd - ( RT_Exports( &rt_cur ) - rt_exportsAtEnd );

		rt_cur.interval = t0 - rt_curStart;
		rt_cur.engine = engine > 0 ? engine : 0;
		if ( rt_recording && rt_numFrames < MAX_FRAMES ) {
			if ( !rt_numFrames ) {
				rt_firstStart = rt_curStart;
			}
			rt_cur.start = rt_curStart - rt_firstStart;
			rt_frames[rt_numFrames++] = rt_cur;
		}
	}
	if ( !rt_record ) {
		rt_record = rt_ri.Cvar_Get( "rt_record", "0", 0 );
	}
	if ( rt_record->integer && !rt_recording ) {
		rt_numFrames = 0;
	} else if ( !rt_record->integer && rt_recording ) {
		RT_Report();
	}
	rt_recording = rt_record->integer != 0;

	memset( &rt_cur, 0, sizeof( rt_cur ) );
	rt_curStart = t0;
	rt_inFrame = 1;

	// not nested in anything; an error that jumped out of an export left
	// rt_depth behind
	rt_depth = 1;
	rt_real.BeginFrame( stereoFrame );
	rt_depth = 0;
	t1 = RT_Now();
	rt_cur.begin += t1 - t0;
}

// times call into rt_cur.field unless it runs inside another wrapped
// export: the registration exports call the engine's TIKI code, which
// registers shaders through the exports again, and nested time would count
// twice
#define RT_TIME( field, call ) { \
	if ( rt_depth++ ) { \
		call; \
	} else { \
		double t0 = RT_Now(); \
		call; \
		t0 = RT_Now() - t0; \
		rt_cur.field += t0; \
		RT_LOAD_TOTAL_##field( t0 ) \
	} \
	rt_depth--; \
}
#define RT_LOAD_TOTAL_scene( t )
#define RT_LOAD_TOTAL_twoD( t )
#define RT_LOAD_TOTAL_load( t ) rt_load += t;

static void RT_RenderScene( const refdef_t *fd ) {
	RT_TIME( scene, rt_real.RenderScene( fd ) )
}

static void RT_EndFrame( int *frontEndMsec, int *backEndMsec ) {
	double t0 = RT_Now();

	rt_depth++;
	rt_real.EndFrame( frontEndMsec, backEndMsec );
	rt_depth--;
	rt_lastEnd = RT_Now();
	rt_cur.end += rt_lastEnd - t0;
	rt_exportsAtEnd = RT_Exports( &rt_cur );
}

// the 2D exports: time added to rt_cur.twoD
#define RT_2D( call ) RT_TIME( twoD, call )
static void RT_Set2DWindow( int x, int y, int w, int h, float left, float right, float bottom, float top, float n, float f ) {
	RT_2D( rt_real.Set2DWindow( x, y, w, h, left, right, bottom, top, n, f ) )
}
static void RT_DrawStretchPic( float x, float y, float w, float h, float s1, float t1, float s2, float t2, qhandle_t hShader ) {
	RT_2D( rt_real.DrawStretchPic( x, y, w, h, s1, t1, s2, t2, hShader ) )
}
static void RT_DrawStretchPic2( float x, float y, float w, float h, float s1, float t1, float s2, float t2, float sx, float sy, qhandle_t hShader ) {
	RT_2D( rt_real.DrawStretchPic2( x, y, w, h, s1, t1, s2, t2, sx, sy, hShader ) )
}
static void RT_DrawTilePic( float x, float y, float w, float h, qhandle_t hShader ) {
	RT_2D( rt_real.DrawTilePic( x, y, w, h, hShader ) )
}
static void RT_DrawTilePicOffset( float x, float y, float w, float h, qhandle_t hShader, int offsetX, int offsetY ) {
	RT_2D( rt_real.DrawTilePicOffset( x, y, w, h, hShader, offsetX, offsetY ) )
}
static void RT_DrawTrianglePic( const vec2_t vPoints[3], const vec2_t vTexCoords[3], qhandle_t hShader ) {
	RT_2D( rt_real.DrawTrianglePic( vPoints, vTexCoords, hShader ) )
}
static void RT_DrawBackground( int cols, int rows, int bgr, uint8_t *data ) {
	RT_2D( rt_real.DrawBackground( cols, rows, bgr, data ) )
}
static void RT_DrawBox( float x, float y, float w, float h ) {
	RT_2D( rt_real.DrawBox( x, y, w, h ) )
}
static void RT_AddBox( float x, float y, float w, float h ) {
	RT_2D( rt_real.AddBox( x, y, w, h ) )
}
static void RT_DrawBoxes( const float (*boxes)[4], int count, const float *color ) {
	RT_2D( rt_real.DrawBoxes( boxes, count, color ) )
}
static void RT_Scissor( int x, int y, int width, int height ) {
	RT_2D( rt_real.Scissor( x, y, width, height ) )
}
static void RT_DrawLineLoop( const vec2_t *points, int count, int stippleFactor, int stippleMask ) {
	RT_2D( rt_real.DrawLineLoop( points, count, stippleFactor, stippleMask ) )
}
static void RT_DrawString( fontheader_t *font, const char *text, float x, float y, int maxLen, const float *pvVirtualScreen ) {
	RT_2D( rt_real.DrawString( font, text, x, y, maxLen, pvVirtualScreen ) )
}
static void RT_DrawStretchRaw( int x, int y, int w, int h, int cols, int rows, int components, const byte *data ) {
	RT_2D( rt_real.DrawStretchRaw( x, y, w, h, cols, rows, components, data ) )
}

// the registration exports: time added to rt_load
#define RT_LOAD( type, call ) { type r; RT_TIME( load, r = call ) return r; }
#define RT_LOAD_VOID( call ) RT_TIME( load, call )
static void RT_BeginRegistration( glconfig_t *config ) {
	RT_LOAD_VOID( rt_real.BeginRegistration( config ) )
}
static void RT_LoadWorld( const char *name ) {
	RT_LOAD_VOID( rt_real.LoadWorld( name ) )
}
static void RT_EndRegistration( void ) {
	RT_LOAD_VOID( rt_real.EndRegistration() )
}
static qhandle_t RT_RegisterModel( const char *name ) {
	RT_LOAD( qhandle_t, rt_real.RegisterModel( name ) )
}
static qhandle_t RT_RegisterSkin( const char *name ) {
	RT_LOAD( qhandle_t, rt_real.RegisterSkin( name ) )
}
static qhandle_t RT_RegisterShader( const char *name ) {
	RT_LOAD( qhandle_t, rt_real.RegisterShader( name ) )
}
static qhandle_t RT_RegisterShaderNoMip( const char *name ) {
	RT_LOAD( qhandle_t, rt_real.RegisterShaderNoMip( name ) )
}
static qhandle_t RT_RegisterServerModel( const char *name ) {
	RT_LOAD( qhandle_t, rt_real.RegisterServerModel( name ) )
}
static qhandle_t RT_RefreshShaderNoMip( const char *name ) {
	RT_LOAD( qhandle_t, rt_real.RefreshShaderNoMip( name ) )
}
static void RT_RegisterFont( const char *fontName, int pointSize, fontInfo_t *font ) {
	RT_LOAD_VOID( rt_real.RegisterFont( fontName, pointSize, font ) )
}
static fontheader_t *RT_LoadFont( const char *name ) {
	RT_LOAD( fontheader_t *, rt_real.LoadFont( name ) )
}

Q_EXPORT refexport_t *QDECL GetRefAPI( int apiVersion, refimport_t *rimp ) {
	char path[4096], *slash;
	const char *name;
	Dl_info info;
	GetRefAPI_t getRefAPI;
	refexport_t *real;

	rt_ri = *rimp;

	name = getenv( "MOH_TIMED_RENDERER" );
	if ( !name || !*name ) {
		name = "vulkan";
	}
	if ( !dladdr( (void *)GetRefAPI, &info ) || strlen( info.dli_fname ) >= sizeof( path ) - 64 ) {
		rimp->Printf( PRINT_ALL, "rtimer: can't find its own path\n" );
		return NULL;
	}
	strcpy( path, info.dli_fname );
	slash = strrchr( path, '/' );
	snprintf( slash ? slash + 1 : path, sizeof( path ) - ( slash ? slash + 1 - path : 0 ), "renderer_%s.so", name );

	rt_lib = dlopen( path, RTLD_NOW | RTLD_LOCAL );
	if ( !rt_lib ) {
		rimp->Printf( PRINT_ALL, "rtimer: %s\n", dlerror() );
		return NULL;
	}
	getRefAPI = (GetRefAPI_t)dlsym( rt_lib, "GetRefAPI" );
	if ( !getRefAPI ) {
		rimp->Printf( PRINT_ALL, "rtimer: %s has no GetRefAPI\n", path );
		return NULL;
	}
	real = getRefAPI( apiVersion, rimp );
	if ( !real ) {
		return NULL;
	}
	rimp->Printf( PRINT_ALL, "rtimer: timing %s\n", path );

	if ( !rt_frames ) {
		rt_frames = malloc( sizeof( rtFrame_t ) * MAX_FRAMES );
	}
	rt_real = *real;
	rt_export = *real;
	rt_export.Shutdown = RT_Shutdown;
	rt_export.BeginFrame = RT_BeginFrame;
	rt_export.RenderScene = RT_RenderScene;
	rt_export.EndFrame = RT_EndFrame;
	rt_export.Set2DWindow = RT_Set2DWindow;
	rt_export.DrawStretchPic = RT_DrawStretchPic;
	rt_export.DrawStretchPic2 = RT_DrawStretchPic2;
	rt_export.DrawTilePic = RT_DrawTilePic;
	rt_export.DrawTilePicOffset = RT_DrawTilePicOffset;
	rt_export.DrawTrianglePic = RT_DrawTrianglePic;
	rt_export.DrawBackground = RT_DrawBackground;
	rt_export.DrawBox = RT_DrawBox;
	rt_export.AddBox = RT_AddBox;
	rt_export.DrawBoxes = RT_DrawBoxes;
	rt_export.Scissor = RT_Scissor;
	rt_export.DrawLineLoop = RT_DrawLineLoop;
	rt_export.DrawString = RT_DrawString;
	rt_export.DrawStretchRaw = RT_DrawStretchRaw;
	rt_export.BeginRegistration = RT_BeginRegistration;
	rt_export.LoadWorld = RT_LoadWorld;
	rt_export.EndRegistration = RT_EndRegistration;
	rt_export.RegisterModel = RT_RegisterModel;
	rt_export.RegisterSkin = RT_RegisterSkin;
	rt_export.RegisterShader = RT_RegisterShader;
	rt_export.RegisterShaderNoMip = RT_RegisterShaderNoMip;
	rt_export.RegisterServerModel = RT_RegisterServerModel;
	rt_export.RefreshShaderNoMip = RT_RefreshShaderNoMip;
	rt_export.RegisterFont = RT_RegisterFont;
	rt_export.LoadFont = RT_LoadFont;
	rt_record = NULL;
	rt_recording = 0;
	rt_inFrame = 0;
	rt_depth = 0;
	rt_numFrames = 0;
	rt_load = 0;
	return &rt_export;
}
