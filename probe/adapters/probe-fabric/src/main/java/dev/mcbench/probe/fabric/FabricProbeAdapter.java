package dev.mcbench.probe.fabric;

import com.mojang.brigadier.exceptions.CommandSyntaxException;
import dev.mcbench.probe.core.ProbeAdapter;
import dev.mcbench.probe.core.ProbeSession;
import java.util.LinkedHashMap;
import java.util.Map;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.command.ServerCommandSource;

/**
 * Fabric implementation of the three platform methods.
 *
 * <p>Everything else — phases, steady-state detection, buffering, protocol emission — comes
 * from {@link ProbeAdapter} and probe-core, which have no idea Minecraft exists. See
 * {@code docs/PLATFORMS.md}.
 */
public final class FabricProbeAdapter extends ProbeAdapter {

    private volatile MinecraftServer server;
    private volatile Runnable shutdownHook;

    public FabricProbeAdapter(ProbeSession session) {
        super(session);
    }

    @Override
    public String platformName() {
        return "fabric";
    }

    /**
     * Attach the server used to run commands.
     *
     * <p>Commands go through the server even for client scenarios, because in single player the
     * integrated server is what owns world state. Issuing them client-side would be both
     * version-fragile and, worse, silently partial.
     */
    public void attachServer(MinecraftServer server) {
        this.server = server;
    }

    public void onShutdownRequested(Runnable hook) {
        this.shutdownHook = hook;
    }

    @Override
    protected boolean executeCommand(String command) {
        MinecraftServer current = server;
        if (current == null) {
            session.reportError("no server attached; dropped command: " + command);
            return false;
        }
        ServerCommandSource source = current.getCommandSource();
        // Commands rather than direct API calls: /summon, /fill, /setblock and /gamerule have
        // been compatible for a decade, while the Java methods behind them have been renamed
        // repeatedly. That stability is what makes one scenario file work across versions.
        //
        // Dispatched directly rather than through executeWithPrefix, which swallows the
        // result: a rejected command logged a chat message and returned normally, so a
        // scenario built on a typo reported no failure and measured an empty world.
        try {
            current.getCommandManager().getDispatcher()
                    .execute(command.startsWith("/") ? command.substring(1) : command, source);
            return true;
        } catch (CommandSyntaxException e) {
            session.reportError("command rejected: " + command + ": " + e.getMessage());
            return false;
        }
    }

    @Override
    protected void requestShutdown() {
        Runnable hook = shutdownHook;
        if (hook != null) {
            hook.run();
            return;
        }
        MinecraftServer current = server;
        if (current != null) {
            current.stop(false);
        }
    }

    /**
     * @param window the framebuffer size, or empty on a server or before one exists. Empty is
     *     omitted rather than reported as a placeholder: the harness treats a field the probe
     *     does not report as unknown, and a stated zero would be a claim.
     */
    public void beginWithGameMetadata(
            String minecraftVersion, String loaderVersion, String window) {
        Map<String, String> metadata = new LinkedHashMap<>();
        metadata.put("minecraft_version", minecraftVersion);
        metadata.put("loader_version", loaderVersion);
        if (!window.isEmpty()) {
            metadata.put("window", window);
        }
        begin(metadata);
    }
}
