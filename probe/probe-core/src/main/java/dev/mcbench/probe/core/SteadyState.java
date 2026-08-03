package dev.mcbench.probe.core;

import java.util.Arrays;

/**
 * Detects when a timing series has stopped trending.
 *
 * <p>The JVM is the single largest confound in Minecraft benchmarking: the first seconds of any
 * run measure the interpreter and the JIT compiler rather than the mod. A fixed warmup duration
 * is a guess, and a wrong guess in either direction is costly — too short charges the mod for
 * the JIT's work, too long wastes minutes on every one of dozens of runs.
 *
 * <p>The test is the one specified in {@code docs/METHODOLOGY.md} section 2: compare the median
 * of the most recent window against the median of the window before it. When consecutive
 * windows agree within a tolerance, the series has plateaued.
 *
 * <p>A median rather than a mean, because warmup is dominated by exactly the sort of enormous
 * outliers — a 500 ms compilation pause — that drag a mean around long after the typical frame
 * has settled.
 *
 * <p>Non-convergence is a real outcome, not an error. When it happens the caller must flag the
 * run {@code warmup_not_converged} rather than silently accept it.
 */
public final class SteadyState {

    private final int window;
    private final double tolerance;
    private final long[] ring;
    private final long[] scratch;
    private int written;

    /**
     * @param window    samples per comparison window
     * @param tolerance maximum relative difference between consecutive window medians
     */
    public SteadyState(int window, double tolerance) {
        if (window < 2) {
            throw new IllegalArgumentException("window must be at least 2, got " + window);
        }
        if (tolerance <= 0 || tolerance >= 1) {
            throw new IllegalArgumentException("tolerance must be in (0,1), got " + tolerance);
        }
        this.window = window;
        this.tolerance = tolerance;
        // Two windows retained: the current one and the one it is compared against.
        this.ring = new long[window * 2];
        this.scratch = new long[window];
    }

    /** Feed one sample. Cheap enough to call per frame. */
    public void accept(long value) {
        ring[(int) (written % ring.length)] = value;
        written++;
    }

    /**
     * Discard every sample seen so far.
     *
     * <p>Called when warmup begins, so that samples taken while the scenario's setup commands
     * were running cannot fill the comparison windows. Setup work is often very uniform — a
     * long run of {@code /setblock} calls costs about the same each tick — so without this the
     * window arrives at warmup already looking perfectly plateaued and the gate opens on the
     * first sample.
     */
    public void reset() {
        written = 0;
        java.util.Arrays.fill(ring, 0L);
    }

    /** Whether enough samples have accumulated to make a judgement. */
    public boolean hasEnoughSamples() {
        return written >= ring.length;
    }

    /**
     * @return true when the two most recent windows agree within tolerance
     */
    public boolean isSteady() {
        if (!hasEnoughSamples()) {
            return false;
        }
        long previous = medianOfWindow(1);
        long current = medianOfWindow(0);
        if (previous <= 0) {
            return false;
        }
        double delta = Math.abs((double) current - (double) previous) / (double) previous;
        return delta <= tolerance;
    }

    /** Median of the most recent window, or 0 before enough samples exist. */
    public long currentMedian() {
        return hasEnoughSamples() ? medianOfWindow(0) : 0L;
    }

    public long samplesSeen() {
        return written;
    }

    /**
     * @param windowsBack 0 for the most recent window, 1 for the one before it
     */
    private long medianOfWindow(int windowsBack) {
        long end = written - (long) windowsBack * window;
        for (int i = 0; i < window; i++) {
            long index = end - window + i;
            scratch[i] = ring[(int) (index % ring.length)];
        }
        Arrays.sort(scratch);
        int mid = window / 2;
        // Even windows average the two central values, matching the harness's percentile
        // definition so both sides agree on what "median" means.
        return (window % 2 == 0) ? (scratch[mid - 1] + scratch[mid]) / 2 : scratch[mid];
    }
}
