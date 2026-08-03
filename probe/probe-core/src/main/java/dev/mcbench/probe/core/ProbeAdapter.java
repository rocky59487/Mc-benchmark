package dev.mcbench.probe.core;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Base class for platform adapters. Everything a new loader needs to implement lives here.
 *
 * <p>The design goal is that supporting a platform costs a few dozen lines. All methodology —
 * phase transitions, steady-state detection, buffering, protocol emission — is in probe-core and
 * shared. An adapter supplies three things:
 *
 * <ol>
 *   <li>a timing hook, calling {@link #onFrame()} or {@link #onTick()} once per frame or tick;
 *   <li>a way to execute a command, via {@link #executeCommand(String)};
 *   <li>a way to stop the game when the run finishes, via {@link #requestShutdown()}.
 * </ol>
 *
 * <p>That small surface is deliberate and is what makes broad version support tractable. The
 * dominant cost of supporting many Minecraft versions is not writing code, it is that game APIs
 * move between versions; the fewer of them an adapter touches, the more versions one
 * implementation covers unchanged. See {@code docs/PLATFORMS.md}.
 *
 * <p><b>Frames are timed between hook calls; ticks are bracketed.</b> The interval between
 * buffer swaps <em>is</em> the frame time, since the renderer presents as fast as it can. A
 * server tick is the opposite: the loop sleeps out the rest of the 50 ms budget, so the
 * interval between end-of-tick callbacks tends to 50 ms whether the work took 5 ms or 30 ms.
 * Adapters call {@link #onTickStart()} and {@link #onTickEnd()}, or report a platform duration
 * through {@link #recordPlatformTick(long)}; {@link #onTickPeriod()} is the last resort and its
 * samples are published under a different metric name.
 */
public abstract class ProbeAdapter {

    /**
     * Setup commands issued per tick.
     *
     * <p>Large scenarios compile to tens of thousands of setup commands. Running them all in
     * one tick trips Minecraft's overload watchdog; spreading them over ticks is free, since
     * setup is untimed by construction.
     */
    public static final int MAX_SETUP_COMMANDS_PER_PUMP = 200;

    /**
     * Ticks to wait between the last setup command and the start of warmup.
     *
     * <p>A second at 20 TPS. Setup leaves chunk saves, lighting and block entities in flight,
     * so warmup starting on the same tick as the last {@code /fill} would spend its opening
     * seconds measuring the tail of world construction. A quiescence signal would need a
     * per-platform API for something a fixed settle handles identically everywhere.
     */
    public static final int SETTLE_TICKS = 20;

    protected final ProbeSession session;
    private final AtomicBoolean shuttingDown = new AtomicBoolean();
    private long lastFrameNanos;
    private long lastTickNanos;
    private long tickStartNanos;
    private boolean setupIssued;
    private int settleTicks;

    protected ProbeAdapter(ProbeSession session) {
        this.session = session;
    }

    /** Human-readable platform name, recorded in the stream's metadata. */
    public abstract String platformName();

    /**
     * Execute one command in the game.
     *
     * <p>Called only from {@link #pump()}, which the adapter invokes on a thread where issuing
     * commands is safe. Running one from the wrong thread is a classic way to corrupt world
     * state in the middle of a measurement.
     *
     * @return whether the game accepted and ran it. A command can be rejected without throwing
     *     — a syntax error, an unknown selector, a missing permission — so catching exceptions
     *     is not enough.
     */
    protected abstract boolean executeCommand(String command);

    /** Ask the game to exit; the run is finished. */
    protected abstract void requestShutdown();

    /**
     * Start the session. Call once the game is far enough along to accept commands.
     */
    public void begin(Map<String, String> extraMetadata) {
        Map<String, String> metadata = new LinkedHashMap<>();
        metadata.put("platform", platformName());
        metadata.put("scenario", session.config().scenarioId());
        metadata.put("scenario_version", session.config().scenarioVersion());
        metadata.put("scenario_hash", session.config().contentHash());
        metadata.put("java", System.getProperty("java.version", "unknown"));
        metadata.put("jvm", System.getProperty("java.vm.name", "unknown"));
        if (extraMetadata != null) {
            metadata.putAll(extraMetadata);
        }
        session.start(metadata);
    }

    /**
     * Call once per rendered frame, from the render thread.
     *
     * <p>The first call only establishes a reference point: there is no previous frame to
     * measure against, and inventing one would put a meaningless sample at the head of the
     * distribution.
     */
    public final void onFrame() {
        long now = System.nanoTime();
        if (lastFrameNanos != 0) {
            session.recordFrame(now - lastFrameNanos);
        }
        lastFrameNanos = now;
    }

    /**
     * Call at the start of a server tick, before the server does any of its work.
     *
     * <p>Paired with {@link #onTickEnd()} to bracket the tick, which is what MSPT means.
     */
    public final void onTickStart() {
        tickStartNanos = System.nanoTime();
    }

    /** Call at the end of a server tick. Records nothing without a matching start. */
    public final void onTickEnd() {
        long start = tickStartNanos;
        if (start == 0) {
            // No paired start: the adapter registered only an end hook, or the run began
            // mid-tick. Recording the interval would substitute the period for the duration.
            return;
        }
        tickStartNanos = 0;
        session.recordTick(System.nanoTime() - start, TickSource.BRACKET);
    }

    /**
     * Record a tick duration a platform API measured itself.
     *
     * <p>Better than bracketing where available: the platform's boundaries include work
     * outside any event an adapter can hook.
     */
    public final void recordPlatformTick(long durationNanos) {
        session.recordTick(durationNanos, TickSource.PLATFORM);
    }

    /**
     * Call once per server tick when the platform can offer neither a bracket nor a duration.
     *
     * <p>The interval between consecutive calls, which on an unsaturated server converges on
     * 50 ms regardless of the work done. Published as {@code tick_period_*} with the run
     * flagged {@code tick_period_only}.
     */
    public final void onTickPeriod() {
        long now = System.nanoTime();
        if (lastTickNanos != 0) {
            session.recordTick(now - lastTickNanos, TickSource.PERIOD);
        }
        lastTickNanos = now;
    }

    /**
     * Drive workload commands and finish the run when the window closes.
     *
     * <p>Call from a thread where {@link #executeCommand} is safe — typically the end of the
     * client or server tick. Kept separate from the timing hooks so that command execution
     * never lands inside the interval being measured.
     */
    public final void pump() {
        if (shuttingDown.get()) {
            return;
        }
        if (!setupIssued) {
            // Setup runs untimed in its own phase: timing it would measure world
            // construction rather than the mod.
            //
            // Batched rather than drained in one go. A scenario that places hundreds of
            // structures compiles to five figures of commands, and running all of them inside
            // a single tick stalls the server long enough to trip Minecraft's own overload
            // watchdog — which would kill the run before it ever reached warmup. Spreading the
            // work over several ticks costs nothing, because none of it is timed.
            for (int issued = 0; issued < MAX_SETUP_COMMANDS_PER_PUMP; issued++) {
                String command = session.pollCommand();
                if (command == null) {
                    break;
                }
                safely(command);
            }
            if (session.setupComplete()) {
                setupIssued = true;
                settleTicks = SETTLE_TICKS;
            }
            return;
        }

        if (settleTicks > 0) {
            // Let the world quiet down: setup leaves chunk saves, lighting and block-entity
            // initialisation in flight.
            settleTicks--;
            if (settleTicks == 0) {
                session.setupFinished();
            }
            return;
        }

        String command = session.pollCommand();
        if (command != null) {
            safely(command);
        }

        if (session.isComplete() && shuttingDown.compareAndSet(false, true)) {
            finish();
        }
    }

    private void safely(String command) {
        try {
            boolean ok = executeCommand(command);
            // Reported either way: a rejected command raises nothing.
            session.commandCompleted(command, ok, ok ? "" : "rejected by the game");
        } catch (RuntimeException e) {
            // Must not take down the game mid-run — the samples so far are worth keeping —
            // but the run is no longer measuring the scenario asked for.
            session.commandCompleted(command, false, e.toString());
        }
    }

    /**
     * Close the session and stop the game. Safe to call more than once.
     *
     * <p>Adapters should also register this as a shutdown hook. The harness treats a missing
     * {@code bye} event as a crash, so an unclean exit that skipped this would misreport a
     * perfectly good run as failed.
     */
    public final void finish() {
        shuttingDown.set(true);
        try {
            session.close();
        } finally {
            requestShutdown();
        }
    }

    public final boolean isFinished() {
        return shuttingDown.get();
    }

    /** True when this adapter should be sampling frames for the configured scenario. */
    public final boolean measuresFrames() {
        return session.config().side().measuresFrames();
    }

    /** True when this adapter should be sampling ticks for the configured scenario. */
    public final boolean measuresTicks() {
        return session.config().side().measuresTicks();
    }
}
