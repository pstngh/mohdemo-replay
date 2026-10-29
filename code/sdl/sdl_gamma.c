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

// sdl_gamma.c -- the hardware gamma ramp
//
// SDL 3 has no gamma ramps (SDL 2's SDL_SetWindowGammaRamp is gone). On
// Windows the ramp is set as SDL 2 set it: SetDeviceGammaRamp on the
// window's display, the display's own ramp saved first and put back while
// the window doesn't have the focus and when it closes. Elsewhere there is
// none and the renderer applies r_gamma to the textures (under Xwayland SDL
// 2's ramp did nothing: Xwayland accepts a ramp and ignores it).

#include <SDL3/SDL.h>

#ifdef _WIN32
#	include <windows.h>
#endif

#include "../renderercommon/tr_common.h"
#include "../qcommon/qcommon.h"

extern SDL_Window *SDL_window;

#ifdef _WIN32
static WORD		gammaRamp[3][256];		// the game's (VKimp_SetGamma)
static WORD		desktopRamp[3][256];	// the display's, saved by VKimp_InitGamma
static qboolean	gammaSaved;				// desktopRamp holds the display's ramp
static qboolean	gammaApplied;			// gammaRamp is on the display

/*
=================
VKimp_DeviceGammaRamp

Gets or sets the gamma ramp of the display the window is on
=================
*/
static qboolean VKimp_DeviceGammaRamp( WORD ramp[3][256], qboolean set )
{
	HWND hwnd = (HWND)SDL_GetPointerProperty( SDL_GetWindowProperties( SDL_window ), SDL_PROP_WINDOW_WIN32_HWND_POINTER, NULL );
	MONITORINFOEXW info;
	BOOL ok = FALSE;
	HDC hdc;

	if( !hwnd )
		return qfalse;

	info.cbSize = sizeof( info );
	if( !GetMonitorInfoW( MonitorFromWindow( hwnd, MONITOR_DEFAULTTONEAREST ), (MONITORINFO *)&info ) )
		return qfalse;

	hdc = CreateDCW( info.szDevice, NULL, NULL, NULL );
	if( hdc )
	{
		ok = set ? SetDeviceGammaRamp( hdc, ramp ) : GetDeviceGammaRamp( hdc, ramp );
		DeleteDC( hdc );
	}

	return ok != FALSE;
}

static qboolean VKimp_HasFocus( void )
{
	return ( SDL_GetWindowFlags( SDL_window ) & SDL_WINDOW_INPUT_FOCUS ) != 0;
}
#endif

/*
=================
VKimp_InitGamma

Whether the window's display takes a gamma ramp: saves the display's and
sets a linear one, as SDL 2's SDL_SetWindowBrightness( window, 1.0f ) did
=================
*/
qboolean VKimp_InitGamma( void )
{
#ifdef _WIN32
	int i;

	if( !VKimp_DeviceGammaRamp( desktopRamp, qfalse ) )
	{
		ri.Printf( PRINT_DEVELOPER, "GetDeviceGammaRamp() failed\n" );
		return qfalse;
	}
	gammaSaved = qtrue;

	for( i = 0; i < 256; i++ )
		gammaRamp[0][i] = gammaRamp[1][i] = gammaRamp[2][i] = (WORD)( ( i << 8 ) | i );

	if( VKimp_HasFocus( ) )
		gammaApplied = VKimp_DeviceGammaRamp( gammaRamp, qtrue );

	return qtrue;
#else
	return qfalse;
#endif
}

/*
=================
VKimp_RestoreGamma

Puts the display's own ramp back (the window closes)
=================
*/
void VKimp_RestoreGamma( void )
{
#ifdef _WIN32
	if( gammaSaved )
		VKimp_DeviceGammaRamp( desktopRamp, qtrue );

	gammaSaved = qfalse;
	gammaApplied = qfalse;
#endif
}

/*
=================
VKimp_GammaFocus

The game's ramp while the window has the focus, the display's otherwise,
as SDL 2 did; called every frame
=================
*/
void VKimp_GammaFocus( void )
{
#ifdef _WIN32
	qboolean focused;

	if( !gammaSaved )
		return;

	focused = VKimp_HasFocus( );
	if( focused == gammaApplied )
		return;

	if( focused )
		gammaApplied = VKimp_DeviceGammaRamp( gammaRamp, qtrue );
	else
	{
		VKimp_DeviceGammaRamp( desktopRamp, qtrue );
		gammaApplied = qfalse;
	}
#endif
}

/*
=================
VKimp_SetGamma
=================
*/
void VKimp_SetGamma( unsigned char red[256], unsigned char green[256], unsigned char blue[256] )
{
#ifdef _WIN32
	WORD table[3][256];
	int i, j;

	if( !glConfig.deviceSupportsGamma || r_ignorehwgamma->integer > 0 )
		return;

	for (i = 0; i < 256; i++)
	{
		table[0][i] = ( ( ( WORD ) red[i] ) << 8 ) | red[i];
		table[1][i] = ( ( ( WORD ) green[i] ) << 8 ) | green[i];
		table[2][i] = ( ( ( WORD ) blue[i] ) << 8 ) | blue[i];
	}

	// Windows puts this odd restriction on gamma ramps...
	ri.Printf( PRINT_DEVELOPER, "performing gamma clamp.\n" );
	for( j = 0 ; j < 3 ; j++ )
	{
		for( i = 0 ; i < 128 ; i++ )
		{
			if( table[ j ][ i ] > ( ( 128 + i ) << 8 ) )
				table[ j ][ i ] = ( 128 + i ) << 8;
		}

		if( table[ j ][ 127 ] > 254 << 8 )
			table[ j ][ 127 ] = 254 << 8;
	}

	// enforce constantly increasing
	for (j = 0; j < 3; j++)
	{
		for (i = 1; i < 256; i++)
		{
			if (table[j][i] < table[j][i-1])
				table[j][i] = table[j][i-1];
		}
	}

	Com_Memcpy( gammaRamp, table, sizeof( gammaRamp ) );

	// applied now with the focus, else when the window gets it
	if( VKimp_HasFocus( ) )
	{
		gammaApplied = VKimp_DeviceGammaRamp( gammaRamp, qtrue );
		if( !gammaApplied )
			ri.Printf( PRINT_DEVELOPER, "SetDeviceGammaRamp() failed\n" );
	}
#endif
}
