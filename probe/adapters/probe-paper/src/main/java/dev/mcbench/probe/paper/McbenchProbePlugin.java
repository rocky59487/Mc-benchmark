package dev.mcbench.probe.paper;

import dev.mcbench.probe.core.ProbeConfig;
import dev.mcbench.probe.core.ProbeSession;
import java.util.Map;
import org.bukkit.Bukkit;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;

/**
 * Paper/Spigot entrypoint.
 *
 * <p>Inert unless {@code MCBENCH_PROBE_CONFIG} is set, so it is harmless left installed on a
 * normal server.
 *
 * <p><b>Tick timing.</b> Bukkit exposes no tick event, and a repeating task cannot bracket a
 * tick: it runs <em>inside</em> one, so two tasks a tick apart measure the gap between two
 * points within it. The interval between consecutive invocations is the tick <em>period</em>,
 * which on a server under its budget is a constant 50 ms whatever the tick actually cost —
 * publishing that as MSPT made the primary server metrics a measurement of the scheduler.
 *
 * <p>Paper exposes the real figure through {@code Server#getTickTimes()}, and the adapter reads
 * it where it exists. On Spigot, which does not, the period is recorded instead and published
 * under {@code tick_period_*} rather than as MSPT.
 *
 * <p>Two scheduled tasks rather than one, matching the Fabric adapter: timing must not include
 * the cost of the commands the workload issues, so {@code pump()} runs in a separate task that
 * executes after the timing sample has been taken.
 */
public final class McbenchProbePlugin extends JavaPlugin {

    private PaperProbeAdapter adapter;
    private BukkitTask timingTask;
    private BukkitTask pumpTask;
    private Thread shutdownHook;

    @Override
    public void onEnable() {
        ProbeSession session = ProbeSession.fromEnvironment();
        if (session == null) {
            getLogger().info("mcbench probe idle: MCBENCH_PROBE_CONFIG is not set.");
            return;
        }

        if (session.config().side() == ProbeConfig.Side.CLIENT) {
            // Refused rather than run: a client scenario on a headless server would record no
            // frames at all, and an empty result is far more confusing than a clear refusal.
            getLogger().severe(
                    "mcbench probe: scenario '" + session.config().scenarioId()
                            + "' is client-side and cannot run on a Paper server.");
            session.reportError("client-side scenario dispatched to a Paper server");
            session.close();
            return;
        }

        adapter = new PaperProbeAdapter(session, this);

        Map<String, String> metadata = Map.of(
                "server_brand", Bukkit.getName(),
                "server_version", Bukkit.getVersion(),
                "bukkit_version", Bukkit.getBukkitVersion());
        adapter.begin(metadata);

        // Period 1 = every tick, on the main thread. sampleTick() reads Paper's own duration
        // for the tick that just finished, and only falls back to the interval where the
        // server cannot supply one.
        timingTask = Bukkit.getScheduler().runTaskTimer(this, adapter::sampleTick, 0L, 1L);
        pumpTask = Bukkit.getScheduler().runTaskTimer(this, adapter::pump, 1L, 1L);

        // The harness treats a missing 'bye' as a crash, so an unclean exit that skipped
        // onDisable would misreport a good run as failed.
        shutdownHook = new Thread(this::finishQuietly, "mcbench-probe-finish");
        Runtime.getRuntime().addShutdownHook(shutdownHook);

        getLogger().info(
                "mcbench probe active: scenario " + session.config().scenarioId()
                        + " (" + session.config().setupCommands().size() + " setup commands)");
    }

    @Override
    public void onDisable() {
        if (timingTask != null) {
            timingTask.cancel();
        }
        if (pumpTask != null) {
            pumpTask.cancel();
        }
        if (shutdownHook != null) {
            try {
                Runtime.getRuntime().removeShutdownHook(shutdownHook);
            } catch (IllegalStateException ignored) {
                // Already shutting down; the hook is running or has run.
            }
        }
        finishQuietly();
    }

    private void finishQuietly() {
        PaperProbeAdapter current = adapter;
        if (current != null && !current.isFinished()) {
            current.finish();
        }
    }
}
