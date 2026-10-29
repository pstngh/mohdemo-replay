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

// sdl_vkimp.c -- window handling for the Vulkan renderer
//
// The window, video modes and fullscreen handling of ioquake3's SDL layer,
// with the window created for Vulkan: the backend (renderervk/vk.c) creates
// its surface from SDL_window. SDL 3: a fullscreen window takes the display
// mode closest to its size (SDL 2's exclusive fullscreen), found when it
// goes fullscreen.

#include <SDL3/SDL.h>

#include "../renderervk/tr_local.h"
#include "sdl_icon.h"

typedef enum
{
	RSERR_OK,

	RSERR_INVALID_FULLSCREEN,
	RSERR_INVALID_MODE,

	RSERR_UNKNOWN
} rserr_t;

SDL_Window *SDL_window = NULL;

cvar_t *r_allowResize; // make window resizable
cvar_t *r_centerWindow;
cvar_t *r_sdlDriver;

/*
===============
VKimp_Shutdown
===============
*/
void VKimp_Shutdown( void )
{
	ri.IN_Shutdown();

	if( SDL_window != NULL )
	{
		VKimp_RestoreGamma( );
		SDL_DestroyWindow( SDL_window );
		SDL_window = NULL;
	}

	SDL_QuitSubSystem( SDL_INIT_VIDEO );
}

/*
===============
VKimp_SetFullscreenMode

The display mode a fullscreen window takes: the closest to the video size
and r_displayRefresh (0: the desktop's rate), as SDL 2 picked the mode of
SDL_SetWindowDisplayMode; the desktop's if there is none
===============
*/
static void VKimp_SetFullscreenMode( void )
{
	SDL_DisplayMode closest;
	SDL_DisplayID display = SDL_GetDisplayForWindow( SDL_window );

	if( display && SDL_GetClosestFullscreenDisplayMode( display, glConfig.vidWidth, glConfig.vidHeight,
			glConfig.displayFrequency, false, &closest ) )
	{
		if( !SDL_SetWindowFullscreenMode( SDL_window, &closest ) )
			ri.Printf( PRINT_DEVELOPER, "SDL_SetWindowFullscreenMode failed: %s\n", SDL_GetError( ) );
	}
	else
	{
		ri.Printf( PRINT_DEVELOPER, "No display mode for %dx%d: %s\n", glConfig.vidWidth, glConfig.vidHeight, SDL_GetError( ) );
		SDL_SetWindowFullscreenMode( SDL_window, NULL );
	}
}

/*
===============
VKimp_CompareModes
===============
*/
static int VKimp_CompareModes( const void *a, const void *b )
{
	const float ASPECT_EPSILON = 0.001f;
	SDL_Rect *modeA = (SDL_Rect *)a;
	SDL_Rect *modeB = (SDL_Rect *)b;
	float aspectA = (float)modeA->w / (float)modeA->h;
	float aspectB = (float)modeB->w / (float)modeB->h;
	int areaA = modeA->w * modeA->h;
	int areaB = modeB->w * modeB->h;
	float aspectDiffA = fabs( aspectA - displayAspect );
	float aspectDiffB = fabs( aspectB - displayAspect );
	float aspectDiffsDiff = aspectDiffA - aspectDiffB;

	if( aspectDiffsDiff > ASPECT_EPSILON )
		return 1;
	else if( aspectDiffsDiff < -ASPECT_EPSILON )
		return -1;
	else
		return areaA - areaB;
}


/*
===============
VKimp_DetectAvailableModes
===============
*/
static void VKimp_DetectAvailableModes(void)
{
	int i, j;
	char buf[ MAX_STRING_CHARS ] = { 0 };
	int numSDLModes = 0;
	SDL_DisplayMode **sdlModes;
	const SDL_DisplayMode *desktopMode;
	SDL_Rect *modes;
	int numModes = 0;

	SDL_DisplayID display = SDL_GetDisplayForWindow( SDL_window );
	if( !display )
	{
		ri.Printf( PRINT_WARNING, "Couldn't get window display, no resolutions detected: %s\n", SDL_GetError() );
		return;
	}

	// the desktop's format (SDL 2 compared the window's display mode)
	desktopMode = SDL_GetDesktopDisplayMode( display );
	sdlModes = SDL_GetFullscreenDisplayModes( display, &numSDLModes );
	if( !desktopMode || !sdlModes || numSDLModes <= 0 )
	{
		ri.Printf( PRINT_WARNING, "Couldn't get window display mode, no resolutions detected: %s\n", SDL_GetError() );
		SDL_free( sdlModes );
		return;
	}

	modes = SDL_calloc( (size_t)numSDLModes, sizeof( SDL_Rect ) );
	if ( !modes )
	{
		ri.Error( ERR_FATAL, "Out of memory" );
	}

	for( i = 0; i < numSDLModes; i++ )
	{
		const SDL_DisplayMode mode = *sdlModes[ i ];

		if( desktopMode->format != mode.format )
			continue;

		// SDL can give the same resolution with different refresh rates.
		// Only list resolution once.
		for( j = 0; j < numModes; j++ )
		{
			if( mode.w == modes[ j ].w && mode.h == modes[ j ].h )
				break;
		}

		if( j != numModes )
			continue;

		modes[ numModes ].w = mode.w;
		modes[ numModes ].h = mode.h;
		numModes++;
	}

	if( numModes > 1 )
		qsort( modes, numModes, sizeof( SDL_Rect ), VKimp_CompareModes );

	for( i = 0; i < numModes; i++ )
	{
		const char *newModeString = va( "%ux%u ", modes[ i ].w, modes[ i ].h );

		if( strlen( newModeString ) < (int)sizeof( buf ) - strlen( buf ) )
			Q_strcat( buf, sizeof( buf ), newModeString );
		else
			ri.Printf( PRINT_WARNING, "Skipping mode %ux%u, buffer too small\n", modes[ i ].w, modes[ i ].h );
	}

	if( *buf )
	{
		buf[ strlen( buf ) - 1 ] = 0;
		ri.Printf( PRINT_ALL, "Available modes: '%s'\n", buf );
		ri.Cvar_Set( "r_availableModes", buf );
	}
	SDL_free( modes );
	SDL_free( sdlModes );
}

/*
===============
VKimp_SetMode
===============
*/
static int VKimp_SetMode(int mode, qboolean fullscreen, qboolean noborder)
{
	SDL_Surface *icon = NULL;
	SDL_PropertiesID props;
	SDL_DisplayMode desktopMode;
	const SDL_DisplayMode *displayMode;
	SDL_DisplayID display = 0;
	int x = SDL_WINDOWPOS_UNDEFINED, y = SDL_WINDOWPOS_UNDEFINED;

	ri.Printf( PRINT_ALL, "Initializing display\n");

#ifdef USE_ICON
	// RGBA bytes
	icon = SDL_CreateSurfaceFrom(
			CLIENT_WINDOW_ICON.width,
			CLIENT_WINDOW_ICON.height,
			SDL_PIXELFORMAT_RGBA32,
			(void *)CLIENT_WINDOW_ICON.pixel_data,
			CLIENT_WINDOW_ICON.bytes_per_pixel * CLIENT_WINDOW_ICON.width );
#endif

	// If a window exists, note its display
	if( SDL_window != NULL )
	{
		display = SDL_GetDisplayForWindow( SDL_window );
		if( !display )
		{
			ri.Printf( PRINT_DEVELOPER, "SDL_GetDisplayForWindow() failed: %s\n", SDL_GetError() );
		}
	}
	if( !display )
		display = SDL_GetPrimaryDisplay( );

	if( ( displayMode = SDL_GetDesktopDisplayMode( display ) ) != NULL )
	{
		desktopMode = *displayMode;
		displayAspect = (float)desktopMode.w / (float)desktopMode.h;

		ri.Printf( PRINT_ALL, "Display aspect: %.3f\n", displayAspect );
	}
	else
	{
		Com_Memset( &desktopMode, 0, sizeof( SDL_DisplayMode ) );

		ri.Printf( PRINT_ALL,
				"Cannot determine display aspect, assuming 1.333\n" );
	}

	ri.Printf (PRINT_ALL, "...setting mode %d:", mode );

	if (mode == -2)
	{
		// use desktop video resolution
		if( desktopMode.h > 0 )
		{
			glConfig.vidWidth = desktopMode.w;
			glConfig.vidHeight = desktopMode.h;
		}
		else
		{
			glConfig.vidWidth = 640;
			glConfig.vidHeight = 480;
			ri.Printf( PRINT_ALL,
					"Cannot determine display resolution, assuming 640x480\n" );
		}

		glConfig.windowAspect = (float)glConfig.vidWidth / (float)glConfig.vidHeight;
	}
	else if ( !R_GetModeInfo( &glConfig.vidWidth, &glConfig.vidHeight, &glConfig.windowAspect, mode ) )
	{
		ri.Printf( PRINT_ALL, " invalid mode\n" );
		SDL_DestroySurface( icon );
		return RSERR_INVALID_MODE;
	}
	ri.Printf( PRINT_ALL, " %d %d\n", glConfig.vidWidth, glConfig.vidHeight);

	// Center window
	if( r_centerWindow->integer && !fullscreen )
	{
		x = ( desktopMode.w / 2 ) - ( glConfig.vidWidth / 2 );
		y = ( desktopMode.h / 2 ) - ( glConfig.vidHeight / 2 );
	}

	// Destroy existing state if it exists
	if( SDL_window != NULL )
	{
		SDL_GetWindowPosition( SDL_window, &x, &y );
		ri.Printf( PRINT_DEVELOPER, "Existing window at %dx%d before being destroyed\n", x, y );
		SDL_DestroyWindow( SDL_window );
		SDL_window = NULL;
	}

	glConfig.isFullscreen = fullscreen;

	props = SDL_CreateProperties( );
	SDL_SetStringProperty( props, SDL_PROP_WINDOW_CREATE_TITLE_STRING, CLIENT_WINDOW_TITLE );
	SDL_SetNumberProperty( props, SDL_PROP_WINDOW_CREATE_X_NUMBER, x );
	SDL_SetNumberProperty( props, SDL_PROP_WINDOW_CREATE_Y_NUMBER, y );
	SDL_SetNumberProperty( props, SDL_PROP_WINDOW_CREATE_WIDTH_NUMBER, glConfig.vidWidth );
	SDL_SetNumberProperty( props, SDL_PROP_WINDOW_CREATE_HEIGHT_NUMBER, glConfig.vidHeight );
	SDL_SetBooleanProperty( props, SDL_PROP_WINDOW_CREATE_HIDDEN_BOOLEAN, true );
	SDL_SetBooleanProperty( props, SDL_PROP_WINDOW_CREATE_VULKAN_BOOLEAN, true );
	SDL_SetBooleanProperty( props, SDL_PROP_WINDOW_CREATE_RESIZABLE_BOOLEAN, r_allowResize->integer != 0 );
	SDL_SetBooleanProperty( props, SDL_PROP_WINDOW_CREATE_FULLSCREEN_BOOLEAN, fullscreen );
	SDL_SetBooleanProperty( props, SDL_PROP_WINDOW_CREATE_BORDERLESS_BOOLEAN, !fullscreen && noborder );

	SDL_window = SDL_CreateWindowWithProperties( props );
	SDL_DestroyProperties( props );
	if( SDL_window == NULL )
	{
		ri.Printf( PRINT_ALL, "SDL_CreateWindowWithProperties failed: %s\n", SDL_GetError( ) );
		SDL_DestroySurface( icon );
		return RSERR_INVALID_MODE;
	}

	// requested framebuffer bits (ioquake3's defaults); the backend updates
	// them when it picks its formats
	glConfig.colorBits = r_colorbits->integer;
	if( !glConfig.colorBits || glConfig.colorBits >= 32 )
		glConfig.colorBits = 24;
	glConfig.depthBits = r_depthbits->integer ? r_depthbits->integer : 24;
	glConfig.stencilBits = r_stencilbits->integer;

	if( r_stereoEnabled->integer )
		ri.Printf( PRINT_ALL, "Stereo rendering is not supported by this renderer\n" );
	glConfig.stereoEnabled = qfalse;

	// keep the fullscreen window when the exact display mode can't be set
	// (as ioquake3 did)
	if( fullscreen )
	{
		glConfig.displayFrequency = ri.Cvar_VariableIntegerValue( "r_displayRefresh" );
		VKimp_SetFullscreenMode( );
	}

	SDL_SetWindowIcon( SDL_window, icon );
	SDL_DestroySurface( icon );

	// shown with the mode set before the swapchain is made for it (SDL 2's
	// SDL_ShowWindow waited for the window system)
	SDL_ShowWindow( SDL_window );
	SDL_SyncWindow( SDL_window );

	VKimp_DetectAvailableModes();

	return RSERR_OK;
}

/*
===============
VKimp_StartDriverAndSetMode
===============
*/
static qboolean VKimp_StartDriverAndSetMode(int mode, qboolean fullscreen, qboolean noborder)
{
	rserr_t err;

	if (!SDL_WasInit(SDL_INIT_VIDEO))
	{
		const char *driverName;

		if (!SDL_Init(SDL_INIT_VIDEO))
		{
			ri.Printf( PRINT_ALL, "SDL_Init( SDL_INIT_VIDEO ) FAILED (%s)\n", SDL_GetError());
			return qfalse;
		}

		driverName = SDL_GetCurrentVideoDriver( );
		ri.Printf( PRINT_ALL, "SDL using driver \"%s\"\n", driverName );
		ri.Cvar_Set( "r_sdlDriver", driverName );
	}

	if (fullscreen && ri.Cvar_VariableIntegerValue( "in_nograb" ) )
	{
		ri.Printf( PRINT_ALL, "Fullscreen not allowed with in_nograb 1\n");
		ri.Cvar_Set( "r_fullscreen", "0" );
		r_fullscreen->modified = qfalse;
		fullscreen = qfalse;
	}

	err = VKimp_SetMode(mode, fullscreen, noborder);

	switch ( err )
	{
		case RSERR_INVALID_FULLSCREEN:
			ri.Printf( PRINT_ALL, "...WARNING: fullscreen unavailable in this mode\n" );
			return qfalse;
		case RSERR_INVALID_MODE:
			ri.Printf( PRINT_ALL, "...WARNING: could not set the given mode (%d)\n", mode );
			return qfalse;
		default:
			break;
	}

	return qtrue;
}

#define R_MODE_FALLBACK 3 // 640 * 480

/*
===============
VKimp_Init

Creates the window; the GPU backend is initialized by vk_initialize()
===============
*/
void VKimp_Init( void )
{
	ri.Printf( PRINT_DEVELOPER, "VKimp_Init( )\n" );

	r_sdlDriver = ri.Cvar_Get( "r_sdlDriver", "", CVAR_ROM );
	r_allowResize = ri.Cvar_Get( "r_allowResize", "0", CVAR_ARCHIVE | CVAR_LATCH );
	r_centerWindow = ri.Cvar_Get( "r_centerWindow", "0", CVAR_ARCHIVE | CVAR_LATCH );

	if( ri.Cvar_VariableIntegerValue( "com_abnormalExit" ) )
	{
		ri.Cvar_Set( "r_mode", va( "%d", R_MODE_FALLBACK ) );
		ri.Cvar_Set( "r_fullscreen", "0" );
		ri.Cvar_Set( "r_centerWindow", "0" );
		ri.Cvar_Set( "com_abnormalExit", "0" );
	}

	// Create the window
	if(VKimp_StartDriverAndSetMode(r_mode->integer, r_fullscreen->integer, r_noborder->integer))
		goto success;

	// Try again without the border setting
	if(VKimp_StartDriverAndSetMode(r_mode->integer, r_fullscreen->integer, qfalse))
		goto success;

	// Finally, try the default screen resolution
	if( r_mode->integer != R_MODE_FALLBACK )
	{
		ri.Printf( PRINT_ALL, "Setting r_mode %d failed, falling back on r_mode %d\n",
				r_mode->integer, R_MODE_FALLBACK );

		if(VKimp_StartDriverAndSetMode(R_MODE_FALLBACK, qfalse, qfalse))
			goto success;
	}

	// Nothing worked, give up
	ri.Error( ERR_FATAL, "VKimp_Init() - could not create the window" VK_DRIVER_HINT );

success:
	// These values force the UI to disable driver selection
	glConfig.driverType = GLDRV_ICD;
	glConfig.hardwareType = GLHW_GENERIC;

	// a linear ramp set to see if hardware gamma is supported (Windows only,
	// see sdl_gamma.c)
	glConfig.deviceSupportsGamma = !r_ignorehwgamma->integer && VKimp_InitGamma( );

	ri.Cvar_Get( "r_availableModes", "", CVAR_ROM );

	// This depends on SDL_INIT_VIDEO, hence having it here
	ri.IN_Init( SDL_window );
}


/*
===============
VKimp_EndFrame

Handles fullscreen toggling once the frame has been presented
===============
*/
void VKimp_EndFrame( void )
{
	VKimp_GammaFocus( );

	if( r_fullscreen->modified )
	{
		int         fullscreen;
		qboolean    needToToggle;
		qboolean    sdlToggled = qfalse;

		// Find out the current state
		fullscreen = !!( SDL_GetWindowFlags( SDL_window ) & SDL_WINDOW_FULLSCREEN );

		if( r_fullscreen->integer && ri.Cvar_VariableIntegerValue( "in_nograb" ) )
		{
			ri.Printf( PRINT_ALL, "Fullscreen not allowed with in_nograb 1\n");
			ri.Cvar_Set( "r_fullscreen", "0" );
			r_fullscreen->modified = qfalse;
		}

		// Is the state we want different from the current state?
		needToToggle = !!r_fullscreen->integer != fullscreen;

		if( needToToggle )
		{
			// the closest display mode, as when the window is made
			// fullscreen, and the change waited for, as SDL 2 did
			if( r_fullscreen->integer )
				VKimp_SetFullscreenMode( );
			sdlToggled = SDL_SetWindowFullscreen( SDL_window, r_fullscreen->integer != 0 );
			if( sdlToggled )
				SDL_SyncWindow( SDL_window );

			// SDL_WM_ToggleFullScreen didn't work, so do it the slow way
			if( !sdlToggled )
				ri.Cmd_ExecuteText(EXEC_APPEND, "vid_restart\n");

			ri.IN_Restart( );
		}

		r_fullscreen->modified = qfalse;
	}
}
