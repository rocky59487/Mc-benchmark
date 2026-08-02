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
 * <p><b>Tick timing.</b> Bukkit exposes no tick event, but a repeating task scheduled at a
 * one-tick period runs exactly once per server tick on the main thread. Measuring the interval
 * between consecutive invocations gives the same quantity as a tick event would, using an API
 * that has been stable since Bukkit's earliest versions — considerably longer than any internal
 * hook would have survived.
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

        // Period 1 = every tick. Runs on the main thread, so the interval it measures is real
        // server tick time.
        timingTask = Bukkit.getScheduler().runTaskTimer(this, adapter::onTick, 0L, 1L);
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
