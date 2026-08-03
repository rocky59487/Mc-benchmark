package dev.mcbench.probe.core;

/**
 * What a tick duration sample actually measures.
 *
 * <p>This distinction exists because ignoring it invalidated the primary server metrics. The
 * probe used to record {@code System.nanoTime()} between consecutive end-of-tick callbacks and
 * publish the result as MSPT. That is the tick <em>period</em>, not the cost of the tick: on an
 * unsaturated 20 TPS server the loop sleeps out the remainder of every tick, so the interval
 * tends to 50 ms whether the work inside took 5 ms or 30 ms.
 *
 * <p>The consequence is not a small bias. It means {@code mspt_mean}, {@code mspt_p95},
 * {@code mspt_p99} and {@code tick_headroom} were all measuring the scheduler, and the
 * documented claim that headroom distinguishes a 5 ms tick from a 30 ms one under ordinary
 * operation was false — the two produce identical numbers. Under tick warp or overload the
 * interval does approach execution time, which is exactly why the error survived: it looks
 * right in the cases people test.
 *
 * <p>So the source travels with every sample, and the harness publishes differently named
 * metrics for each. A platform that cannot expose tick execution time reports
 * {@code tick_period_*}, which is an honest measurement of something else, rather than an MSPT
 * figure that is not one.
 */
public enum TickSource {
    /** Bracketed start-to-end of the tick. This is MSPT. */
    BRACKET("bracket"),
    /**
     * Interval between consecutive end-of-tick callbacks. Includes whatever the server loop
     * waits out, so it is a lower bound on TPS rather than a measure of tick cost.
     */
    PERIOD("period"),
    /** Duration reported by a platform API that measures the tick itself. */
    PLATFORM("platform");

    private final String wireName;

    TickSource(String wireName) {
        this.wireName = wireName;
    }

    public String wireName() {
        return wireName;
    }

    /** Whether samples from this source may be published as MSPT. */
    public boolean measuresExecution() {
        return this != PERIOD;
    }
}
