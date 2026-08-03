package dev.mcbench.probe.agent;

import dev.mcbench.probe.core.ProbeConfig;
import dev.mcbench.probe.core.ProbeSession;
import dev.mcbench.probe.core.ProbeWriter;
import java.io.IOException;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * The per-frame hook the instrumented swap-buffers call lands in.
 *
 * <p>Everything about this class is shaped by one fact: it runs on the render thread, once per
 * presented frame, in a process whose frame timing we are trying to measure. It must therefore
 * be as close to free as a Java method gets — no allocation, no locks it can contend on, no I/O.
 * {@link ProbeSession#recordFrame} is already built for that; this adds only a timestamp and a
 * subtraction.
 *
 * <p>It is also referenced by name from injected bytecode, so its fully-qualified name is part
 * of the agent's contract and must not move. It is deliberately not relocated by the shadow
 * configuration for that reason.
 *
 * <p><b>Failure policy.</b> Nothing here may throw into the game. An exception escaping this
 * method would propagate out of {@code glfwSwapBuffers} and crash the client — a benchmarking
 * tool that crashes the thing it measures is worse than useless. Every path is guarded, and a
 * broken session simply stops recording.
 */
public final class FrameHook {

    private FrameHook() {}

    private static final AtomicBoolean STARTED = new AtomicBoolean();
    private static volatile ProbeSession session;
    private static volatile boolean disabled;
    private static long lastFrameNanos;
    private static long framesSeen;

    /**
     * Called once per presented frame from instrumented LWJGL.
     *
     * <p>Must stay static and void with no arguments: the injected instruction is a bare
     * {@code INVOKESTATIC}, chosen so the transformer never has to reason about the argument
     * types of whichever swap-buffers overload it found.
     */
    public static void onFrame() {
        if (disabled) {
            return;
        }
        try {
            ProbeSession current = session;
            if (current == null) {
                return;
            }
            long now = System.nanoTime();
            long previous = lastFrameNanos;
            lastFrameNanos = now;
            framesSeen++;
            if (previous != 0) {
                // The first call only establishes a reference point; there is no previous
                // frame to measure against and inventing one would put a meaningless sample
                // at the head of the distribution.
                current.recordFrame(now - previous);
            }
            if (current.isComplete()) {
                finish();
            }
        } catch (Throwable t) {
            // Never propagate into the render loop. A tool that crashes the client it is
            // measuring has failed at something more basic than accuracy.
            disabled = true;
        }
    }

    /**
     * Open the session. Called from {@code premain}; safe to call more than once.
     *
     * @return true when a session was opened and instrumentation is worth installing
     */
    public static boolean start(Map<String, String> metadata) {
        if (!STARTED.compareAndSet(false, true)) {
            return session != null;
        }
        try {
            String configPath = System.getenv(ProbeSession.ENV_CONFIG);
            if (configPath == null || configPath.isBlank()) {
                // Not launched by mcbench. The agent stays completely inert, so it is
                // harmless to leave on a normal launch command.
                return false;
            }

            ProbeConfig config = ProbeConfig.load(Path.of(configPath));
            if (!config.side().measuresFrames()) {
                // A server-side scenario has no frames; instrumenting would be pointless
                // work in the one place we most want to add none.
                return false;
            }

            Path output = agentOutput(config);
            ProbeSession opened = new ProbeSession(config, new ProbeWriter(output));

            Map<String, String> full = new LinkedHashMap<>();
            full.put("platform", "jvm-agent");
            full.put("scenario", config.scenarioId());
            full.put("scenario_hash", config.contentHash());
            full.put("java", System.getProperty("java.version", "unknown"));
            full.putAll(metadata);
            opened.start(full);

            session = opened;
            Runtime.getRuntime().addShutdownHook(new Thread(FrameHook::finish, "mcbench-agent-finish"));
            return true;
        } catch (IOException | RuntimeException e) {
            // A probe that cannot start must not prevent the game from starting.
            disabled = true;
            return false;
        }
    }

    /**
     * Where the agent writes.
     *
     * <p>Deliberately a *separate* file from a loader adapter's stream. Both can be active at
     * once — an adapter driving the workload and ticks while the agent supplies frames — and two
     * writers appending to one file would interleave into an unparseable mess. Keeping them
     * apart lets the harness read whichever it wants, or both.
     */
    static Path agentOutput(ProbeConfig config) {
        String override = System.getenv("MCBENCH_AGENT_OUTPUT");
        if (override != null && !override.isBlank()) {
            return Path.of(override);
        }
        Path base = config.output();
        Path parent = base.getParent();
        String name = "probe-agent.jsonl";
        return parent == null ? Path.of(name) : parent.resolve(name);
    }

    /** Close the session. Idempotent, and safe from a shutdown hook. */
    public static synchronized void finish() {
        ProbeSession current = session;
        if (current == null) {
            return;
        }
        session = null;
        try {
            current.close();
        } catch (RuntimeException ignored) {
            // Shutdown is already in progress; there is nowhere useful to report this.
        }
    }

    /** Frames observed so far. Test seam, and useful in the agent's own diagnostics. */
    public static long framesSeen() {
        return framesSeen;
    }

    public static boolean active() {
        return session != null && !disabled;
    }

    /** Test seam: reset static state between cases. */
    static synchronized void resetForTesting() {
        finish();
        STARTED.set(false);
        disabled = false;
        lastFrameNanos = 0;
        framesSeen = 0;
    }
}
