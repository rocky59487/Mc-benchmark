package dev.mcbench.probe.agent;

import java.lang.instrument.Instrumentation;
import java.util.Map;

/**
 * Agent entry point: {@code -javaagent:mcbench-probe-agent.jar}.
 *
 * <p>The universal fallback described in {@code docs/PLATFORMS.md}. Loader adapters cover the
 * modern ecosystem but each inherits its loader's version range; this covers everything else,
 * including vanilla, by instrumenting LWJGL rather than the game.
 *
 * <p><b>What it does and does not do.</b> The agent is a *timing source*, not a full platform.
 * It supplies frame timings; it cannot execute commands, because it has no access to any game
 * API — that is the price of not coupling to one. So it either runs alongside a loader adapter
 * that drives the workload, or measures a workload driven some other way. Stating that plainly
 * matters: an agent-only run on a scenario that needs setup commands would produce frame
 * timings for a world that was never built.
 *
 * <p>It is inert unless {@code MCBENCH_PROBE_CONFIG} is set, so it is harmless to leave on a
 * launch command permanently.
 */
public final class McbenchAgent {

    private McbenchAgent() {}

    /**
     * Standard agent entry point, invoked before the main class loads.
     *
     * <p>Nothing here may throw. An exception out of {@code premain} aborts JVM startup, so a
     * misconfigured benchmark would present as a game that will not launch at all.
     */
    public static void premain(String args, Instrumentation instrumentation) {
        try {
            install(args, instrumentation);
        } catch (Throwable t) {
            System.err.println("[mcbench] agent disabled: " + t);
        }
    }

    /** Attach-on-the-fly entry point, for completeness. */
    public static void agentmain(String args, Instrumentation instrumentation) {
        premain(args, instrumentation);
    }

    private static void install(String args, Instrumentation instrumentation) {
        if (!FrameHook.start(Map.of("agent_args", args == null ? "" : args))) {
            // Not launched by mcbench, or a server-side scenario with no frames to time.
            // Installing a transformer anyway would add work to every class load for nothing.
            return;
        }

        // Note what is deliberately NOT done here: appending the agent jar to the bootstrap
        // classloader search. That looks like a way to make the hook visible everywhere, and
        // it does — but it also puts a *second* copy of the agent's own classes on a different
        // loader, and the two halves then cannot see each other's package-private state. It
        // fails with an IllegalAccessError before instrumenting anything.
        //
        // Instead the transformer checks, per classloader, whether the hook actually resolves,
        // and declines to instrument when it does not. Refusing costs frame timing; injecting
        // a call that cannot link costs the whole launch.
        SwapBufferTransformer transformer = new SwapBufferTransformer();
        instrumentation.addTransformer(transformer, false);

        System.err.println(
                "[mcbench] agent active; will instrument "
                        + String.join(" or ", SwapBufferTransformer.TARGETS.keySet()));
    }

}
