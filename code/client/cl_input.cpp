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
// cl.input.c  -- builds an intended movement command to send to the server

#include "client.h"
#include "cl_ui.h"

unsigned	frame_msec;
int			old_com_frameTime;
static int	old_look_frameTime;	// com_frameTime of the last CL_ApplyLook

cvar_t		*cl_movefps;			// usercmds a second, whatever the frame rate; 0: one per frame
static cvar_t	*cl_movesubgrid;	// a shot's or a jump's usercmd at the half step (CL_SubGridStep)
static cvar_t	*cl_nullbinds;		// opposing movement and lean keys: the newest press wins (CL_NullbindKey)

/*
=================
CL_MoveInterval

The usercmd step of cl_movefps in whole milliseconds of server time, 1000 /
cl_movefps (85 to 125 a second: 11 to 8 ms), or 0 for cl_movefps 0 (and
below): one usercmd per frame at the frame's server time, as a stock client,
each as long as its frame (as on the metal branch)
=================
*/
static int CL_MoveInterval( void ) {
	if ( cl_movefps->integer <= 0 ) {
		return 0;
	}
	return 1000 / (int)Com_Clamp( 85, 125, cl_movefps->integer );
}

/*
=================
CL_CommandPerFrame

cl_movefps 0: one usercmd per frame. Com_Frame then keeps every frame a
whole millisecond or longer, at most 1000 a second (also uncapped), as stock:
each usercmd has a server time of its own.
=================
*/
qboolean CL_CommandPerFrame( void ) {
	return cl_movefps && cl_movefps->integer <= 0;
}

/*
===============================================================================

KEY BUTTONS

Continuous button event tracking is complicated by the fact that two different
input sources (say, mouse button 1 and the control key) can both press the
same button, but the button should only be released when both of the
pressing key have been released.

When a key event issues a button command (+forward, +attackprimary, etc), it appends
its key number as argv(1) so it can be matched up with the release.

argv(2) will be set to the time the event happened, which allows exact
control even at low framerates when the down and up events may both get qued
at the same time.

===============================================================================
*/


kbutton_t	in_left, in_right, in_forward, in_back;
kbutton_t	in_lookup, in_lookdown, in_moveleft, in_moveright;
kbutton_t	in_strafe, in_speed;

qboolean	in_guimouse;

kbutton_t	in_up, in_down;

#ifdef USE_VOIP
kbutton_t	in_voiprecord;
#endif

kbutton_t	in_buttons[16];

qboolean	in_mlooking;

void IN_ToggleMouse( void ) {
	if( in_guimouse )
	{
		IN_MouseOff();
	}
	else
	{
		IN_MouseOn();
	}
}

void IN_MouseOn( void ) {
	if( !in_guimouse )
	{
		for( int k = 0; k <= K_MWHEELUP; k++ )
		{
			if( keys[ k ].down )
			{
				CL_KeyEvent( k, qfalse, 0 );
			}
		}
	}

	in_guimouse = qtrue;
}

void IN_MouseOff( void ) {
	in_guimouse = qfalse;
}

void IN_MLookDown( void ) {
	in_mlooking = qtrue;
}

void IN_MLookUp( void ) {
	in_mlooking = qfalse;
	if ( !cl_freelook->integer ) {
		IN_CenterView ();
	}
}

void IN_KeyDown( kbutton_t *b ) {
	int		k;
	char	*c;

	c = Cmd_Argv(1);
	if ( c[0] ) {
		k = atoi(c);
	} else {
		k = -1;		// typed manually at the console for continuous down
	}

	if ( k == b->down[0] || k == b->down[1] ) {
		return;		// repeating key
	}

	if ( !b->down[0] ) {
		b->down[0] = k;
	} else if ( !b->down[1] ) {
		b->down[1] = k;
	} else {
		Com_Printf ("Three keys down for a button!\n");
		return;
	}

	if ( b->active ) {
		return;		// still down
	}

	// save timestamp for partial frame summing
	c = Cmd_Argv(2);
	b->downtime = atoi(c);

	b->active = qtrue;
	b->wasPressed = qtrue;
}

void IN_KeyUp( kbutton_t *b ) {
	int		k;
	char	*c;
	unsigned	uptime;

	c = Cmd_Argv(1);
	if ( c[0] ) {
		k = atoi(c);
	} else {
		// typed manually at the console, assume for unsticking, so clear all
		b->down[0] = b->down[1] = 0;
		b->active = qfalse;
		return;
	}

	if ( b->down[0] == k ) {
		b->down[0] = 0;
	} else if ( b->down[1] == k ) {
		b->down[1] = 0;
	} else {
		return;		// key up without coresponding down (menu pass through)
	}
	if ( b->down[0] || b->down[1] ) {
		return;		// some other key is still holding it down
	}

	b->active = qfalse;

	// save timestamp for partial frame summing
	c = Cmd_Argv(2);
	uptime = atoi(c);
	if ( uptime ) {
		b->msec += uptime - b->downtime;
	} else {
		b->msec += frame_msec / 2;
	}

	b->active = qfalse;
}



/*
===============
CL_KeyState

Returns the fraction of the frame that the key was down
===============
*/
float CL_KeyState( kbutton_t *key ) {
	float		val;
	int			msec;

	msec = key->msec;
	key->msec = 0;

	if ( key->active ) {
		// still down
		if ( !key->downtime ) {
			msec = com_frameTime;
		} else {
			msec += com_frameTime - key->downtime;
		}
		key->downtime = com_frameTime;
	}

#if 0
	if (msec) {
		Com_Printf ("%i ", msec);
	}
#endif

	val = (float)msec / frame_msec;
	if ( val < 0 ) {
		val = 0;
	}
	if ( val > 1 ) {
		val = 1;
	}

	return val;
}



/*
===============================================================================

NULLBINDS

Opposing movement and lean keys never cancel out: the newest press wins while
the other side's key stays held, and releasing it hands control back. kbutton_t
already tracks the keys holding a button separately from its effective state,
so only active, the partial-frame msec and wasPressed are adjusted here.

===============================================================================
*/

static qboolean CL_NullbindHeld( const kbutton_t *b ) {
	return (qboolean)( b->down[0] || b->down[1] );
}

static void CL_NullbindSuppress( kbutton_t *b, unsigned time ) {
	unsigned	elapsed;

	if ( !b->active ) {
		return;
	}
	b->active = qfalse;

	if ( !time || !b->downtime ) {
		b->msec += frame_msec / 2;
		return;
	}

	// unsigned subtraction handles timer wrap; a delta with the high bit set
	// is an out-of-order timestamp, so credit no phantom movement
	elapsed = time - b->downtime;
	if ( elapsed < 0x80000000u ) {
		b->msec += elapsed;
	}
}

static void CL_NullbindKey( kbutton_t *b, kbutton_t *opposite, qboolean down ) {
	int			holders[2] = { b->down[0], b->down[1] };
	qboolean	suppressed = (qboolean)( !b->active && CL_NullbindHeld( b ) );
	unsigned	msec = b->msec;
	unsigned	time;

	if ( down ) {
		IN_KeyDown( b );
	} else {
		IN_KeyUp( b );
	}

	if ( b->down[0] == holders[0] && b->down[1] == holders[1] ) {
		return;		// repeat, third key, or release without a matching press
	}
	if ( down && !cl_nullbinds->integer ) {
		return;		// stock: both sides held cancel out
	}

	// sub-frame taps belong to the winning side, so a usercmd never carries
	// both sides of a button pair
	time = atoi( Cmd_Argv( 2 ) );
	if ( down ) {
		// the newest press wins
		CL_NullbindSuppress( opposite, time );
		opposite->wasPressed = qfalse;
		return;
	}

	if ( suppressed ) {
		// IN_KeyUp credits a suppressed button from its stale downtime, but
		// that time was already settled when the button was suppressed
		b->msec = msec;
	}

	// releasing the winner hands control back to a side still held
	if ( !CL_NullbindHeld( b ) && CL_NullbindHeld( opposite ) && !opposite->active ) {
		opposite->downtime = time;
		opposite->active = qtrue;
		b->wasPressed = qfalse;
	}
}

void IN_UpDown(void) {IN_KeyDown(&in_up);}
void IN_UpUp(void) {IN_KeyUp(&in_up);}
void IN_DownDown(void) {IN_KeyDown(&in_down);}
void IN_DownUp(void) {IN_KeyUp(&in_down);}
void IN_LeftDown(void) {IN_KeyDown(&in_left);}
void IN_LeftUp(void) {IN_KeyUp(&in_left);}
void IN_RightDown(void) {IN_KeyDown(&in_right);}
void IN_RightUp(void) {IN_KeyUp(&in_right);}
void IN_ForwardDown(void) {CL_NullbindKey(&in_forward, &in_back, qtrue);}
void IN_ForwardUp(void) {CL_NullbindKey(&in_forward, &in_back, qfalse);}
void IN_BackDown(void) {CL_NullbindKey(&in_back, &in_forward, qtrue);}
void IN_BackUp(void) {CL_NullbindKey(&in_back, &in_forward, qfalse);}
void IN_LookupDown(void) {IN_KeyDown(&in_lookup);}
void IN_LookupUp(void) {IN_KeyUp(&in_lookup);}
void IN_LookdownDown(void) {IN_KeyDown(&in_lookdown);}
void IN_LookdownUp(void) {IN_KeyUp(&in_lookdown);}
void IN_MoveleftDown(void) {CL_NullbindKey(&in_moveleft, &in_moveright, qtrue);}
void IN_MoveleftUp(void) {CL_NullbindKey(&in_moveleft, &in_moveright, qfalse);}
void IN_MoverightDown(void) {CL_NullbindKey(&in_moveright, &in_moveleft, qtrue);}
void IN_MoverightUp(void) {CL_NullbindKey(&in_moveright, &in_moveleft, qfalse);}
void IN_AttackPrimaryDown(void) { IN_KeyDown(&in_buttons[0]);}
void IN_AttackPrimaryUp(void) { IN_KeyUp(&in_buttons[0]); }
void IN_AttackSecondaryDown(void) { IN_KeyDown(&in_buttons[1]); }
void IN_AttackSecondaryUp(void) { IN_KeyUp(&in_buttons[1]); }
void IN_UseDown(void) { IN_KeyDown(&in_buttons[3]); }
void IN_UseUp(void) { IN_KeyUp(&in_buttons[3]); }
void IN_LeanLeftDown(void) { CL_NullbindKey(&in_buttons[4], &in_buttons[5], qtrue); }
void IN_LeanLeftUp(void) { CL_NullbindKey(&in_buttons[4], &in_buttons[5], qfalse); }
void IN_LeanRightDown(void) { CL_NullbindKey(&in_buttons[5], &in_buttons[4], qtrue); }
void IN_LeanRightUp(void) { CL_NullbindKey(&in_buttons[5], &in_buttons[4], qfalse); }

void IN_SpeedDown(void) {IN_KeyDown(&in_speed);}
void IN_SpeedUp(void) {IN_KeyUp(&in_speed);}
void IN_StrafeDown(void) {IN_KeyDown(&in_strafe);}
void IN_StrafeUp(void) {IN_KeyUp(&in_strafe);}

#ifdef USE_VOIP
void IN_VoipRecordDown(void)
{
	IN_KeyDown(&in_voiprecord);
	Cvar_Set("cl_voipSend", "1");
}

void IN_VoipRecordUp(void)
{
	IN_KeyUp(&in_voiprecord);
	Cvar_Set("cl_voipSend", "0");
}
#endif

void IN_Button0Down(void) {IN_KeyDown(&in_buttons[0]);}
void IN_Button0Up(void) {IN_KeyUp(&in_buttons[0]);}
void IN_Button1Down(void) {IN_KeyDown(&in_buttons[1]);}
void IN_Button1Up(void) {IN_KeyUp(&in_buttons[1]);}
void IN_Button2Down(void) {IN_KeyDown(&in_buttons[2]);}
void IN_Button2Up(void) {IN_KeyUp(&in_buttons[2]);}
void IN_Button3Down(void) {IN_KeyDown(&in_buttons[3]);}
void IN_Button3Up(void) {IN_KeyUp(&in_buttons[3]);}
void IN_Button4Down(void) {CL_NullbindKey(&in_buttons[4], &in_buttons[5], qtrue);}
void IN_Button4Up(void) {CL_NullbindKey(&in_buttons[4], &in_buttons[5], qfalse);}
void IN_Button5Down(void) {CL_NullbindKey(&in_buttons[5], &in_buttons[4], qtrue);}
void IN_Button5Up(void) {CL_NullbindKey(&in_buttons[5], &in_buttons[4], qfalse);}
void IN_Button6Down(void) {IN_KeyDown(&in_buttons[6]);}
void IN_Button6Up(void) {IN_KeyUp(&in_buttons[6]);}
void IN_Button7Down(void) {IN_KeyDown(&in_buttons[7]);}
void IN_Button7Up(void) {IN_KeyUp(&in_buttons[7]);}
void IN_Button8Down(void) {IN_KeyDown(&in_buttons[8]);}
void IN_Button8Up(void) {IN_KeyUp(&in_buttons[8]);}
void IN_Button9Down(void) {IN_KeyDown(&in_buttons[9]);}
void IN_Button9Up(void) {IN_KeyUp(&in_buttons[9]);}
void IN_Button10Down(void) {IN_KeyDown(&in_buttons[10]);}
void IN_Button10Up(void) {IN_KeyUp(&in_buttons[10]);}
void IN_Button11Down(void) {IN_KeyDown(&in_buttons[11]);}
void IN_Button11Up(void) {IN_KeyUp(&in_buttons[11]);}
void IN_Button12Down(void) {IN_KeyDown(&in_buttons[12]);}
void IN_Button12Up(void) {IN_KeyUp(&in_buttons[12]);}
void IN_Button13Down(void) {IN_KeyDown(&in_buttons[13]);}
void IN_Button13Up(void) {IN_KeyUp(&in_buttons[13]);}
void IN_Button14Down(void) {IN_KeyDown(&in_buttons[14]);}
void IN_Button14Up(void) {IN_KeyUp(&in_buttons[14]);}
void IN_Button15Down(void) {IN_KeyDown(&in_buttons[15]);}
void IN_Button15Up(void) {IN_KeyUp(&in_buttons[15]);}

void IN_ButtonDown (void) {
	IN_KeyDown(&in_buttons[1]);}
void IN_ButtonUp (void) {
	IN_KeyUp(&in_buttons[1]);}

void IN_CenterView (void) {
	cl.viewangles[PITCH] = -SHORT2ANGLE(cl.snap.ps.delta_angles[PITCH]);
}


//==========================================================================

cvar_t	*cl_upspeed;
cvar_t	*cl_forwardspeed;
cvar_t	*cl_sidespeed;

cvar_t	*cl_yawspeed;
cvar_t	*cl_pitchspeed;

cvar_t	*cl_run;

cvar_t	*cl_anglespeedkey;


/*
================
CL_AdjustAngles

Moves the local angle positions
================
*/
void CL_AdjustAngles( void ) {
	float	speed;

	if ( in_speed.active ) {
		speed = 0.001 * cls.frametime * cl_anglespeedkey->value;
	} else {
		speed = 0.001 * cls.frametime;
	}

	if ( !in_strafe.active ) {
		cl.viewangles[YAW] -= speed*cl_yawspeed->value*CL_KeyState (&in_right);
		cl.viewangles[YAW] += speed*cl_yawspeed->value*CL_KeyState (&in_left);
	}

	cl.viewangles[PITCH] -= speed*cl_pitchspeed->value * CL_KeyState (&in_lookup);
	cl.viewangles[PITCH] += speed*cl_pitchspeed->value * CL_KeyState (&in_lookdown);
}

/*
================
CL_KeyMove

Sets the usercmd_t based on key states
================
*/
void CL_KeyMove( usercmd_t *cmd ) {
	int		movespeed;
	int		forward, side, up;

	forward = 0;
	side = 0;
	up = 0;
	movespeed = 127;
	if ( in_strafe.active ) {
		side += movespeed * CL_KeyState (&in_right);
		side -= movespeed * CL_KeyState (&in_left);
	}

	side += movespeed * CL_KeyState (&in_moveright);
	side -= movespeed * CL_KeyState (&in_moveleft);


	up += movespeed * CL_KeyState (&in_up);
	up -= movespeed * CL_KeyState (&in_down);

	forward += movespeed * CL_KeyState (&in_forward);
	forward -= movespeed * CL_KeyState (&in_back);

	cmd->forwardmove = ClampChar( forward );
	cmd->rightmove = ClampChar( side );
	cmd->upmove = ClampChar( up );
}

/*
=================
CL_GetMouseState
=================
*/
void CL_GetMouseState( int *x, int *y, unsigned int *buttons )
{
	if( x )
		*x = cl.mousex;

	if( y )
		*y = cl.mousey;

	if( buttons )
	{
		if( in_guimouse ) {
			*buttons = cl.mouseButtons;
		} else {
			*buttons = 0;
		}
	}
}

/*
=================
CL_SetMousePos
=================
*/
void CL_SetMousePos( int x, int y )
{
	cl.mousex = x;
	cl.mousey = y;
}

/*
=================
CL_MouseEvent
=================
*/
void CL_MouseEvent( int dx, int dy, int time ) {
	if( in_guimouse )
	{
		cl.mousex += dx;
		cl.mousey += dy;

		if( cl.mousex < 0 )
			cl.mousex = 0;

		if( cl.mousex > cls.glconfig.vidWidth )
			cl.mousex = cls.glconfig.vidWidth;

		if( cl.mousey < 0 )
			cl.mousey = 0;

		if( cl.mousey > cls.glconfig.vidHeight )
			cl.mousey = cls.glconfig.vidHeight;
	}
	else if ( !paused->integer )
	{
		cl.mouseDx += dx;
		cl.mouseDy += dy;
	}
}

/*
=================
CL_JoystickEvent

Joystick values stay set until changed
=================
*/
void CL_JoystickEvent( int axis, int value, int time ) {
	if ( axis < 0 || axis >= MAX_JOYSTICK_AXIS ) {
		Com_Error( ERR_DROP, "CL_JoystickEvent: bad axis %i", axis );
	}
	cl.joystickAxis[axis] = value;
}

/*
=================
CL_UpdateMouse

Added in OPM
Update mouse position with absolute position (relative to the window)
when the UI catcher is active.
=================
*/
void CL_UpdateMouse() {
    if (!(Key_GetCatcher() & KEYCATCH_UI)) {
        return;
    }

    if (IN_IsCursorActive()) {
        // If it's grabbed, don't use absolute position
        return;
    }

    if (com_unfocused->integer) {
        // Ignore updates when unfocused
        return;
    }

    IN_GetMousePosition(&cl.mousex, &cl.mousey);
}

/*
=================
CL_JoystickLook

The joystick's turning, every frame (CL_ApplyLook)
=================
*/
static void CL_JoystickLook( void ) {
	float	anglespeed;

	float yaw     = j_yaw->value     * cl.joystickAxis[j_yaw_axis->integer];
	float right   = j_side->value    * cl.joystickAxis[j_side_axis->integer];
	float forward = j_forward->value * cl.joystickAxis[j_forward_axis->integer];
	float pitch   = j_pitch->value   * cl.joystickAxis[j_pitch_axis->integer];

	if ( in_speed.active ) {
		anglespeed = 0.001 * cls.frametime * cl_anglespeedkey->value;
	} else {
		anglespeed = 0.001 * cls.frametime;
	}

	if ( !in_strafe.active ) {
		cl.viewangles[YAW] += anglespeed * yaw;
	} else {
		cl.viewangles[YAW] += anglespeed * right;
	}

	if ( in_mlooking ) {
		cl.viewangles[PITCH] += anglespeed * forward;
	} else {
		cl.viewangles[PITCH] += anglespeed * pitch;
	}
}

/*
=================
CL_JoystickMove

The joystick's movement, into each usercmd
=================
*/
void CL_JoystickMove( usercmd_t *cmd ) {
	float yaw     = j_yaw->value     * cl.joystickAxis[j_yaw_axis->integer];
	float right   = j_side->value    * cl.joystickAxis[j_side_axis->integer];
	float forward = j_forward->value * cl.joystickAxis[j_forward_axis->integer];
	float pitch   = j_pitch->value   * cl.joystickAxis[j_pitch_axis->integer];
	float up      = j_up->value      * cl.joystickAxis[j_up_axis->integer];

	if ( in_speed.active ^ cl_run->integer ) {
		cmd->buttons |= BUTTON_RUN;
	}

	if ( !in_strafe.active ) {
		cmd->rightmove = ClampChar( cmd->rightmove + (int)right );
	} else {
		cmd->rightmove = ClampChar( cmd->rightmove + (int)yaw );
	}

	if ( in_mlooking ) {
		cmd->forwardmove = ClampChar( cmd->forwardmove + (int)pitch );
	} else {
		cmd->forwardmove = ClampChar( cmd->forwardmove + (int)forward );
	}

	cmd->upmove = ClampChar( cmd->upmove + (int)up );
}

/*
=================
CL_MouseLook

The mouse's turning, every frame (CL_ApplyLook); its strafe and BUTTON_ANY
wait for the next usercmd (CL_CreateCmd)
=================
*/
static void CL_MouseLook( void ) {
	float	mx, my;
	float	cgameSensitivity;

	// the movement since the last frame, unfiltered (no m_filter: averaging
	// with the frame before showed half of every movement a frame later)
	mx = cl.mouseDx;
	my = cl.mouseDy;
	cl.mouseDx = 0;
	cl.mouseDy = 0;

    if (mx == 0.0f && my == 0.0f)
        return;

	if (cl_mouseAccel->value != 0.0f)
	{
		if(cl_mouseAccelStyle->integer == 0)
		{
			float accelSensitivity;
			float rate;
			
			rate = sqrt(mx * mx + my * my) / (float) frame_msec;

			accelSensitivity = cl_sensitivity->value + rate * cl_mouseAccel->value;
			mx *= accelSensitivity;
			my *= accelSensitivity;
			
			if(cl_showMouseRate->integer)
				Com_Printf("rate: %f, accelSensitivity: %f\n", rate, accelSensitivity);
		}
		else
		{
			float rate[2];
			float power[2];

			// sensitivity remains pretty much unchanged at low speeds
			// cl_mouseAccel is a power value to how the acceleration is shaped
			// cl_mouseAccelOffset is the rate for which the acceleration will have doubled the non accelerated amplification
			// NOTE: decouple the config cvars for independent acceleration setup along X and Y?

			rate[0] = fabs(mx) / (float) frame_msec;
			rate[1] = fabs(my) / (float) frame_msec;
			power[0] = powf(rate[0] / cl_mouseAccelOffset->value, cl_mouseAccel->value);
			power[1] = powf(rate[1] / cl_mouseAccelOffset->value, cl_mouseAccel->value);

			mx = cl_sensitivity->value * (mx + ((mx < 0) ? -power[0] : power[0]) * cl_mouseAccelOffset->value);
			my = cl_sensitivity->value * (my + ((my < 0) ? -power[1] : power[1]) * cl_mouseAccelOffset->value);

			if(cl_showMouseRate->integer)
				Com_Printf("ratex: %f, ratey: %f, powx: %f, powy: %f\n", rate[0], rate[1], power[0], power[1]);
		}
	}
	else
	{
		mx *= cl_sensitivity->value;
		my *= cl_sensitivity->value;
	}

	cgameSensitivity = 1.0f;
	if( cge && !UI_MenuActive() )
	{
		if( cge->CG_SensitivityScale() >= 0.0 ) {
			cgameSensitivity = cge->CG_SensitivityScale();
		}
	}

	mx *= cgameSensitivity;
	my *= cgameSensitivity;

	cl.lookButtons |= BUTTON_ANY;

	// add mouse X/Y movement to the next cmd
	if(in_strafe.active)
		cl.lookMove[0] += m_side->value * mx;
	else
		cl.viewangles[YAW] -= m_yaw->value * mx;

	if ((in_mlooking || cl_freelook->integer) && !in_strafe.active)
		cl.viewangles[PITCH] += m_pitch->value * my;
	else
		cl.lookMove[1] -= m_forward->value * my;

	if (!isfinite(cl.viewangles[PITCH]) || !isfinite(cl.viewangles[YAW])) {
		Com_DPrintf("Invalid client viewangles encountered (view pitch: %f, view yaw: %f)!\n", cl.viewangles[PITCH], cl.viewangles[YAW]);
		Com_DPrintf("cgameSensitivity: %f | mx: %f | my: %f | m_pitch: %f | m_yaw: %f\n", cgameSensitivity, mx, my, m_pitch->value, m_yaw->value);
		Com_DPrintf("Resetting client viewangles\n");
		cl.viewangles[PITCH] = 0;
		cl.viewangles[YAW] = 0;
	}
}

/*
==============
CL_ClearButtons
==============
*/
void CL_ClearButtons( void ) {
	memset( in_buttons, 0, sizeof( in_buttons ) );
}

/*
==============
CL_CmdButtons
==============
*/
void CL_CmdButtons( usercmd_t *cmd ) {
	int		i;
	
	//
	// figure button bits
	// send a button bit even if the key was pressed and released in
	// less than a frame
	//
	for (i = 0 ; i < 15 ; i++) {
		if ( in_buttons[i].active || in_buttons[i].wasPressed ) {
			cmd->buttons |= 1 << i;
		}
		in_buttons[i].wasPressed = qfalse;
	}

	if (UI_MenuActive() || UI_ConsoleIsOpen()) {
		cmd->buttons |= BUTTON_TALK;
	}
	
	if ( in_speed.active ^ cl_run->integer ) {
		cmd->buttons |= BUTTON_RUN;
	}

	if (cge) {
		// send weapon commands from cg
		cmd->buttons |= cge->CG_WeaponCommandButtonBits();
	}

	// allow the game to know if any key at all is
	// currently pressed, even if it isn't bound to anything
	if ( anykeydown && Key_GetCatcher() == 0) {
		cmd->buttons |= BUTTON_MOUSE;
	}

	if (anykeydown) {
		cmd->buttons |= BUTTON_ANY;
	}
}


/*
==============
CL_FinishMove
==============
*/
void CL_FinishMove( usercmd_t *cmd ) {
	int		i;

	// send the current server time so the amount of movement
	// can be determined without allowing cheating
	cmd->serverTime = cl.serverTime;

	for (i=0 ; i<3 ; i++) {
		cmd->angles[i] = ANGLE2SHORT(cl.viewangles[i]);
	}
}


/*
=================
CL_ApplyLook

Turns the view with this frame's input: the keyboard, the mouse and the
joystick, every frame. Usercmds come at cl_movefps (CL_CreateNewCommands) and
carry the angles of the frame that makes them; cgame turns the view of the
frames between them (CL_GetViewUserCmd). The picture's step uses the keys
held now (CL_GetLiveUserCmd). The usercmd still waits for the grid (at
cl_movefps 0 every frame makes one).
=================
*/
static void CL_ApplyLook( void ) {
	vec3_t		oldAngles;
	unsigned	cmdMsec;

	VectorCopy( cl.viewangles, oldAngles );

	// the keys and the mouse rate take the fraction of this frame; a frame
	// in the same millisecond as the last one counts as 1 ms, as in
	// CL_CreateNewCommands
	cmdMsec = frame_msec;
	frame_msec = com_frameTime - old_look_frameTime;
	if ( frame_msec < 1 ) {
		frame_msec = 1;
	}
	if ( frame_msec > 200 ) {
		frame_msec = 200;
	}
	old_look_frameTime = com_frameTime;

	// keyboard angle adjustment
	CL_AdjustAngles ();

	// get basic movement from mouse
	CL_MouseLook();

	// get basic movement from joystick
	CL_JoystickLook();

	frame_msec = cmdMsec;

	// check to make sure the angles haven't wrapped
	if ( cl.viewangles[PITCH] - oldAngles[PITCH] > 90 ) {
		cl.viewangles[PITCH] = oldAngles[PITCH] + 90;
	} else if ( oldAngles[PITCH] - cl.viewangles[PITCH] > 90 ) {
		cl.viewangles[PITCH] = oldAngles[PITCH] - 90;
	}

	// draw debug graphs of turning for mouse testing
	if ( cl_debugMove->integer ) {
		if ( cl_debugMove->integer == 1 ) {
			SCR_DebugGraph( fabs(cl.viewangles[YAW] - oldAngles[YAW]) );
		}
		if ( cl_debugMove->integer == 2 ) {
			SCR_DebugGraph( fabs(cl.viewangles[PITCH] - oldAngles[PITCH]) );
		}
	}
}

/*
=================
CL_CreateCmd
=================
*/
usercmd_t CL_CreateCmd( void ) {
	usercmd_t	cmd;

	Com_Memset( &cmd, 0, sizeof( cmd ) );

	CL_CmdButtons( &cmd );

	// get basic movement from keyboard
	CL_KeyMove( &cmd );

	// what the mouse added since the last cmd (CL_ApplyLook)
	cmd.buttons |= cl.lookButtons;
	cmd.rightmove = ClampChar( cmd.rightmove + cl.lookMove[0] );
	cmd.forwardmove = ClampChar( cmd.forwardmove + cl.lookMove[1] );
	cl.lookButtons = 0;
	cl.lookMove[0] = cl.lookMove[1] = 0;

	// get basic movement from joystick
	CL_JoystickMove( &cmd );

	// store out the final values
	CL_FinishMove( &cmd );

	return cmd;
}


// the sub-grid (cl_movesubgrid, CL_SubGridStep)
static struct {
	int			step;		// to the next usercmd when not the interval: the half to an early one, the rest after it
	qboolean	early;		// the next usercmd is an early one
	qboolean	send;		// the newest usercmds started with a short step: their packet goes at once
	qboolean	upHeld;		// the jump key held at the newest usercmd
	int			prevNow;	// the server time of the frame before
} cmd_sub;

/*
=================
CL_SubGridStep

cl_movesubgrid: the step from the newest usercmd to the next one, called in
every frame of cl_movefps before its usercmds. A fire click (attack primary
or secondary) or a jump seen in the first half of a step takes the next
usercmd to the half step (the newest one's time + half the interval: 5 ms at
100, 4 at 125), made by the first frame there, so the server gets the shot or
the jump up to half a step sooner, and the grid goes on from its own points:
that step is split in two (CL_CreateNewCommands). It is early when the
frame before was still before the half point (the click or the key came
after that frame's input read). A jump's share of its usercmd counts from
its key event's time, the frame before's input read (IN_Frame, CL_KeyState),
so only an early usercmd made now in that same millisecond would carry none
of it: that one waits for the grid. One early usercmd a step; one still to
come when a frame is at the grid's point is dropped: the grid's usercmd
takes the event. Use and the movement keys stay on the grid. notuern's
idea, as on the metal branch.
=================
*/
static int CL_SubGridStep( int interval, int elapsed ) {
	int		half = interval / 2;
	int		before = cmd_sub.prevNow - ( cl.serverTime - elapsed );	// the frame before's elapsed
	int		seen = half;

	cmd_sub.prevNow = cl.serverTime;
	if ( cmd_sub.early && elapsed >= interval ) {
		cmd_sub.step = 0;
		cmd_sub.early = qfalse;
	}
	if ( !cmd_sub.step && elapsed < interval && cl_movesubgrid->integer > 0 ) {
		if ( in_buttons[0].wasPressed || in_buttons[1].wasPressed ) {
			seen = before;
		} else if ( !cmd_sub.upHeld && ( in_up.active || in_up.msec ) ) {
			seen = elapsed >= half && in_up.active && in_up.downtime == (unsigned)com_frameTime ? half : before;
		}
		if ( seen < half ) {
			cmd_sub.step = half;
			cmd_sub.early = qtrue;
		}
	}
	return cmd_sub.step ? cmd_sub.step : interval;
}

/*
=================
CL_CreateNewCommands

Create the usercmds of this frame. In the game they come at a fixed rate,
whatever the frame rate: one every 1000 / cl_movefps whole milliseconds of
server time (85 to 125 a second; 1000 / 85 is 11 ms, 1000 / 125 is 8), so that
every pmove step has that length, at 144 frames a second as at 250 or 2000,
also when the server time drifts (CL_AdjustTimeDelta). A frame that passes
none makes none (the view still turns, CL_ApplyLook), a frame longer than
the step makes several, with the same input (up to 100 ms of them). A shot
or a jump can split a step in two (cl_movesubgrid, CL_SubGridStep).
cl_movefps 0 is stock: one per frame, each step as long as its frame.
=================
*/
void CL_CreateNewCommands( void ) {
	usercmd_t	cmd;
	int			interval, elapsed, step, cmdTime, count, i;
	qboolean	upNow;

	// before the first snapshot cl.serverTime doesn't run, and cl_movefps 0
	// is stock: one per frame
	interval = clc.state == CA_ACTIVE ? CL_MoveInterval() : 0;
	step = interval;
	if ( !interval ) {
		Com_Memset( &cmd_sub, 0, sizeof( cmd_sub ) );
		cmd_sub.prevNow = cl.serverTime;
	}

	// no need to create usercmds until we have a gamestate
	if ( clc.state < CA_PRIMED ) {
		return;
	}

	count = 1;
	cmdTime = cl.serverTime;
	if ( interval ) {
		elapsed = cl.serverTime - cl.cmds[cl.cmdNumber & CMD_MASK].serverTime;
		// else the first one, or the time went back (map_restart) or jumped
		// (a hitch, or under 10 frames a second: one step of all of it, as
		// before, so that a packet with the last one's usercmds
		// (cl_packetdup) stays within MAX_PACKET_USERCMDS): from now on
		if ( elapsed >= 0 && elapsed <= 100 ) {
			step = CL_SubGridStep( interval, elapsed );
			if ( elapsed < step ) {
				return;
			}
			count = 1 + ( elapsed - step ) / interval;
			cmdTime = cl.cmds[cl.cmdNumber & CMD_MASK].serverTime + step;
		} else {
			cmd_sub.step = 0;
			cmd_sub.early = qfalse;
			cmd_sub.prevNow = cl.serverTime;
		}
	}

	// a jump pressed in this millisecond has no share of its usercmd
	// (CL_KeyState), so it is still a starting event after it (CL_SubGridStep)
	upNow = in_up.active && in_up.downtime == (unsigned)com_frameTime;

	frame_msec = com_frameTime - old_com_frameTime;

	// if running over 1000fps, act as if each frame is 1ms
	// prevents divisions by zero
	if ( frame_msec < 1 ) {
		frame_msec = 1;
	}

	// if running less than 5fps, truncate the extra time to prevent
	// unexpected moves after a hitch
	if ( frame_msec > 200 ) {
		frame_msec = 200;
	}
	old_com_frameTime = com_frameTime;

	// generate the commands for this frame
	cmd = CL_CreateCmd ();
	for ( i = 0; i < count; i++ ) {
		if ( i ) {
			// the weapon command goes into a set number of usercmds
			// (CG_WeaponCommandButtonBits)
			cmd.buttons &= ~GetWeaponCommandMask( WEAPON_COMMAND_MAX_VER17 );
			if ( cge ) {
				cmd.buttons |= cge->CG_WeaponCommandButtonBits();
			}
		}
		cmd.serverTime = cmdTime + i * interval;
		cl.cmdNumber++;
		cl.cmds[cl.cmdNumber & CMD_MASK] = cmd;
	}

	// the sub-grid: after an early usercmd the rest of the step, back to the
	// grid's point; the packet of a short step goes at once
	if ( interval ) {
		cmd_sub.send = step < interval;
		cmd_sub.step = cmd_sub.early ? interval - interval / 2 : 0;
		cmd_sub.early = qfalse;
		cmd_sub.upHeld = in_up.active && !upNow;
	}
}

/*
=================
CL_GetViewUserCmd

The newest usercmd with the view angles of this frame, for cgame to turn the
view in the frames between usercmds (CL_ApplyLook). Prediction runs the
usercmds as they were made, the angles they were sent with.
=================
*/
void CL_GetViewUserCmd( usercmd_t *ucmd ) {
	int		i;

	*ucmd = cl.cmds[cl.cmdNumber & CMD_MASK];
	for ( i = 0; i < 3; i++ ) {
		ucmd->angles[i] = ANGLE2SHORT( cl.viewangles[i] );
	}
}

/*
=================
CL_LiveKeyFraction

The share of this cl_movefps interval CL_KeyMove will store if the key stays
as it is until the command is sealed. Does not touch key->msec or downtime
(CL_KeyState does, and the real command still needs them).

projectHold counts the rest of the interval while the key is down. Turn
keys are sampled by CL_AdjustAngles earlier in this frame, so the sealed
command gets about none of them; projecting those would move the picture
and the replay would take it back.
=================
*/
static float CL_LiveKeyFraction( const kbutton_t *key, int interval, int elapsed, qboolean projectHold ) {
	int		msec, remain;

	if ( interval < 1 ) {
		interval = 1;
	}
	remain = interval - elapsed;
	if ( remain < 0 ) {
		remain = 0;
	}

	msec = (int)key->msec;
	if ( key->active ) {
		if ( !key->downtime ) {
			msec = interval;
		} else {
			msec += (int)( com_frameTime - key->downtime );
		}
		if ( msec < 0 ) {
			msec = 0;
		}
		if ( projectHold ) {
			msec += remain;
		}
	}

	if ( msec < 0 ) {
		msec = 0;
	}
	if ( msec > interval ) {
		msec = interval;
	}

	return (float)msec / (float)interval;
}

/*
=================
CL_GetLiveUserCmd

The next usercmd as it goes out if the keys stay as they are: this frame's
view, the moves for its step (the interval, or the sub-grid's,
CL_SubGridStep) and its time. The open interval is one stock Pmove of that
command. Nothing is written into cl.cmds. At cl_movefps 0 there is no open
interval (every frame makes its usercmd): the newest one with this frame's
view.
=================
*/
void CL_GetLiveUserCmd( usercmd_t *ucmd ) {
	int			i, interval, elapsed, movespeed;
	int			forward, side, up;
	unsigned	weapon;

	interval = CL_MoveInterval();
	if ( !interval ) {
		CL_GetViewUserCmd( ucmd );
		return;
	}
	if ( cmd_sub.step ) {
		interval = cmd_sub.step;	// the sub-grid's step
	}

	*ucmd = cl.cmds[cl.cmdNumber & CMD_MASK];

	elapsed = cl.serverTime - ucmd->serverTime;
	if ( elapsed < 0 ) {
		elapsed = 0;
	}
	if ( elapsed > interval ) {
		elapsed = interval;
	}

	movespeed = 127;
	forward = 0;
	side = 0;
	up = 0;
	if ( in_strafe.active ) {
		side += movespeed * CL_LiveKeyFraction( &in_right, interval, elapsed, qfalse );
		side -= movespeed * CL_LiveKeyFraction( &in_left, interval, elapsed, qfalse );
	}
	side += movespeed * CL_LiveKeyFraction( &in_moveright, interval, elapsed, qtrue );
	side -= movespeed * CL_LiveKeyFraction( &in_moveleft, interval, elapsed, qtrue );
	up += movespeed * CL_LiveKeyFraction( &in_up, interval, elapsed, qtrue );
	up -= movespeed * CL_LiveKeyFraction( &in_down, interval, elapsed, qtrue );
	forward += movespeed * CL_LiveKeyFraction( &in_forward, interval, elapsed, qtrue );
	forward -= movespeed * CL_LiveKeyFraction( &in_back, interval, elapsed, qtrue );

	ucmd->forwardmove = ClampChar( forward );
	ucmd->rightmove = ClampChar( side );
	ucmd->upmove = ClampChar( up );

	ucmd->rightmove = ClampChar( ucmd->rightmove + (int)cl.lookMove[0] );
	ucmd->forwardmove = ClampChar( ucmd->forwardmove + (int)cl.lookMove[1] );

	weapon = ucmd->buttons & GetWeaponCommandMask( WEAPON_COMMAND_MAX_VER17 );
	ucmd->buttons = weapon;
	for ( i = 0; i < 15; i++ ) {
		if ( in_buttons[i].active || in_buttons[i].wasPressed ) {
			ucmd->buttons |= 1 << i;
		}
	}
	ucmd->buttons |= weapon;

	if ( UI_MenuActive() || UI_ConsoleIsOpen() ) {
		ucmd->buttons |= BUTTON_TALK;
	}
	if ( in_speed.active ^ cl_run->integer ) {
		ucmd->buttons |= BUTTON_RUN;
	}
	if ( anykeydown && Key_GetCatcher() == 0 ) {
		ucmd->buttons |= BUTTON_MOUSE;
	}
	if ( anykeydown || cl.lookButtons ) {
		ucmd->buttons |= BUTTON_ANY;
	}

	CL_JoystickMove( ucmd );

	for ( i = 0; i < 3; i++ ) {
		ucmd->angles[i] = ANGLE2SHORT( cl.viewangles[i] );
	}
	ucmd->serverTime += interval;
}

/*
=================
CL_EyeInfo

Gather eyes data from client game module
=================
*/
void CL_EyeInfo(usereyes_t* info) {
	int i;
	vec3_t vAngles, vOfs;

	if (cge) {
		// copy the eye offset
		cge->CG_EyeOffset(&vOfs);

		for (i = 0; i < 3; i++) {
			vOfs[i] += 0.5;
			if (vOfs[i] < -127.0) {
				vOfs[i] = -127.0;
			} else if (vOfs[i] > 128.0) {
				vOfs[i] = 128.0;
			}

			info->ofs[i] = vOfs[i];
		}

		cge->CG_EyeAngles(&vAngles);
		info->angles[0] = vAngles[0];
		info->angles[1] = vAngles[1];
	} else {
		// clear the eye offset
		for (i = 0; i < 3; i++) {
			info->ofs[i] = 0;
		}

		info->angles[0] = info->angles[1] = 0.0;
	}
}

/*
=================
CL_ReadyToSendPacket

Returns qfalse if we are over the maxpackets limit
and should choke back the bandwidth a bit by not sending
a packet this frame.  All the commands will still get
delivered in the next packet, but saving a header and
getting more delta compression will reduce total bandwidth.
=================
*/
qboolean CL_ReadyToSendPacket( void ) {
	int		oldPacketNum;
	int		delta;

	// don't send anything if playing back a demo
	if ( clc.demoplaying || clc.state == CA_CINEMATIC ) {
		return qfalse;
	}

	// If we are downloading, we send no less than 50ms between packets
	if ( *clc.downloadTempName &&
		cls.realtime - clc.lastPacketSentTime < 50 ) {
		return qfalse;
	}

	// if we don't have a valid gamestate yet, only send
	// one packet a second
	if ( clc.state != CA_ACTIVE &&
		clc.state != CA_PRIMED &&
		!*clc.downloadTempName &&
		cls.realtime - clc.lastPacketSentTime < 1000 ) {
		return qfalse;
	}

	// send every frame for loopbacks
	if ( clc.netchan.remoteAddress.type == NA_LOOPBACK ) {
		return qtrue;
	}

	// send every frame for LAN
	if ( cl_lanForcePackets->integer && Sys_IsLANAddress( clc.netchan.remoteAddress ) ) {
		return qtrue;
	}

	// check for exceeding cl_maxpackets
	if ( cl_maxpackets->integer < 30 ) {
		Cvar_Set( "cl_maxpackets", "30" );
	} else if ( cl_maxpackets->integer > 125 ) {
		Cvar_Set( "cl_maxpackets", "125" );
	}
	oldPacketNum = (clc.netchan.outgoingSequence - 1) & PACKET_MASK;

	// the sub-grid's usercmds (cl_movesubgrid) go at once: cl_maxpackets
	// would hold one made half a step after the packet before
	if ( cmd_sub.send && cl.cmdNumber != cl.outPackets[ oldPacketNum ].p_cmdNumber ) {
		return qtrue;
	}

	// cl_movefps: a new usercmd waits by the usercmds' own time, the grid's
	// (as on the metal branch). Counted in real time, the time delta's
	// corrections bring two of them closer than their step now and then, or
	// the frames do (7 ms apart at 144 frames a second), and the packet
	// waited a frame. A grid started again (the time went back) sends at
	// once; a packet without a new usercmd counts real time as before
	if ( clc.state == CA_ACTIVE && CL_MoveInterval() && cl.cmdNumber != cl.outPackets[ oldPacketNum ].p_cmdNumber ) {
		delta = cl.cmds[ cl.cmdNumber & CMD_MASK ].serverTime - cl.outPackets[ oldPacketNum ].p_serverTime;
		if ( delta < 0 ) {
			return qtrue;
		}
	} else {
		delta = cls.realtime - cl.outPackets[ oldPacketNum ].p_realtime;
	}
	if ( delta < 1000 / cl_maxpackets->integer ) {
		// the accumulated commands will go out in the next packet
		return qfalse;
	}

	return qtrue;
}

/*
===================
CL_WritePacket

Create and send the command packet to the server
Including both the reliable commands and the usercmds

During normal gameplay, a client packet will contain something like:

4	sequence number
2	qport
4	serverid
4	acknowledged sequence number
4	clc.serverCommandSequence
<optional reliable commands>
1	clc_move or clc_moveNoDelta
1	command count
<count * usercmds>

===================
*/
void CL_WritePacket( void ) {
	msg_t		buf;
	byte		data[MAX_MSGLEN];
	int			i, j;
	usercmd_t	*cmd, *oldcmd;
	usercmd_t	nullcmd;
	int			packetNum;
	int			oldPacketNum;
	int			count, key;
	usereyes_t	eyeInfo;

	// don't send anything if playing back a demo
	if ( clc.demoplaying || clc.state == CA_CINEMATIC ) {
		return;
	}

	Com_Memset( &nullcmd, 0, sizeof(nullcmd) );
	oldcmd = &nullcmd;		
		
	MSG_Init( &buf, data, sizeof(data) );

	MSG_Bitstream( &buf );
	// write the current serverId so the server
	// can tell if this is from the current gameState
	MSG_WriteLong( &buf, cl.serverId );

	// write the last message we received, which can
	// be used for delta compression, and is also used
	// to tell if we dropped a gamestate
	MSG_WriteLong( &buf, clc.serverMessageSequence );

	// write the last reliable message we received
	MSG_WriteLong( &buf, clc.serverCommandSequence );

	// write any unacknowledged clientCommands
	for ( i = clc.reliableAcknowledge + 1 ; i <= clc.reliableSequence ; i++ ) {
		MSG_WriteByte( &buf, clc_clientCommand );
		MSG_WriteLong( &buf, i );
		MSG_WriteScrambledString( &buf, clc.reliableCommands[ i & (MAX_RELIABLE_COMMANDS-1) ] );
	}

	// we want to send all the usercmds that were generated in the last
	// few packet, so even if a couple packets are dropped in a row,
	// all the cmds will make it to the server
	if ( cl_packetdup->integer < 0 ) {
		Cvar_Set( "cl_packetdup", "0" );
	} else if ( cl_packetdup->integer > 5 ) {
		Cvar_Set( "cl_packetdup", "5" );
	}
	oldPacketNum = (clc.netchan.outgoingSequence - 1 - cl_packetdup->integer) & PACKET_MASK;
	count = cl.cmdNumber - cl.outPackets[ oldPacketNum ].p_cmdNumber;
	if ( count > MAX_PACKET_USERCMDS ) {
		count = MAX_PACKET_USERCMDS;
		Com_Printf("MAX_PACKET_USERCMDS\n");
	}

#ifdef USE_VOIP
	if (clc.voipOutgoingDataSize > 0)
	{
		if((clc.voipFlags & VOIP_SPATIAL) || Com_IsVoipTarget(clc.voipTargets, sizeof(clc.voipTargets), -1))
		{
			MSG_WriteByte (&buf, clc_voipOpus);
			MSG_WriteByte (&buf, clc.voipOutgoingGeneration);
			MSG_WriteLong (&buf, clc.voipOutgoingSequence);
			MSG_WriteByte (&buf, clc.voipOutgoingDataFrames);
			MSG_WriteData (&buf, clc.voipTargets, sizeof(clc.voipTargets));
			MSG_WriteByte(&buf, clc.voipFlags);
			MSG_WriteShort (&buf, clc.voipOutgoingDataSize);
			MSG_WriteData (&buf, clc.voipOutgoingData, clc.voipOutgoingDataSize);

			// If we're recording a demo, we have to fake a server packet with
			//  this VoIP data so it gets to disk; the server doesn't send it
			//  back to us, and we might as well eliminate concerns about dropped
			//  and misordered packets here.
			if(clc.demorecording && !clc.demowaiting)
			{
				const int voipSize = clc.voipOutgoingDataSize;
				msg_t fakemsg;
				byte fakedata[MAX_MSGLEN];
				MSG_Init (&fakemsg, fakedata, sizeof (fakedata));
				MSG_Bitstream (&fakemsg);
				MSG_WriteLong (&fakemsg, clc.reliableAcknowledge);
				MSG_WriteByte (&fakemsg, svc_voipOpus);
				MSG_WriteShort (&fakemsg, clc.clientNum);
				MSG_WriteByte (&fakemsg, clc.voipOutgoingGeneration);
				MSG_WriteLong (&fakemsg, clc.voipOutgoingSequence);
				MSG_WriteByte (&fakemsg, clc.voipOutgoingDataFrames);
				MSG_WriteShort (&fakemsg, clc.voipOutgoingDataSize );
				MSG_WriteBits (&fakemsg, clc.voipFlags, VOIP_FLAGCNT);
				MSG_WriteData (&fakemsg, clc.voipOutgoingData, voipSize);
				MSG_WriteByte (&fakemsg, svc_EOF);
				CL_WriteDemoMessage (&fakemsg, 0);
			}

			clc.voipOutgoingSequence += clc.voipOutgoingDataFrames;
			clc.voipOutgoingDataSize = 0;
			clc.voipOutgoingDataFrames = 0;
		}
		else
		{
			// We have data, but no targets. Silently discard all data
			clc.voipOutgoingDataSize = 0;
			clc.voipOutgoingDataFrames = 0;
		}
	}
#endif

	if ( count >= 1 ) {
		if ( cl_showSend->integer ) {
			Com_Printf( "(%i)", count );
		}

		// begin a client move command
		if ( cl_nodelta->integer || !cl.snap.valid || clc.demowaiting
			|| clc.serverMessageSequence != cl.snap.messageNum ) {
			MSG_WriteByte (&buf, clc_moveNoDelta);
		} else {
			MSG_WriteByte (&buf, clc_move);
		}

		// write the command count
		MSG_WriteByte( &buf, count );

		CL_EyeInfo( &eyeInfo );
		MSG_WriteDeltaEyeInfo( &buf, &cl.outPackets[ oldPacketNum ].p_eyeinfo, &eyeInfo );
		// use the checksum feed in the key
		key = clc.checksumFeed;
		// also use the message acknowledge
		key ^= clc.serverMessageSequence;
		// also use the last acknowledged server command in the key
		key ^= Com_HashKey(clc.serverCommands[ clc.serverCommandSequence & (MAX_RELIABLE_COMMANDS-1) ], 32);

		// write all the commands, including the predicted command
		for ( i = 0 ; i < count ; i++ ) {
			j = (cl.cmdNumber - count + i + 1) & CMD_MASK;
			cmd = &cl.cmds[j];
			MSG_WriteDeltaUsercmdKey (&buf, key, oldcmd, cmd);
			oldcmd = cmd;
		}
	}
	else {
        eyeInfo.ofs[0] = 0;
        eyeInfo.ofs[1] = 0;
        eyeInfo.ofs[2] = 0;
        eyeInfo.angles[0] = cl.viewangles[0];
        eyeInfo.angles[1] = cl.viewangles[1];
	}

	//
	// deliver the message
	//
	packetNum = clc.netchan.outgoingSequence & PACKET_MASK;
	cl.outPackets[ packetNum ].p_realtime = cls.realtime;
	cl.outPackets[ packetNum ].p_serverTime = oldcmd->serverTime;
	cl.outPackets[ packetNum ].p_cmdNumber = cl.cmdNumber;
	cl.outPackets[ packetNum ].p_eyeinfo = eyeInfo;
	clc.lastPacketSentTime = cls.realtime;

	if ( cl_showSend->integer ) {
		Com_Printf( "%zu ", buf.cursize );
	}

	CL_Netchan_Transmit (&clc.netchan, &buf);
}

/*
=================
CL_SendCmd

Called every frame to builds and sends a command packet to the server.
=================
*/
void CL_SendCmd( void ) {
	// don't send any message if not connected
	if ( clc.state < CA_CONNECTED ) {
		return;
	}

	// don't send commands if paused
	if ( com_sv_running->integer && paused->integer  ) {
		return;
	}

	// the view turns every frame, usercmds come at cl_movefps (every frame at 0)
	if ( clc.state >= CA_PRIMED ) {
		CL_ApplyLook();
	}

	// we create commands even if a demo is playing,
	CL_CreateNewCommands();

	// nothing new for the server: no usercmd since the last packet and no
	// reliable command waiting
	if ( clc.state == CA_ACTIVE
		&& cl.cmdNumber == cl.outPackets[ ( clc.netchan.outgoingSequence - 1 ) & PACKET_MASK ].p_cmdNumber
		&& clc.reliableSequence <= clc.reliableAcknowledge ) {
		return;
	}

	// don't send a packet if the last packet was sent too recently
	if ( !CL_ReadyToSendPacket() ) {
		if ( cl_showSend->integer ) {
			Com_Printf( ". " );
		}
		return;
	}

	CL_WritePacket();
}

/*
============
CL_InitInput
============
*/
void CL_InitInput( void ) {
	Cmd_AddCommand("centerview", IN_CenterView);

	Cmd_AddCommand("+moveup", IN_UpDown);
	Cmd_AddCommand("-moveup", IN_UpUp);
	Cmd_AddCommand("+movedown", IN_DownDown);
	Cmd_AddCommand("-movedown", IN_DownUp);
	Cmd_AddCommand("+left", IN_LeftDown);
	Cmd_AddCommand("-left", IN_LeftUp);
	Cmd_AddCommand("+right", IN_RightDown);
	Cmd_AddCommand("-right", IN_RightUp);
	Cmd_AddCommand("+forward", IN_ForwardDown);
	Cmd_AddCommand("-forward", IN_ForwardUp);
	Cmd_AddCommand("+back", IN_BackDown);
	Cmd_AddCommand("-back", IN_BackUp);
	Cmd_AddCommand("+lookup", IN_LookupDown);
	Cmd_AddCommand("-lookup", IN_LookupUp);
	Cmd_AddCommand("+lookdown", IN_LookdownDown);
	Cmd_AddCommand("-lookdown", IN_LookdownUp);
	Cmd_AddCommand("+strafe", IN_StrafeDown);
	Cmd_AddCommand("-strafe", IN_StrafeUp);
	Cmd_AddCommand("+moveleft", IN_MoveleftDown);
	Cmd_AddCommand("-moveleft", IN_MoveleftUp);
	Cmd_AddCommand("+moveright", IN_MoverightDown);
	Cmd_AddCommand("-moveright", IN_MoverightUp);
	Cmd_AddCommand("+attack", IN_AttackPrimaryDown);
	Cmd_AddCommand("-attack", IN_AttackPrimaryUp);
	Cmd_AddCommand("+attackprimary", IN_AttackPrimaryDown);
	Cmd_AddCommand("-attackprimary", IN_AttackPrimaryUp);
	Cmd_AddCommand("+attacksecondary", IN_AttackSecondaryDown);
	Cmd_AddCommand("-attacksecondary", IN_AttackSecondaryUp);
	Cmd_AddCommand("+use", IN_Button3Down);
	Cmd_AddCommand("-use", IN_Button3Up);
	Cmd_AddCommand("+leanleft", IN_LeanLeftDown);
	Cmd_AddCommand("-leanleft", IN_LeanLeftUp);
	Cmd_AddCommand("+leanright", IN_LeanRightDown);
	Cmd_AddCommand("-leanright", IN_LeanRightUp);
	Cmd_AddCommand("+speed", IN_SpeedDown);
	Cmd_AddCommand("-speed", IN_SpeedUp);
	Cmd_AddCommand("+button0", IN_Button0Down);
	Cmd_AddCommand("-button0", IN_Button0Up);
	Cmd_AddCommand("+button1", IN_Button1Down);
	Cmd_AddCommand("-button1", IN_Button1Up);
	Cmd_AddCommand("+button2", IN_Button2Down);
	Cmd_AddCommand("-button2", IN_Button2Up);
	Cmd_AddCommand("+button3", IN_Button3Down);
	Cmd_AddCommand("-button3", IN_Button3Up);
	Cmd_AddCommand("+button4", IN_Button4Down);
	Cmd_AddCommand("-button4", IN_Button4Up);
	Cmd_AddCommand("+button5", IN_Button5Down);
	Cmd_AddCommand("-button5", IN_Button5Up);
	Cmd_AddCommand("+button6", IN_Button6Down);
	Cmd_AddCommand("-button6", IN_Button6Up);
	Cmd_AddCommand("+button7", IN_Button7Down);
	Cmd_AddCommand("-button7", IN_Button7Up);
	Cmd_AddCommand("+button8", IN_Button8Down);
	Cmd_AddCommand("-button8", IN_Button8Up);
	Cmd_AddCommand("+button9", IN_Button9Down);
	Cmd_AddCommand("-button9", IN_Button9Up);
	Cmd_AddCommand("+button10", IN_Button10Down);
	Cmd_AddCommand("-button10", IN_Button10Up);
	Cmd_AddCommand("+button11", IN_Button11Down);
	Cmd_AddCommand("-button11", IN_Button11Up);
	Cmd_AddCommand("+button12", IN_Button12Down);
	Cmd_AddCommand("-button12", IN_Button12Up);
	Cmd_AddCommand("+button13", IN_Button13Down);
	Cmd_AddCommand("-button13", IN_Button13Up);
	Cmd_AddCommand("+button14", IN_Button14Down);
	Cmd_AddCommand("-button14", IN_Button14Up);
	Cmd_AddCommand("+mlook", IN_MLookDown);
	Cmd_AddCommand("-mlook", IN_MLookUp);

#ifdef USE_VOIP
	Cmd_AddCommand ("+voiprecord", IN_VoipRecordDown);
	Cmd_AddCommand ("-voiprecord", IN_VoipRecordUp);
#endif

	cl_nodelta = Cvar_Get ("cl_nodelta", "0", 0);
	cl_debugMove = Cvar_Get ("cl_debugMove", "0", 0);
	cl_movefps = Cvar_Get ("cl_movefps", "100", CVAR_ARCHIVE);
	cl_movesubgrid = Cvar_Get ("cl_movesubgrid", "1", CVAR_ARCHIVE);
	cl_nullbinds = Cvar_Get ("cl_nullbinds", "0", CVAR_ARCHIVE);
}

/*
============
CL_ShutdownInput
============
*/
void CL_ShutdownInput(void)
{
	Cmd_RemoveCommand("centerview");

	Cmd_RemoveCommand("+moveup");
	Cmd_RemoveCommand("-moveup");
	Cmd_RemoveCommand("+movedown");
	Cmd_RemoveCommand("-movedown");
	Cmd_RemoveCommand("+left");
	Cmd_RemoveCommand("-left");
	Cmd_RemoveCommand("+right");
	Cmd_RemoveCommand("-right");
	Cmd_RemoveCommand("+forward");
	Cmd_RemoveCommand("-forward");
	Cmd_RemoveCommand("+back");
	Cmd_RemoveCommand("-back");
	Cmd_RemoveCommand("+lookup");
	Cmd_RemoveCommand("-lookup");
	Cmd_RemoveCommand("+lookdown");
	Cmd_RemoveCommand("-lookdown");
	Cmd_RemoveCommand("+strafe");
	Cmd_RemoveCommand("-strafe");
	Cmd_RemoveCommand("+moveleft");
	Cmd_RemoveCommand("-moveleft");
	Cmd_RemoveCommand("+moveright");
	Cmd_RemoveCommand("-moveright");
	Cmd_RemoveCommand("+speed");
	Cmd_RemoveCommand("-speed");
	Cmd_RemoveCommand("+attack");
	Cmd_RemoveCommand("-attack");
	Cmd_RemoveCommand("+button0");
	Cmd_RemoveCommand("-button0");
	Cmd_RemoveCommand("+button1");
	Cmd_RemoveCommand("-button1");
	Cmd_RemoveCommand("+button2");
	Cmd_RemoveCommand("-button2");
	Cmd_RemoveCommand("+button3");
	Cmd_RemoveCommand("-button3");
	Cmd_RemoveCommand("+button4");
	Cmd_RemoveCommand("-button4");
	Cmd_RemoveCommand("+button5");
	Cmd_RemoveCommand("-button5");
	Cmd_RemoveCommand("+button6");
	Cmd_RemoveCommand("-button6");
	Cmd_RemoveCommand("+button7");
	Cmd_RemoveCommand("-button7");
	Cmd_RemoveCommand("+button8");
	Cmd_RemoveCommand("-button8");
	Cmd_RemoveCommand("+button9");
	Cmd_RemoveCommand("-button9");
	Cmd_RemoveCommand("+button10");
	Cmd_RemoveCommand("-button10");
	Cmd_RemoveCommand("+button11");
	Cmd_RemoveCommand("-button11");
	Cmd_RemoveCommand("+button12");
	Cmd_RemoveCommand("-button12");
	Cmd_RemoveCommand("+button13");
	Cmd_RemoveCommand("-button13");
	Cmd_RemoveCommand("+button14");
	Cmd_RemoveCommand("-button14");
	Cmd_RemoveCommand("+mlook");
	Cmd_RemoveCommand("-mlook");

#ifdef USE_VOIP
	Cmd_RemoveCommand("+voiprecord");
	Cmd_RemoveCommand("-voiprecord");
#endif
}
