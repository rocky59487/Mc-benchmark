package dev.mcbench.probe.fabric;

import dev.mcbench.probe.core.ProbeSession;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.client.rendering.v1.HudRenderCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.client.MinecraftClient;

/**
 * Fabric entrypoint. Wires the platform's events onto the adapter and does nothing else.
 *
 * <p>The mod is inert unless {@code MCBENCH_PROBE_CONFIG} is set, so it is harmless to leave
 * installed in a normal play session — {@link ProbeSession#fromEnvironment()} returns null and
 * every hook short-circuits.
 *
 * <p><b>Why {@code HudRenderCallback} for frame timing.</b> Fabric API has no per-frame event,
 * and the obvious alternative is a mixin into the client's render method — which is precisely
 * the kind of version-fragile coupling {@code docs/PLATFORMS.md} exists to avoid, since that
 * method's name and signature move between versions. {@code HudRenderCallback} fires once per
 * presented frame, is part of the stable Fabric API surface, and needs no mixin at all.
 *
 * <p>The tradeoff is that it does not fire while the HUD is hidden or on some screens. Benchmark
 * scenarios control both, and a scenario that suppressed the HUD would simply record no frames
 * — a visible absence rather than a silent distortion.
 */
public final class McbenchProbeMod implements ModInitializer, ClientModInitializer {

    private static volatile FabricProbeAdapter adapter;

    @Override
    public void onInitialize() {
        ProbeSession session = ProbeSession.fromEnvironment();
        if (session == null) {
            // Not launched by mcbench. Do nothing at all.
            return;
        }

        FabricProbeAdapter created = new FabricProbeAdapter(session);
        adapter = created;

        String loaderVersion = FabricLoader.getInstance()
                .getModContainer("fabricloader")
                .map(c -> c.getMetadata().getVersion().getFriendlyString())
                .orElse("unknown");
        String minecraftVersion = FabricLoader.getInstance()
                .getModContainer("minecraft")
                .map(c -> c.getMetadata().getVersion().getFriendlyString())
                .orElse("unknown");

        ServerLifecycleEvents.SERVER_STARTED.register(server -> {
            created.attachServer(server);
            created.onShutdownRequested(() -> server.stop(false));
            // Started only once the server can accept commands; starting earlier would time
            // world loading as though it were warmup.
            created.beginWithGameMetadata(minecraftVersion, loaderVersion);
        });

        if (created.measuresTicks()) {
            ServerTickEvents.END_SERVER_TICK.register(server -> created.onTick());
        }

        // pump() runs at the end of the server tick, where issuing commands is safe. Kept out
        // of the timing hooks so command execution never lands inside a measured interval.
        ServerTickEvents.END_SERVER_TICK.register(server -> created.pump());

        ServerLifecycleEvents.SERVER_STOPPING.register(server -> created.finish());

        // The harness treats a missing 'bye' as a crash, so an unclean exit that skipped
        // finish() would misreport a perfectly good run as failed.
        Runtime.getRuntime().addShutdownHook(new Thread(created::finish, "mcbench-probe-finish"));
    }

    @Override
    public void onInitializeClient() {
        HudRenderCallback.EVENT.register((context, tickCounter) -> {
            FabricProbeAdapter current = adapter;
            if (current != null && current.measuresFrames() && !current.isFinished()) {
                current.onFrame();
            }
        });

        // A client-only scenario has no server lifecycle to close the run, so the client
        // drives shutdown once the measurement window has elapsed.
        HudRenderCallback.EVENT.register((context, tickCounter) -> {
            FabricProbeAdapter current = adapter;
            if (current == null || current.isFinished()) {
                return;
            }
            if (current.measuresFrames() && !current.measuresTicks()) {
                current.pump();
                if (current.isFinished()) {
                    MinecraftClient.getInstance().scheduleStop();
                }
            }
        });
    }
}
