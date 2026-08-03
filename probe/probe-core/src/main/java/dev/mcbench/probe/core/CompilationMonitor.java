package dev.mcbench.probe.core;

import java.lang.management.CompilationMXBean;
import java.lang.management.ManagementFactory;

/**
 * The JIT half of the warmup gate.
 *
 * <p>{@code docs/METHODOLOGY.md} section 2 states that warmup ends when the timing series has
 * plateaued <em>and</em> compilation has settled. Only the first half was implemented: the
 * probe compared rolling medians of tick or frame time and never consulted the compiler at all,
 * so the stated JIT-stability guarantee was not enforced anywhere. The failure it was meant to
 * prevent is a series that looks flat because the workload is momentarily uniform while the
 * compiler is still promoting hot methods — measurement then starts, tiered compilation
 * finishes a few seconds in, and the mod is charged for the difference.
 *
 * <p>{@link CompilationMXBean#getTotalCompilationTime()} is the portable signal: monotonic
 * milliseconds spent compiling, present on every JVM that has a JIT. A plateau is defined as
 * consecutive observations whose growth stays under a small threshold — not zero growth, since
 * background recompilation never entirely stops in a running game and demanding zero would mean
 * warmup never converges.
 *
 * <p>Where the bean is absent — an interpreted-only or AOT-only JVM — the gate opens rather
 * than blocking forever, and the caller records that the compilation half was unavailable
 * rather than claiming it passed.
 */
public final class CompilationMonitor {

    /**
     * Compilation growth per observation, in milliseconds, below which the compiler counts as
     * settled.
     *
     * <p>Deliberately non-zero. A steady-state Minecraft server still compiles a few
     * milliseconds' worth per second as new code paths are reached, so a zero threshold would
     * hold warmup open until the ceiling on every run and flag every run unconverged — turning
     * a real gate into a permanent false alarm, which is worse than no gate.
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
     * <p>For tests exercising the timing half alone, and for callers that have
     * deliberately opted out. Distinguishable from a real one through
     * {@link #available()}, so a run that used it does not record a compilation
     * gate it never applied.
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
     * <p>True when the bean is unavailable: a gate that can never open would hold every run at
     * its warmup ceiling on such a JVM. The caller records the reason warmup ended, so an
     * unavailable half is visible rather than passed off as a satisfied one.
     */
    public boolean settled() {
        return !available || quietObservations >= required;
    }
}
