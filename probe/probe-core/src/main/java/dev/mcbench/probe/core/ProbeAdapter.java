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
 * <p>Timing is measured between consecutive hook calls rather than by wrapping the frame. A
 * wrapper would have to bracket rendering from inside, which is both more invasive and more
 * version-fragile; the interval between successive swap-buffer calls is the same quantity and
 * needs only a single hook point.
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

    protected final ProbeSession session;
    private final AtomicBoolean shuttingDown = new AtomicBoolean();
    private long lastFrameNanos;
    private long lastTickNanos;
    private boolean setupIssued;

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
     */
    protected abstract void executeCommand(String command);

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

    /** Call once per server tick, from the server thread. */
    public final void onTick() {
        long now = System.nanoTime();
        if (lastTickNanos != 0) {
            session.recordTick(now - lastTickNanos);
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
            // Setup runs untimed, before warmup: it builds the world the scenario describes,
            // and timing that work would measure world construction rather than the mod.
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
            executeCommand(command);
        } catch (RuntimeException e) {
            // A failed command is a scenario defect worth reporting, but it must not take down
            // the game mid-run — the samples already collected are still worth keeping.
            session.reportError("command failed: " + command + ": " + e);
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
