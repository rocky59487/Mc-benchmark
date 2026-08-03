package dev.mcbench.probe.forge;

import com.mojang.brigadier.exceptions.CommandSyntaxException;
import dev.mcbench.probe.core.ProbeAdapter;
import dev.mcbench.probe.core.ProbeSession;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.server.MinecraftServer;

/**
 * Forge implementation of the three platform methods.
 *
 * <p>The fourth adapter, and the strongest evidence the SPI is the right size: Fabric, NeoForge,
 * Paper and Forge are four unrelated ecosystems, and their adapters are four nearly identical
 * files, none containing a line of methodology. Everything that decides what a number *means*
 * lives in probe-core and is shared verbatim.
 *
 * <p>Forge uses Mojang's official mappings, so the game class names here match the NeoForge
 * adapter's rather than the Fabric adapter's Yarn names. That difference is confined to this
 * file, which is the point.
 */
public final class ForgeProbeAdapter extends ProbeAdapter {

    private volatile MinecraftServer server;
    private volatile Runnable shutdownHook;

    public ForgeProbeAdapter(ProbeSession session) {
        super(session);
    }

    @Override
    public String platformName() {
        return "forge";
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

    /** Client runs have no server to stop; this is how the client closes itself instead. */
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
        CommandSourceStack source = current.createCommandSourceStack();
        // Commands rather than direct API calls, for the same reason as on every other
        // platform: /fill, /summon and /gamerule outlive the Java methods behind them, and that
        // stability is what lets one scenario file run across versions.
        //
        // Dispatched directly so a rejection is visible rather than logged and discarded.
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
        Runnable hook = shutdownHook;
        if (hook != null) {
            hook.run();
            return;
        }
        MinecraftServer current = server;
        if (current != null) {
            current.halt(false);
        }
    }
}
