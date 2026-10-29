#include "gserverlist_scheduler.h"

void GServerListSchedulerInit(GServerListScheduler *scheduler)
{
	scheduler->next       = 0;
	scheduler->completed  = 0;
	scheduler->responsive = 0;
	scheduler->timedout   = 0;
	scheduler->retryCount = 0;
	scheduler->nextRetry  = 0;
}

// index of the next server to query for the first time, -1: none
int GServerListSchedulerClaim(GServerListScheduler *scheduler, int discovered)
{
	if (scheduler->next >= discovered) {
		return -1;
	}
	return scheduler->next++;
}

void GServerListSchedulerComplete(GServerListScheduler *scheduler, int responsive)
{
	scheduler->completed++;
	if (responsive) {
		scheduler->responsive++;
	} else {
		scheduler->timedout++;
	}
}

void GServerListSchedulerQueueRetry(GServerListScheduler *scheduler)
{
	scheduler->retryCount++;
}

// index in the retry queue of the next server to query again, -1: none
int GServerListSchedulerClaimRetry(GServerListScheduler *scheduler)
{
	if (scheduler->nextRetry >= scheduler->retryCount) {
		return -1;
	}
	return scheduler->nextRetry++;
}

// every server listed so far was queried, every query ended (none active)
int GServerListSchedulerDone(const GServerListScheduler *scheduler, int discovered, int active)
{
	return scheduler->next >= discovered && scheduler->nextRetry >= scheduler->retryCount && active == 0;
}
