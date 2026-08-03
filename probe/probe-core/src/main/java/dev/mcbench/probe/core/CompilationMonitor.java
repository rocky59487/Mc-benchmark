package dev.mcbench.probe.core;

import java.lang.management.CompilationMXBean;
import java.lang.management.ManagementFactory;

/**
 * The JIT half of the warmup gate.
 *
 * <p>{@code docs/METHODOLOGY.md} section 2 ends warmup when the timing series has plateaued
 * <em>and</em> compilation has settled. A series can look flat because the workload is
 * momentarily uniform while the compiler is still promoting hot methods; measurement started
 * there charges the mod for the compiler's remaining work.
 *
 * <p>{@link CompilationMXBean#getTotalCompilationTime()} is the portable signal. A plateau is
 * consecutive observations whose growth stays under a small threshold — not zero, since
 * background recompilation never entirely stops in a running game.
 *
 * <p>Where the bean is absent the gate opens rather than blocking forever, and the caller
 * records that the half was unavailable rather than claiming it passed.
 */
public final class CompilationMonitor {

    /**
     * Compilation growth per observation, in milliseconds, below which the compiler counts as
     * settled.
     *
     * <p>Non-zero: a steady-state server still compiles a few milliseconds' worth per second,
     * so a zero threshold would hold every run to its ceiling.
     */
    public static final double DEFAULT_TOLERANCE_MS = 10.0;

    /** Consecutive quiet observations required. */
    public static final int DEFAULT_PLATEAU_OBSERVATIONS = 3;

    /** Reads total compilation milliseconds, or -1 when unavailable. */
    @FunctionalInterface
    public interface Source {
        long totalCompilationMs();
    }

    private final Source source;
    private final boolean available;
    private final double toleranceMs;
    private final int required;

    private long lastTotalMs = -1;
    private int quietObservations;

    public CompilationMonitor() {
        this(DEFAULT_TOLERANCE_MS, DEFAULT_PLATEAU_OBSERVATIONS);
    }

    public CompilationMonitor(double toleranceMs, int required) {
        this(platformSource(), toleranceMs, required);
    }

    /** Test seam: drive the gate from a supplied counter. */
    public CompilationMonitor(Source source, double toleranceMs, int required) {
        this.source = source;
        this.available = source != null && source.totalCompilationMs() >= 0;
        this.toleranceMs = toleranceMs;
        this.required = Math.max(1, required);
    }

    /**
     * A monitor that never blocks the gate.
     *
     * <p>For tests exercising the timing half alone. {@link #available()} stays false, so a
     * run does not record a gate it never applied.
     */
    public static CompilationMonitor alwaysSettled() {
        return new CompilationMonitor(null, DEFAULT_TOLERANCE_MS, 1);
    }

    private static Source platformSource() {
        CompilationMXBean candidate = null;
        try {
            CompilationMXBean found = ManagementFactory.getCompilationMXBean();
            if (found != null && found.isCompilationTimeMonitoringSupported()) {
                candidate = found;
            }
        } catch (RuntimeException ignored) {
            // No compiler bean; the gate degrades to the timing test alone.
        }
        if (candidate == null) {
            return null;
        }
        CompilationMXBean bean = candidate;
        return () -> {
            try {
                return bean.getTotalCompilationTime();
            } catch (RuntimeException e) {
                return -1;
            }
        };
    }

    /** Whether this JVM can report compilation time at all. */
    public boolean available() {
        return available;
    }

    /** Total milliseconds spent compiling, or -1 when unavailable. */
    public long totalCompilationMs() {
        return source == null ? -1 : source.totalCompilationMs();
    }

    /**
     * Take one observation. Called on the sampling cadence, not per frame — the bean is a
     * cheap read but not free, and compilation does not change meaningfully within a frame.
     */
    public void observe() {
        long total = totalCompilationMs();
        if (total < 0) {
            return;
        }
        if (lastTotalMs >= 0 && total - lastTotalMs <= toleranceMs) {
            quietObservations++;
        } else {
            quietObservations = 0;
        }
        lastTotalMs = total;
    }

    /** Reset the plateau counter, so a new phase does not inherit an old verdict. */
    public void reset() {
        lastTotalMs = totalCompilationMs();
        quietObservations = 0;
    }

    /**
     * Whether compilation has settled.
     *
     * <p>True when the bean is unavailable, since a gate that can never open would hold every
     * run to its ceiling. The caller records the reason warmup ended.
     */
    public boolean settled() {
        return !available || quietObservations >= required;
    }
}
