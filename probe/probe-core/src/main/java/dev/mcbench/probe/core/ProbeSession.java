package dev.mcbench.probe.core;

import java.io.IOException;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * The probe's orchestrator, and the entire API a platform adapter needs.
 *
 * <p>Every adapter — Fabric, NeoForge, Forge, Paper, or the version-agnostic JVM agent — does
 * the same three things: call {@link #recordFrame(long)} or {@link #recordTick(long)} on the
 * hot path, drain {@link #pollCommand()} on the game thread, and call {@link #close()} at the
 * end. All phase logic, steady-state detection, and protocol emission live here, so adding a
 * platform means writing a few dozen lines rather than reimplementing the methodology.
 *
 * <p>Threading: {@code recordFrame} and {@code recordTick} are safe to call from the render or
 * server thread and never block on I/O. A background flusher drains the buffers on a cadence
 * and does all writing. That separation is the reason the probe can time a frame without
 * becoming part of it.
 */
public final class ProbeSession implements AutoCloseable {

    public static final String PROBE_VERSION = "0.1.0";

    /** Environment variables the harness sets when launching an instance. */
    public static final String ENV_CONFIG = "MCBENCH_PROBE_CONFIG";
    public static final String ENV_OUTPUT = "MCBENCH_PROBE_OUTPUT";

    private static final int BUFFER_CAPACITY = 1 << 16;
    private static final long FLUSH_INTERVAL_NANOS = 500_000_000L;

    private final ProbeConfig config;
    private final ProbeWriter writer;
    private final SampleBuffer frames = new SampleBuffer(BUFFER_CAPACITY);
    private final SampleBuffer ticks = new SampleBuffer(BUFFER_CAPACITY);
    private final RuntimeMonitor runtime = new RuntimeMonitor();
    private final PhaseController phases;
    private final CommandQueue commands;

    private final AtomicBoolean started = new AtomicBoolean();
    private final AtomicBoolean finished = new AtomicBoolean();
    private final AtomicLong chunksGenerated = new AtomicLong();
    private final AtomicLong chunksLoaded = new AtomicLong();

    private volatile Phase lastEmittedPhase = Phase.PROVISION;
    private volatile long measurementStartNanos;
    private volatile long lastFlushNanos;
    private volatile boolean warmupFlagEmitted;

    public ProbeSession(ProbeConfig config, ProbeWriter writer) {
        this.config = config;
        this.writer = writer;
        this.phases = new PhaseController(
                config.warmupMinimum(),
                config.warmupMaxMultiple(),
                config.measurementDuration(),
                config.steadyStateWindow(),
                config.steadyStateTolerance());
        this.commands = new CommandQueue(config.setupCommands(), config.workloadCommands());
    }

    /**
     * Open a session from the environment the harness provides, or {@code null} when this JVM
     * was not launched by mcbench.
     *
     * <p>Returning null rather than throwing is deliberate: the probe mod may be present in a
     * normal play session, and it must then do nothing at all.
     */
    public static ProbeSession fromEnvironment() {
        String configPath = System.getenv(ENV_CONFIG);
        if (configPath == null || configPath.isBlank()) {
            return null;
        }
        try {
            ProbeConfig config = ProbeConfig.load(Path.of(configPath));
            String override = System.getenv(ENV_OUTPUT);
            Path output = (override == null || override.isBlank())
                    ? config.output()
                    : Path.of(override);
            return new ProbeSession(config, new ProbeWriter(output));
        } catch (IOException | RuntimeException e) {
            // The probe must never prevent the game from starting. A session that cannot be
            // opened simply does not measure, and the harness notices the missing stream.
            return null;
        }
    }

    /** Emit the hello event and begin the timed warmup. Idempotent. */
    public void start(Map<String, String> metadata) {
        if (!started.compareAndSet(false, true)) {
            return;
        }
        writer.hello(PROBE_VERSION, metadata);
        runtime.resetBaseline();
        phases.beginWarmup();
        writer.phase(Phase.WARMUP);
        lastEmittedPhase = Phase.WARMUP;
        lastFlushNanos = System.nanoTime();
    }

    // -- hot path --------------------------------------------------------

    /**
     * Record one rendered frame. Called from the render thread; allocation-free.
     *
     * @param durationNanos time for this frame
     */
    public void recordFrame(long durationNanos) {
        if (durationNanos <= 0 || !started.get() || finished.get()) {
            return;
        }
        // Client scenarios advance in seconds, because wall-clock time is what a player
        // experiences.
        advance(durationNanos / 1_000_000_000.0, durationNanos);
        // Recorded during warmup as well as measurement. The harness separates them by phase
        // and discards warmup from the statistics, but it can only *verify* that warmup
        // actually converged if it can see the warmup series. Emitting only measurement
        // samples would make the convergence claim untestable, and an unverifiable warmup is
        // how the JIT's cost silently gets charged to a mod.
        frames.record(durationNanos);
        maybeFlush();
    }

    /**
     * Record one server tick. Called from the server thread; allocation-free.
     *
     * @param durationNanos time for this tick
     */
    public void recordTick(long durationNanos) {
        if (durationNanos <= 0 || !started.get() || finished.get()) {
            return;
        }
        // Server scenarios advance in ticks, not seconds: under tick warp wall-clock time is
        // exactly the quantity being compressed, so counting it would make the measurement
        // window depend on how fast the machine is.
        advance(1.0, durationNanos);
        // Recorded during warmup too; see recordFrame for why.
        ticks.record(durationNanos);
        maybeFlush();
    }

    private void advance(double delta, long sampleNanos) {
        boolean changed = phases.advance(delta, sampleNanos);
        if (changed && phases.isMeasuring()) {
            onMeasurementBegan();
        }
    }

    private synchronized void onMeasurementBegan() {
        if (lastEmittedPhase == Phase.MEASUREMENT) {
            return;
        }
        flushBuffers();
        if (!phases.converged() && !warmupFlagEmitted) {
            // Warmup hit its ceiling without settling. The run is kept — discarding it would
            // bias the sample toward configurations that happen to converge quickly — but it
            // must be flagged, because otherwise the mod is charged for the JIT's work.
            writer.flag("warmup_not_converged");
            warmupFlagEmitted = true;
        }
        // Reset after warmup so GC and allocation describe the measurement window only.
        runtime.resetBaseline();
        writer.phase(Phase.MEASUREMENT);
        lastEmittedPhase = Phase.MEASUREMENT;
        measurementStartNanos = System.nanoTime();
        commands.enterWorkload();
    }

    private void maybeFlush() {
        long now = System.nanoTime();
        if (now - lastFlushNanos < FLUSH_INTERVAL_NANOS) {
            return;
        }
        lastFlushNanos = now;
        flushBuffers();
        sampleRuntime();
    }

    private synchronized void flushBuffers() {
        SampleBuffer.Drained drainedFrames = frames.drain();
        if (!drainedFrames.isEmpty()) {
            writer.frames(drainedFrames.values(), drainedFrames.count());
        }
        SampleBuffer.Drained drainedTicks = ticks.drain();
        if (!drainedTicks.isEmpty()) {
            writer.ticks(drainedTicks.values(), drainedTicks.count());
        }
    }

    private void sampleRuntime() {
        RuntimeMonitor.Sample sample = runtime.sample();
        writer.memory(sample.heapMb(), sample.collected(), sample.allocatedBytes());
        if (sample.pauseMs() > 0) {
            writer.gcPauses(new double[] {sample.pauseMs()}, 1);
        }
    }

    // -- workload driving ------------------------------------------------

    /**
     * Next command for the adapter to execute on the game thread, or {@code null}.
     *
     * <p>The adapter polls rather than the session pushing, because only the adapter knows
     * which thread is safe to run a command on — and running one from the wrong thread is a
     * classic way to corrupt world state mid-measurement.
     */
    public String pollCommand() {
        return commands.poll();
    }

    public boolean setupComplete() {
        return commands.setupComplete();
    }

    // -- counters and state ----------------------------------------------

    public void addChunksGenerated(long count) {
        chunksGenerated.addAndGet(count);
    }

    public void addChunksLoaded(long count) {
        chunksLoaded.addAndGet(count);
    }

    public void recordFingerprint(String sha256) {
        writer.fingerprint(sha256);
    }

    public void reportError(String message) {
        writer.error(message);
    }

    public boolean isComplete() {
        return phases.isComplete();
    }

    public Phase phase() {
        return phases.phase();
    }

    public ProbeConfig config() {
        return config;
    }

    public boolean overflowed() {
        return frames.hasOverflowed() || ticks.hasOverflowed();
    }

    /**
     * Finish the run: flush everything, emit {@code bye}, close the stream. Idempotent.
     *
     * <p>The harness treats a missing {@code bye} as a crash, so this must run even on an
     * unclean shutdown — adapters register it as a shutdown hook as well as calling it.
     */
    @Override
    public void close() {
        if (!finished.compareAndSet(false, true)) {
            return;
        }
        try {
            synchronized (this) {
                flushBuffers();
                if (started.get()) {
                    sampleRuntime();
                    if (chunksGenerated.get() > 0 || chunksLoaded.get() > 0) {
                        writer.chunks(chunksGenerated.get(), chunksLoaded.get());
                    }
                    if (overflowed()) {
                        // Dropped samples would bias the distribution toward whatever was
                        // happening while the buffer had room. Say so rather than publish it.
                        writer.error("sample buffer overflowed; some samples were dropped");
                    }
                    double elapsed = measurementStartNanos == 0
                            ? 0
                            : (System.nanoTime() - measurementStartNanos) / 1_000_000_000.0;
                    writer.bye(elapsed, config.saturated());
                }
            }
        } finally {
            writer.close();
        }
    }
}
