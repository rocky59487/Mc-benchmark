package dev.mcbench.probe.forge;

import dev.mcbench.probe.core.ProbeSession;
import java.util.Map;
import net.minecraftforge.api.distmarker.Dist;
import net.minecraftforge.common.MinecraftForge;
import net.minecraftforge.event.TickEvent;
import net.minecraftforge.event.server.ServerStartedEvent;
import net.minecraftforge.event.server.ServerStoppingEvent;
import net.minecraftforge.fml.common.Mod;
import net.minecraftforge.fml.loading.FMLEnvironment;

/**
 * Forge entrypoint. Wires platform events onto the adapter and does nothing else.
 *
 * <p>Inert unless {@code MCBENCH_PROBE_CONFIG} is set, so it is harmless left installed in a
 * normal play session.
 *
 * <p><b>Frame timing.</b> Forge is the one loader with a per-frame event of its own:
 * {@code TickEvent.RenderTickEvent} fires around the client's render tick, once per frame, with
 * no mixin required. Fabric and NeoForge have no equivalent, which is why they lean on
 * {@code HudRenderCallback} and the JVM agent respectively.
 *
 * <p>The agent nevertheless remains the more precise instrument, and the harness prefers it when
 * both are present: {@code RenderTickEvent} brackets Minecraft's render tick, while the agent
 * instruments the buffer swap itself, which is the actual moment a frame is presented. The
 * difference is small and systematic rather than random — but a systematic difference is exactly
 * the kind that survives averaging, so the two are never mixed within a run
 * ({@code adopt_agent_frames} substitutes, it does not merge).
 *
 * <p>No mod event bus is used. Every hook here is a game-bus event, so a no-argument constructor
 * and {@code MinecraftForge.EVENT_BUS} cover it — and constructor injection is the part of
 * Forge's entrypoint contract that has changed most between versions.
 */
@Mod(McbenchProbeMod.MOD_ID)
public final class McbenchProbeMod {

    public static final String MOD_ID = "mcbench_probe";

    private static volatile ForgeProbeAdapter adapter;

    public McbenchProbeMod() {
        ProbeSession session = ProbeSession.fromEnvironment();
        if (session == null) {
            // Not launched by mcbench. Do nothing at all.
            return;
        }

        ForgeProbeAdapter created = new ForgeProbeAdapter(session);
        adapter = created;

        MinecraftForge.EVENT_BUS.addListener((ServerStartedEvent event) -> {
            created.attachServer(event.getServer());
            // Started only once the server can accept commands. Starting earlier would time
            // world loading as though it were warmup.
            created.begin(Map.of(
                    "forge", "true",
                    "dist", String.valueOf(FMLEnvironment.dist)));
        });

        if (created.measuresTicks()) {
            MinecraftForge.EVENT_BUS.addListener(
                    (TickEvent.ServerTickEvent.Post event) -> created.onTick());
        }

        // pump() runs at the end of the server tick, where issuing commands is safe, and is
        // deliberately kept out of the timing hook so command execution never lands inside a
        // measured interval.
        MinecraftForge.EVENT_BUS.addListener(
                (TickEvent.ServerTickEvent.Post event) -> created.pump());

        MinecraftForge.EVENT_BUS.addListener(
                (ServerStoppingEvent event) -> created.finish());

        if (FMLEnvironment.dist == Dist.CLIENT) {
            // Client-only wiring lives behind this guard and in a separate class, so a
            // dedicated server never has cause to resolve a single client class.
            ClientHooks.install(created);
        }

        // The harness treats a missing 'bye' as a crash, so an unclean exit that skipped
        // finish() would misreport a perfectly good run as failed.
        Runtime.getRuntime().addShutdownHook(new Thread(created::finish, "mcbench-probe-finish"));
    }

    /** The active adapter, or null when not launched by mcbench. */
    static ForgeProbeAdapter adapter() {
        return adapter;
    }
}
