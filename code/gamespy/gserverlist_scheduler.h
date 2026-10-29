#ifndef _GSERVERLIST_SCHEDULER_H
#define _GSERVERLIST_SCHEDULER_H

#ifdef __cplusplus
extern "C" {
#endif

/*
 * The order of a server list's status queries: the servers in the order the
 * masters list them, first queries before the second ones of the servers that
 * did not answer the first (gserverlist.c), and what the queries came to.
 */
typedef struct GServerListScheduler {
	int next;       // the next server to query for the first time
	int completed;  // servers that answered or timed out twice
	int responsive;
	int timedout;
	int retryCount; // servers queued for a second query
	int nextRetry;  // the next of them to query
} GServerListScheduler;

void GServerListSchedulerInit(GServerListScheduler *scheduler);
int  GServerListSchedulerClaim(GServerListScheduler *scheduler, int discovered);
void GServerListSchedulerComplete(GServerListScheduler *scheduler, int responsive);
void GServerListSchedulerQueueRetry(GServerListScheduler *scheduler);
int  GServerListSchedulerClaimRetry(GServerListScheduler *scheduler);
int  GServerListSchedulerDone(const GServerListScheduler *scheduler, int discovered, int active);

#ifdef __cplusplus
}
#endif

#endif
