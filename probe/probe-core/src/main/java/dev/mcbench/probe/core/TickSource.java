package dev.mcbench.probe.core;

/**
 * What a tick duration sample actually measures.
 *
 * <p>The interval between consecutive end-of-tick callbacks is the tick <em>period</em>, not
 * the cost of the tick: on an unsaturated 20 TPS server the loop sleeps out the remainder of
 * every tick, so the interval tends to 50 ms whether the work took 5 ms or 30 ms. Published as
 * MSPT it made {@code mspt_mean}, the percentiles and {@code tick_headroom} measurements of the
 * scheduler. Under tick warp or overload the interval does approach execution time, which is
 * why the error survives casual testing.
 *
 * <p>The source travels with every sample, and the harness publishes differently named metrics
 * for each. A platform that cannot measure execution time reports {@code tick_period_*}.
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
