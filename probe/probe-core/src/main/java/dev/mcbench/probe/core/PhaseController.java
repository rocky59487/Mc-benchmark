package dev.mcbench.probe.core;

/**
 * Decides when setup ends, when warmup ends, and when measurement is complete.
 *
 * <p>Implements the warmup rule from {@code docs/METHODOLOGY.md} section 2.
 *
 * <p><b>Setup is a phase.</b> Warmup begins only when the caller declares setup finished, and
 * every warmup window and counter is reset then. Setup sharing the warmup budget meant a
 * scenario whose setup ran long entered <em>measurement</em> with commands still building the
 * world, and identical scenarios got different effective warmup on different machines.
 *
 * <p><b>The gate has three conditions:</b> a minimum duration, a timing plateau, and settled
 * compilation. A series can look flat while tiered compilation is still promoting hot methods.
 *
 * <p>If the gate is never satisfied by a hard ceiling the run is <em>kept</em> but flagged —
 * discarding it would bias the sample toward configurations that converge quickly — and
 * {@link #gateReason()} says which condition failed.
 *
 * <p>Durations are counted in whatever unit the caller advances: seconds for client scenarios,
 * ticks for server ones. The controller is deliberately agnostic — a client run measures
 * wall-clock time because that is what a player experiences, while a server run measures ticks
 * because under tick warp wall-clock time is precisely the thing being compressed.
 */
public final class PhaseController {

    /**
     * Samples between compilation observations.
     *
     * <p>The counter read is cheap but not free and runs on the render or server thread. Once
     * every few dozen samples gives tens of observations across a realistic warmup.
     */
    static final int OBSERVE_EVERY_SAMPLES = 16;

    private final double warmupMinimum;
    private final double warmupCeiling;
    private final double measurementDuration;
    private final SteadyState steadyState;
    private final CompilationMonitor compilation;

    private Phase phase = Phase.PROVISION;
    private double progress;
    private double setupEndedAt = -1;
    private double warmupStartedAt = -1;
    private double warmupEndedAt = -1;
    private boolean converged;
    private int samplesSinceObservation;
    private String gateReason = "";

    /**
     * @param warmupMinimum       minimum warmup, in the caller's unit
     * @param warmupMaxMultiple   ceiling as a multiple of the minimum
     * @param measurementDuration measurement window, in the caller's unit
     * @param steadyStateWindow   samples per steady-state window
     * @param tolerance           steady-state tolerance
     */
    public PhaseController(
            double warmupMinimum,
            double warmupMaxMultiple,
            double measurementDuration,
            int steadyStateWindow,
            double tolerance) {
        this(warmupMinimum, warmupMaxMultiple, measurementDuration, steadyStateWindow,
                tolerance, new CompilationMonitor());
    }

    /** Test seam: inject a compilation monitor. */
    public PhaseController(
            double warmupMinimum,
            double warmupMaxMultiple,
            double measurementDuration,
            int steadyStateWindow,
            double tolerance,
            CompilationMonitor compilation) {
        if (warmupMinimum <= 0) {
            throw new IllegalArgumentException("warmupMinimum must be positive");
        }
        if (warmupMaxMultiple < 1) {
            throw new IllegalArgumentException("warmupMaxMultiple must be at least 1");
        }
        if (measurementDuration <= 0) {
            throw new IllegalArgumentException("measurementDuration must be positive");
        }
        this.warmupMinimum = warmupMinimum;
        this.warmupCeiling = warmupMinimum * warmupMaxMultiple;
        this.measurementDuration = measurementDuration;
        this.steadyState = new SteadyState(steadyStateWindow, tolerance);
        this.compilation = compilation;
    }

    /** Leave provisioning and begin running the scenario's setup commands. */
    public void beginSetup() {
        if (phase != Phase.PROVISION) {
            throw new IllegalStateException("setup already begun; phase is " + phase);
        }
        phase = Phase.SETUP;
        progress = 0;
    }

    /**
     * Setup is finished and settled; begin the timed warmup.
     *
     * <p>Every warmup reference is reset here. Samples taken while setup was placing blocks
     * describe world construction, and setup's uniformity would open the gate immediately.
     */
    public void beginWarmup() {
        if (phase == Phase.PROVISION) {
            // Callers with no setup to run go straight through.
            beginSetup();
        }
        if (phase != Phase.SETUP) {
            throw new IllegalStateException("warmup already begun; phase is " + phase);
        }
        setupEndedAt = progress;
        warmupStartedAt = progress;
        phase = Phase.WARMUP;
        steadyState.reset();
        compilation.reset();
        samplesSinceObservation = 0;
    }

    /**
     * Advance by {@code delta} and feed one timing sample.
     *
     * @return true when the phase changed, so the caller can emit a phase event
     */
    public boolean advance(double delta, long sampleNanos) {
        if (phase == Phase.PROVISION) {
            return false;
        }
        progress += delta;
        if (phase == Phase.SETUP) {
            // Timed only so setup duration can be reported. Nothing here feeds the gate.
            return false;
        }
        steadyState.accept(sampleNanos);

        if (phase == Phase.WARMUP) {
            // Observed here rather than from the caller's flush loop, so the gate does not
            // depend on a cadence the controller cannot see.
            if (++samplesSinceObservation >= OBSERVE_EVERY_SAMPLES) {
                samplesSinceObservation = 0;
                compilation.observe();
            }
            boolean minimumElapsed = warmupProgress() >= warmupMinimum;
            if (minimumElapsed && steadyState.isSteady() && compilation.settled()) {
                converged = true;
                gateReason = compilation.available()
                        ? "timing plateau and compilation settled"
                        : "timing plateau (compilation time unavailable on this JVM)";
                return enterMeasurement();
            }
            if (warmupProgress() >= warmupCeiling) {
                // Kept, but the caller must flag it. Silently accepting an unconverged run
                // credits the mod with the JIT's warmup cost.
                converged = false;
                gateReason = describeCeiling();
                return enterMeasurement();
            }
        }
        return false;
    }

    private String describeCeiling() {
        boolean timing = steadyState.isSteady();
        boolean compiler = compilation.settled();
        if (!timing && !compiler) {
            return "ceiling reached: neither the timing series nor compilation had settled";
        }
        if (!timing) {
            return "ceiling reached: the timing series had not plateaued";
        }
        if (!compiler) {
            return "ceiling reached: compilation was still active";
        }
        return "ceiling reached before the minimum warmup had elapsed";
    }

    private boolean enterMeasurement() {
        warmupEndedAt = progress;
        phase = Phase.MEASUREMENT;
        return true;
    }

    /** Whether the measurement window has elapsed. */
    public boolean isComplete() {
        return phase == Phase.MEASUREMENT && measurementProgress() >= measurementDuration;
    }

    public double measurementProgress() {
        return warmupEndedAt < 0 ? 0 : progress - warmupEndedAt;
    }

    /** How long setup took, in the caller's unit. */
    public double setupDuration() {
        return setupEndedAt < 0 ? progress : setupEndedAt;
    }

    /** How long warmup took, in the caller's unit. */
    public double warmupDuration() {
        if (warmupStartedAt < 0) {
            return 0;
        }
        return (warmupEndedAt < 0 ? progress : warmupEndedAt) - warmupStartedAt;
    }

    private double warmupProgress() {
        return warmupStartedAt < 0 ? 0 : progress - warmupStartedAt;
    }

    public Phase phase() {
        return phase;
    }

    public boolean isMeasuring() {
        return phase == Phase.MEASUREMENT;
    }

    public boolean isWarmingUp() {
        return phase == Phase.WARMUP;
    }

    /**
     * Whether warmup ended because the gate opened rather than because it timed out.
     * False means the run carries {@code warmup_not_converged}.
     */
    public boolean converged() {
        return converged;
    }

    /**
     * Why warmup ended, in words.
     *
     * <p>"converged: false" does not say what to change: a run that timed out on compilation
     * wants a longer ceiling, one that never plateaued wants a quieter machine.
     */
    public String gateReason() {
        return gateReason;
    }

    /** Whether this JVM could report compilation time, for the record. */
    public boolean compilationGateAvailable() {
        return compilation.available();
    }

    public double totalProgress() {
        return progress;
    }
}
