package dev.mcbench.probe.paper;

import dev.mcbench.probe.core.ProbeAdapter;
import dev.mcbench.probe.core.ProbeSession;
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

    public PaperProbeAdapter(ProbeSession session, Plugin plugin) {
        super(session);
        this.plugin = plugin;
    }

    @Override
    public String platformName() {
        return "paper";
    }

    @Override
    protected void executeCommand(String command) {
        // Dispatched as console so scenario commands are never subject to a player's
        // permissions or position. Commands rather than the Bukkit API for the same reason as
        // on Fabric: /fill and /summon outlive the Java methods behind them.
        Bukkit.dispatchCommand(Bukkit.getConsoleSender(), command);
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
