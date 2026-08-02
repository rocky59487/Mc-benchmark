package dev.mcbench.probe.core;

/**
 * Decides when warmup ends and when measurement is complete.
 *
 * <p>Implements the warmup rule from {@code docs/METHODOLOGY.md} section 2. Warmup ends when
 * <em>both</em> a minimum duration has elapsed <em>and</em> the timing series has plateaued.
 * Requiring both matters: the minimum stops a briefly-flat early series from ending warmup
 * prematurely, and the steady-state test stops a fixed duration from cutting off a
 * slow-starting configuration while it is still compiling.
 *
 * <p>If steady state is never reached by a hard ceiling, the run is <em>kept</em> but flagged.
 * Discarding it would quietly bias the sample toward configurations that happen to converge
 * quickly; accepting it silently would charge the mod for the JIT's warmup. Reporting it is the
 * only honest option.
 *
 * <p>Durations are counted in whatever unit the caller advances: seconds for client scenarios,
 * ticks for server ones. The controller is deliberately agnostic — a client run measures
 * wall-clock time because that is what a player experiences, while a server run measures ticks
 * because under tick warp wall-clock time is precisely the thing being compressed.
 */
public final class PhaseController {

    private final double warmupMinimum;
    private final double warmupCeiling;
    private final double measurementDuration;
    private final SteadyState steadyState;

    private Phase phase = Phase.PROVISION;
    private double progress;
    private double warmupEndedAt = -1;
    private boolean converged;

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
    }

    /** Leave provisioning and begin the timed warmup. */
    public void beginWarmup() {
        if (phase != Phase.PROVISION) {
            throw new IllegalStateException("warmup already begun; phase is " + phase);
        }
        phase = Phase.WARMUP;
        progress = 0;
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
        steadyState.accept(sampleNanos);

        if (phase == Phase.WARMUP) {
            boolean minimumElapsed = progress >= warmupMinimum;
            if (minimumElapsed && steadyState.isSteady()) {
                converged = true;
                return enterMeasurement();
            }
            if (progress >= warmupCeiling) {
                // Kept, but the caller must flag it. Silently accepting an unconverged run
                // credits the mod with the JIT's warmup cost.
                converged = false;
                return enterMeasurement();
            }
        }
        return false;
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

    public Phase phase() {
        return phase;
    }

    public boolean isMeasuring() {
        return phase == Phase.MEASUREMENT;
    }

    /**
     * Whether warmup ended because the series settled rather than because it timed out.
     * False means the run carries {@code warmup_not_converged}.
     */
    public boolean converged() {
        return converged;
    }

    public double totalProgress() {
        return progress;
    }
}
