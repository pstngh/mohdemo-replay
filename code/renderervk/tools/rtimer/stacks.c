/*
 * libstacks.so -- wall-clock stack sampler for the game's main thread
 *
 *   STACKS_OUT=file LD_PRELOAD=libstacks.so ./openmohaa ...
 *
 * Interrupts the thread that loaded it (the main thread) every
 * STACKS_INTERVAL_US microseconds (default 1000) of wall-clock time on
 * average, whether it runs or waits, and records the time (CLOCK_MONOTONIC)
 * and its backtrace. The intervals are random between half and one and a
 * half of that: at a fixed 1 ms the samples lock to the phase of frames
 * capped at 1000 per second and always land in the same part of the frame. A helper thread copies new lines of /proc/self/maps every
 * 200 ms (the game unloads the renderer and the Vulkan driver before it
 * exits). At exit it writes the mappings and the samples to STACKS_OUT;
 * tools/stacks.py symbolizes and sums them. Needs no
 * privileges, unlike perf with kernel.perf_event_paranoid > 2; blocked
 * time (fence waits, poll, futexes) shows up where perf's CPU samples
 * would show nothing. Build with tools/rtimer/build.sh.
 */

#define _GNU_SOURCE
#include <execinfo.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define MAX_DEPTH	32

typedef struct {
	uint64_t	ns;
	int			depth;
	void		*pc[MAX_DEPTH];
} sample_t;

static sample_t		*samples;
static int			numSamples, maxSamples;
static timer_t		timer;
static const char	*outPath;
static long			interval;		// microseconds
static uint32_t		rng = 2463534242u;

// every line seen in /proc/self/maps
static char			**mapLines;
static int			numMapLines, maxMapLines;
static pthread_mutex_t	mapLock = PTHREAD_MUTEX_INITIALIZER;

static void ReadMaps( void ) {
	char line[4096];
	FILE *f;
	int i;

	f = fopen( "/proc/self/maps", "r" );
	if ( !f ) {
		return;
	}
	pthread_mutex_lock( &mapLock );
	while ( fgets( line, sizeof( line ), f ) ) {
		if ( !strchr( line, '/' ) ) {
			continue;
		}
		for ( i = numMapLines - 1; i >= 0 && strcmp( mapLines[i], line ); i-- ) {
		}
		if ( i >= 0 ) {
			continue;
		}
		if ( numMapLines == maxMapLines ) {
			maxMapLines = maxMapLines ? maxMapLines * 2 : 1024;
			mapLines = realloc( mapLines, sizeof( *mapLines ) * maxMapLines );
		}
		mapLines[numMapLines++] = strdup( line );
	}
	pthread_mutex_unlock( &mapLock );
	fclose( f );
}

static void *MapThread( void *arg ) {
	sigset_t set;

	// the samples are for the main thread only
	sigemptyset( &set );
	sigaddset( &set, SIGRTMIN + 7 );
	pthread_sigmask( SIG_BLOCK, &set, NULL );
	for ( ;; ) {
		ReadMaps();
		usleep( 200000 );
	}
	return NULL;
}

// arms the timer for the next sample, interval / 2 to interval * 3 / 2 away
static void Arm( void ) {
	struct itimerspec its;

	rng ^= rng << 13;
	rng ^= rng >> 17;
	rng ^= rng << 5;
	memset( &its, 0, sizeof( its ) );
	its.it_value.tv_nsec = ( interval / 2 + rng % ( interval + 1 ) ) * 1000;
	timer_settime( timer, 0, &its, NULL );
}

static void Handler( int sig, siginfo_t *info, void *context ) {
	struct timespec ts;
	sample_t *s;

	if ( numSamples >= maxSamples ) {
		return;
	}
	Arm();
	s = &samples[numSamples++];
	clock_gettime( CLOCK_MONOTONIC, &ts );
	s->ns = (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
	s->depth = backtrace( s->pc, MAX_DEPTH );
}

static void Write( void ) {
	FILE *f;
	int i, j;

	timer_delete( timer );
	ReadMaps();
	f = fopen( outPath, "w" );
	if ( !f ) {
		return;
	}
	pthread_mutex_lock( &mapLock );
	for ( i = 0; i < numMapLines; i++ ) {
		fprintf( f, "map %s", mapLines[i] );
	}
	pthread_mutex_unlock( &mapLock );
	for ( i = 0; i < numSamples; i++ ) {
		fprintf( f, "s %llu", (unsigned long long)samples[i].ns );
		for ( j = 0; j < samples[i].depth; j++ ) {
			fprintf( f, " %lx", (unsigned long)samples[i].pc[j] );
		}
		fputc( '\n', f );
	}
	fclose( f );
}

__attribute__(( constructor )) static void Start( void ) {
	struct sigaction sa;
	struct sigevent sev;
	const char *s;
	void *dummy[4];

	outPath = getenv( "STACKS_OUT" );
	if ( !outPath || !*outPath ) {
		return;
	}
	s = getenv( "STACKS_INTERVAL_US" );
	interval = s ? atol( s ) : 1000;
	if ( interval < 100 || interval > 500000 ) {
		interval = 1000;
	}

	// at most 200000 samples (200 s at the default interval); the pages
	// are only touched as samples are taken
	maxSamples = 200000;
	samples = malloc( sizeof( sample_t ) * maxSamples );
	if ( !samples ) {
		return;
	}
	// loads the unwinder outside of the signal handler
	backtrace( dummy, 4 );

	memset( &sa, 0, sizeof( sa ) );
	sa.sa_sigaction = Handler;
	sa.sa_flags = SA_SIGINFO | SA_RESTART;
	sigemptyset( &sa.sa_mask );
	sigaction( SIGRTMIN + 7, &sa, NULL );

	memset( &sev, 0, sizeof( sev ) );
	sev.sigev_notify = SIGEV_THREAD_ID;
	sev.sigev_signo = SIGRTMIN + 7;
	sev._sigev_un._tid = (pid_t)syscall( SYS_gettid );
	if ( timer_create( CLOCK_MONOTONIC, &sev, &timer ) ) {
		return;
	}
	Arm();

	{
		pthread_t thread;

		pthread_create( &thread, NULL, MapThread, NULL );
	}
	atexit( Write );
}
