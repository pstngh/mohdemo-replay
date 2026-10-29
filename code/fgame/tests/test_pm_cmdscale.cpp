/*
===========================================================================
Copyright (C) 2026 the OpenMoHAA team

This file is part of OpenMoHAA source code.

OpenMoHAA source code is free software; you can redistribute it
and/or modify it under the terms of the GNU General Public License as
published by the Free Software Foundation; either version 2 of the License,
or (at your option) any later version.
===========================================================================
*/

// Wish speeds must match AA 1.11, SH 2.15 and BT 2.40, or stock clients
// mispredict whenever they backpedal or strafe. PM_CmdScale is static, so
// the source is included directly.

#include "../bg_pmove.cpp"

#include <cmath>
#include <cstdio>

static int failures;

#define CHECK_NEAR(value, expected)                                             \
    do {                                                                        \
        double got = (value);                                                   \
        if (fabs(got - (expected)) > 0.001) {                                   \
            fprintf(                                                            \
                stderr, "FAIL %s:%d: %s = %.4f, want %.3f\n", __FILE__, __LINE__, \
                #value, got, (double)(expected)                                 \
            );                                                                  \
            failures++;                                                         \
        }                                                                       \
    } while (0)

// Wish speed as a fraction of ps->speed, built the way PM_WalkMove and
// PM_AirMove build it.
static float WishFraction(int forward, int right)
{
    playerState_t ps    = {};
    pmove_t       pmove = {};
    float         fmove, smove;

    ps.speed              = 250;
    pmove.ps              = &ps;
    pmove.cmd.forwardmove = forward;
    pmove.cmd.rightmove   = right;
    pm                    = &pmove;

    PM_GetMove(&fmove, &smove);
    return sqrt(fmove * fmove + smove * smove) * PM_CmdScale(&pmove.cmd) / ps.speed;
}

int main()
{
    failures = 0;

    CHECK_NEAR(WishFraction(127, 0), 1.000);
    CHECK_NEAR(WishFraction(-127, 0), 0.636);
    CHECK_NEAR(WishFraction(0, 127), 0.716);
    CHECK_NEAR(WishFraction(0, -127), 0.716);
    CHECK_NEAR(WishFraction(127, 127), 0.928);
    CHECK_NEAR(WishFraction(-127, 127), 0.695);
    CHECK_NEAR(WishFraction(0, 0), 0.000);

    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }

    printf("test_pm_cmdscale: ok\n");
    return 0;
}
