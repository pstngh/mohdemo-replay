#include "../gserverlist_scheduler.h"

#undef NDEBUG // the checks are asserts, in every build type
#include <cassert>
#include <cstdio>

static void test_claim_order_and_refill()
{
	GServerListScheduler scheduler;
	int                  i;

	GServerListSchedulerInit(&scheduler);

	for (i = 0; i < 32; i++) {
		assert(GServerListSchedulerClaim(&scheduler, 150) == i);
	}

	for (i = 0; i < 31; i++) {
		GServerListSchedulerComplete(&scheduler, 1);
	}
	GServerListSchedulerComplete(&scheduler, 0);

	assert(scheduler.completed == 32);
	assert(scheduler.responsive == 31);
	assert(scheduler.timedout == 1);
	assert(GServerListSchedulerClaim(&scheduler, 150) == 32);
}

static void test_late_master_arrivals()
{
	GServerListScheduler scheduler;

	GServerListSchedulerInit(&scheduler);

	assert(GServerListSchedulerClaim(&scheduler, 2) == 0);
	assert(GServerListSchedulerClaim(&scheduler, 2) == 1);
	assert(GServerListSchedulerClaim(&scheduler, 2) == -1);
	// Outstanding queries keep it busy.
	assert(!GServerListSchedulerDone(&scheduler, 2, 2));

	// A master lists one more server.
	assert(GServerListSchedulerClaim(&scheduler, 3) == 2);
	assert(!GServerListSchedulerDone(&scheduler, 3, 3));

	GServerListSchedulerComplete(&scheduler, 1);
	GServerListSchedulerComplete(&scheduler, 1);
	GServerListSchedulerComplete(&scheduler, 1);
	assert(GServerListSchedulerDone(&scheduler, 3, 0));
	// and one more after that: not done until it is queried
	assert(!GServerListSchedulerDone(&scheduler, 4, 0));
}

static void test_retry_then_timeout()
{
	GServerListScheduler scheduler;

	GServerListSchedulerInit(&scheduler);

	assert(GServerListSchedulerClaim(&scheduler, 1) == 0);
	GServerListSchedulerQueueRetry(&scheduler);
	assert(GServerListSchedulerClaim(&scheduler, 1) == -1);
	assert(GServerListSchedulerClaimRetry(&scheduler) == 0);
	assert(GServerListSchedulerClaimRetry(&scheduler) == -1);
	assert(scheduler.completed == 0);

	GServerListSchedulerComplete(&scheduler, 0);
	assert(scheduler.timedout == 1);
	assert(GServerListSchedulerDone(&scheduler, 1, 0));

	GServerListSchedulerInit(&scheduler);
	assert(scheduler.retryCount == 0);
	assert(scheduler.timedout == 0);
}

static void test_successful_retry()
{
	GServerListScheduler scheduler;

	GServerListSchedulerInit(&scheduler);

	assert(GServerListSchedulerClaim(&scheduler, 1) == 0);
	GServerListSchedulerQueueRetry(&scheduler);
	// the retry is still to be sent
	assert(!GServerListSchedulerDone(&scheduler, 1, 0));
	assert(GServerListSchedulerClaimRetry(&scheduler) == 0);
	GServerListSchedulerComplete(&scheduler, 1);

	assert(scheduler.completed == 1);
	assert(scheduler.responsive == 1);
	assert(scheduler.timedout == 0);
	assert(GServerListSchedulerDone(&scheduler, 1, 0));
}

static void test_single_server_update()
{
	GServerListScheduler scheduler;

	// ServerListAuxUpdate on an idle list of 50 servers: only the last one
	GServerListSchedulerInit(&scheduler);
	scheduler.next = 49;

	assert(GServerListSchedulerClaim(&scheduler, 50) == 49);
	assert(!GServerListSchedulerDone(&scheduler, 50, 1));
	GServerListSchedulerComplete(&scheduler, 1);
	assert(GServerListSchedulerDone(&scheduler, 50, 0));
}

int main()
{
	test_claim_order_and_refill();
	test_late_master_arrivals();
	test_retry_then_timeout();
	test_successful_retry();
	test_single_server_update();

	std::printf("test_serverlist_scheduler: all tests passed\n");
	return 0;
}
