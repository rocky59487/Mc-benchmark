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
 * <p><b>Frames are timed between hook calls; ticks are not.</b> For a frame the interval
 * between successive buffer swaps <em>is</em> the frame time — the renderer is presenting as
 * fast as it can, so there is no idle to include. A server tick is the opposite case: the loop
 * sleeps out whatever remains of the 50 ms budget, so the interval between end-of-tick
 * callbacks tends to 50 ms whether the work took 5 ms or 30 ms. Measuring ticks that way made
 * {@code mspt_mean}, the percentiles and {@code tick_headroom} all measurements of the
 * scheduler. Adapters therefore call {@link #onTickStart()} and {@link #onTickEnd()} around the
 * tick, or report a platform-supplied duration through {@link #recordPlatformTick(long)}; where
 * neither is possible {@link #onTickPeriod()} is available and its samples are published under
 * a different metric name.
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
     * <p>A second at 20 TPS. Setup leaves work in flight — chunk saves, lighting propagation,
     * block entities initialising — and warmup that began on the same tick as the last
     * {@code /fill} would spend its opening seconds measuring the tail of world construction.
     * The alternative, waiting for a quiescence signal, would need a per-platform API for
     * something a fixed settle handles adequately and identically everywhere.
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
     * @return whether the game accepted and ran the command. A command can be rejected without
     *     throwing anything — a syntax error, an unknown selector, a missing permission — so
     *     catching exceptions is not enough. The previous signature returned nothing, and a
     *     scenario built on a mistyped command therefore ran to completion and measured an
     *     empty world as though setup had succeeded.
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
     * <p>Paired with {@link #onTickEnd()}. Together they bracket the tick, which is what MSPT
     * means; the interval between end-of-tick callbacks is the tick period and is a different
     * number entirely on any server that is not saturated.
     */
    public final void onTickStart() {
        tickStartNanos = System.nanoTime();
    }

    /** Call at the end of a server tick. Records nothing without a matching start. */
    public final void onTickEnd() {
        long start = tickStartNanos;
        if (start == 0) {
            // No paired start — the adapter registered only an end hook, or the run began
            // mid-tick. Recording the interval here would silently substitute the period for
            // the execution time, which is the whole error being corrected.
            return;
        }
        tickStartNanos = 0;
        session.recordTick(System.nanoTime() - start, TickSource.BRACKET);
    }

    /**
     * Record a tick duration a platform API measured itself.
     *
     * <p>For platforms that expose the figure directly — Paper's tick times, for instance —
     * which is better than bracketing, since the platform's own boundaries include work that
     * happens outside any event an adapter can hook.
     */
    public final void recordPlatformTick(long durationNanos) {
        session.recordTick(durationNanos, TickSource.PLATFORM);
    }

    /**
     * Call once per server tick when the platform can offer neither a bracket nor a duration.
     *
     * <p>Records the interval between consecutive calls. This is the tick <em>period</em>: on
     * an unsaturated server it converges on 50 ms regardless of how much work the tick did. The
     * samples are published as {@code tick_period_*} rather than as MSPT, and the run is
     * flagged {@code tick_period_only} so nothing downstream can mistake one for the other.
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
            // Setup runs untimed, in its own phase before warmup: it builds the world the
            // scenario describes, and timing that work would measure world construction rather
            // than the mod.
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
            // Let the world quiet down before warmup starts measuring it. Setup leaves chunk
            // saves, lighting updates and block-entity initialisation in flight, and beginning
            // warmup on top of that hands the first seconds of the timing series a workload no
            // variant will see again.
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
            // Reported either way. A command the dispatcher rejected raises nothing, so
            // catching exceptions alone let a mistyped scenario run to completion having built
            // none of the world it describes.
            session.commandCompleted(command, ok, ok ? "" : "rejected by the game");
        } catch (RuntimeException e) {
            // A failed command must not take down the game mid-run — the samples already
            // collected are still worth keeping — but the run is no longer measuring the
            // scenario that was asked for, and the session marks it accordingly.
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
