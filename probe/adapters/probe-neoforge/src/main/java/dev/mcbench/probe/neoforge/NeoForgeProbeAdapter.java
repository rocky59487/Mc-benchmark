package dev.mcbench.probe.neoforge;

import com.mojang.brigadier.exceptions.CommandSyntaxException;
import dev.mcbench.probe.core.ProbeAdapter;
import dev.mcbench.probe.core.ProbeSession;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.server.MinecraftServer;

/**
 * NeoForge implementation of the three platform methods.
 *
 * <p>Compare this with the Fabric and Paper adapters: three unrelated platforms, three nearly
 * identical files, none of them containing a line of methodology. That is the SPI doing its job
 * — everything deciding what a number means lives in probe-core and is shared verbatim.
 *
 * <p>NeoForge uses Mojang's official mappings rather than Yarn, so the class names here differ
 * from the Fabric adapter's even though the operations are the same. That difference is confined
 * to this file, which is exactly the point of keeping the coupled surface to three methods.
 */
public final class NeoForgeProbeAdapter extends ProbeAdapter {

    private volatile MinecraftServer server;

    public NeoForgeProbeAdapter(ProbeSession session) {
        super(session);
    }

    @Override
    public String platformName() {
        return "neoforge";
    }

    /**
     * Attach the server used to run commands.
     *
     * <p>Commands go through the server even for client scenarios, because in single player the
     * integrated server owns world state; issuing them client-side would be both
     * version-fragile and silently partial.
     */
    public void attachServer(MinecraftServer server) {
        this.server = server;
    }

    @Override
    protected boolean executeCommand(String command) {
        MinecraftServer current = server;
        if (current == null) {
            session.reportError("no server attached; dropped command: " + command);
            return false;
        }
        CommandSourceStack source = current.createCommandSourceStack();
        // Commands rather than direct API calls, for the same reason as on every other
        // platform: /fill and /summon outlive the Java methods behind them.
        //
        // Dispatched directly so a rejection is visible. performPrefixedCommand reports the
        // failure into chat and returns normally, which let a mistyped setup command build
        // none of the world and still be measured as though it had.
        try {
            current.getCommands().getDispatcher()
                    .execute(command.startsWith("/") ? command.substring(1) : command, source);
            return true;
        } catch (CommandSyntaxException e) {
            session.reportError("command rejected: " + command + ": " + e.getMessage());
            return false;
        }
    }

    @Override
    protected void requestShutdown() {
        MinecraftServer current = server;
        if (current != null) {
            current.halt(false);
        }
    }
}
