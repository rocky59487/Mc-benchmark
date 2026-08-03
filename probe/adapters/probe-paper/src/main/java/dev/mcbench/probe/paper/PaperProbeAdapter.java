package dev.mcbench.probe.paper;

import dev.mcbench.probe.core.ProbeAdapter;
import dev.mcbench.probe.core.ProbeSession;
import java.lang.reflect.Method;
import org.bukkit.Bukkit;
import org.bukkit.plugin.Plugin;

/**
 * Paper/Spigot implementation of the three platform methods.
 *
 * <p>Worth noting how little this differs from the Fabric adapter despite Bukkit and Fabric
 * being entirely unrelated platforms — which is the point of keeping the SPI at three methods.
 * Everything that constitutes the benchmark's methodology is in probe-core and is shared
 * verbatim between them.
 *
 * <p>This adapter is server-only by construction. Paper has no client, so scenarios declaring
 * {@code side: client} cannot run here, and the plugin refuses them rather than silently
 * recording no frames.
 */
public final class PaperProbeAdapter extends ProbeAdapter {

    private final Plugin plugin;
    private final Method tickTimes;
    private long lastSeenTick = -1;

    public PaperProbeAdapter(ProbeSession session, Plugin plugin) {
        super(session);
        this.plugin = plugin;
        this.tickTimes = findTickTimes();
    }

    /**
     * Paper's own per-tick durations, if this server exposes them.
     *
     * <p>Bukkit has no tick event, and the scheduler cannot bracket a tick: a repeating task
     * runs *inside* the tick, so two tasks a tick apart measure the gap between two points
     * within it rather than the whole thing. What the previous code measured — the interval
     * between consecutive scheduler invocations — is the tick period, which on a server under
     * its budget is a constant 50 ms regardless of the work done.
     *
     * <p>Paper does expose the real figure: {@code Server#getTickTimes()} returns the last 100
     * ticks' durations in nanoseconds. Reached reflectively so the same jar still runs on
     * Spigot, which has no such method — there the adapter falls back to the period and the
     * samples are published under a name that says so.
     */
    private static Method findTickTimes() {
        try {
            Method method = Bukkit.getServer().getClass().getMethod("getTickTimes");
            if (method.getReturnType() == long[].class) {
                return method;
            }
        } catch (ReflectiveOperationException | RuntimeException ignored) {
            // Not Paper, or a build without it.
        }
        return null;
    }

    /** Whether real tick execution times are available on this server. */
    public boolean hasPlatformTickTimes() {
        return tickTimes != null;
    }

    @Override
    public String platformName() {
        return "paper";
    }

    /**
     * Sample one tick. Called from a scheduler task running every tick on the main thread.
     *
     * <p>Reads Paper's own measurement of the tick that just completed where it can, and only
     * otherwise records the period.
     */
    public void sampleTick() {
        if (tickTimes == null) {
            onTickPeriod();
            return;
        }
        try {
            long[] times = (long[]) tickTimes.invoke(Bukkit.getServer());
            if (times == null || times.length == 0) {
                onTickPeriod();
                return;
            }
            int current = Bukkit.getCurrentTick();
            if (current == lastSeenTick) {
                return;
            }
            lastSeenTick = current;
            // The ring is indexed by tick number; the entry for the tick that just finished is
            // the one before the current index.
            int index = Math.floorMod(current - 1, times.length);
            long duration = times[index];
            if (duration > 0) {
                recordPlatformTick(duration);
            }
        } catch (ReflectiveOperationException | RuntimeException e) {
            // The method disappeared or misbehaved mid-run. Falling back keeps the run alive,
            // and the source change is recorded in the stream.
            onTickPeriod();
        }
    }

    @Override
    protected boolean executeCommand(String command) {
        // Dispatched as console so scenario commands are never subject to a player's
        // permissions or position. Commands rather than the Bukkit API for the same reason as
        // on Fabric: /fill and /summon outlive the Java methods behind them.
        //
        // dispatchCommand already returns whether the command was found and ran, so this
        // platform could always have reported a rejection — nothing was asking.
        String stripped = command.startsWith("/") ? command.substring(1) : command;
        return Bukkit.dispatchCommand(Bukkit.getConsoleSender(), stripped);
    }

    @Override
    protected void requestShutdown() {
        // Scheduled onto the main thread: Bukkit.shutdown() from the shutdown hook thread
        // would re-enter the server's own stop sequence.
        if (Bukkit.isPrimaryThread()) {
            Bukkit.shutdown();
        } else {
            Bukkit.getScheduler().runTask(plugin, Bukkit::shutdown);
        }
    }
}
