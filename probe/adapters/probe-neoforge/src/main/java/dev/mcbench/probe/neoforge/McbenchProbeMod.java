package dev.mcbench.probe.neoforge;

import dev.mcbench.probe.core.ProbeSession;
import java.util.Map;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.client.event.ClientTickEvent;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.event.server.ServerStartedEvent;
import net.neoforged.neoforge.event.server.ServerStoppingEvent;
import net.neoforged.neoforge.event.tick.ServerTickEvent;

/**
 * NeoForge entrypoint. Wires platform events onto the adapter and nothing else.
 *
 * <p>Inert unless {@code MCBENCH_PROBE_CONFIG} is set, so it is harmless left installed in a
 * normal play session.
 *
 * <p><b>Frame timing.</b> NeoForge has no per-frame event either, so the client tick is used as
 * the shutdown driver only, and frame timing on this platform comes from the JVM agent described
 * in {@code docs/PLATFORMS.md} rather than from a mixin into the render loop. Choosing the agent
 * over a mixin is the same trade the Fabric adapter makes with
 * {@code HudRenderCallback}: the render method's name and signature move between versions, and
 * anything anchored to them inherits that fragility.
 *
 * <p>Server-side scenarios are fully supported here today; client scenarios on NeoForge need the
 * agent, and the harness reports that rather than recording an empty run.
 */
@Mod(McbenchProbeMod.MOD_ID)
public final class McbenchProbeMod {

    public static final String MOD_ID = "mcbench_probe";

    private static volatile NeoForgeProbeAdapter adapter;

    public McbenchProbeMod(IEventBus modBus) {
        ProbeSession session = ProbeSession.fromEnvironment();
        if (session == null) {
            // Not launched by mcbench. Do nothing at all.
            return;
        }

        NeoForgeProbeAdapter created = new NeoForgeProbeAdapter(session);
        adapter = created;

        IEventBus gameBus = NeoForge.EVENT_BUS;

        gameBus.addListener(ServerStartedEvent.class, event -> {
            created.attachServer(event.getServer());
            // Started only once the server can accept commands; starting earlier would time
            // world loading as though it were warmup.
            created.begin(Map.of("neoforge", "true"));
        });

        if (created.measuresTicks()) {
            gameBus.addListener(ServerTickEvent.Post.class, event -> created.onTick());
        }

        // pump() runs at the end of the server tick, where issuing commands is safe, and is
        // kept out of the timing hook so command execution never lands inside a measured
        // interval.
        gameBus.addListener(ServerTickEvent.Post.class, event -> created.pump());

        gameBus.addListener(ServerStoppingEvent.class, event -> created.finish());

        // A client-only run has no server lifecycle to close it, so the client tick drives
        // shutdown once the measurement window has elapsed.
        gameBus.addListener(ClientTickEvent.Post.class, event -> {
            NeoForgeProbeAdapter current = adapter;
            if (current != null && current.isFinished()) {
                net.minecraft.client.Minecraft.getInstance().stop();
            }
        });

        // The harness treats a missing 'bye' as a crash, so an unclean exit that skipped
        // finish() would misreport a perfectly good run as failed.
        Runtime.getRuntime().addShutdownHook(new Thread(created::finish, "mcbench-probe-finish"));
    }
}
