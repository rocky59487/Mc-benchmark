package dev.mcbench.probe.forge;

import net.minecraft.client.Minecraft;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.event.TickEvent;

/**
 * Client-side wiring, held apart from {@link McbenchProbeMod} so a dedicated server never has
 * cause to resolve a client class.
 *
 * <p>Java would in practice get away with inlining this into a lambda that a server never
 * invokes — resolution is lazy — but "in practice" is doing real work in that sentence, and a
 * benchmarking tool that occasionally refuses to boot a server is worse than one that is
 * slightly more verbose.
 */
final class ClientHooks {

    private ClientHooks() {}

    static void install(ForgeProbeAdapter adapter) {
        if (adapter.measuresFrames()) {
            // Forge's own per-frame event. Post rather than Pre so the frame's work is
            // included; measuring the interval between consecutive Post events gives the
            // frame duration without bracketing anything.
            MinecraftForge.EVENT_BUS.addListener(
                    (TickEvent.RenderTickEvent.Post event) -> adapter.onFrame());
        }

        // A client-only run has no server lifecycle to close it, so the client tick drives
        // shutdown once the measurement window has elapsed. The client tick is used rather
        // than the render tick because stopping the game from inside the render loop is a
        // reliable way to produce a crash log instead of a result.
        MinecraftForge.EVENT_BUS.addListener((TickEvent.ClientTickEvent.Post event) -> {
            if (adapter.isFinished()) {
                Minecraft.getInstance().stop();
            }
        });

        adapter.onShutdownRequested(() -> Minecraft.getInstance().stop());
    }
}
