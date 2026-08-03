package dev.mcbench.probe.core;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Random;

/**
 * Conformance tool: drives a real {@link ProbeSession} with synthetic timings and writes a real
 * probe stream.
 *
 * <p>This exists because the probe protocol has two independent implementations — this Java
 * writer and the Python reader in {@code src/mcbench/runner/protocol.py} — and a wire format
 * with two implementations will drift unless something checks. Running this and parsing its
 * output with the harness exercises the actual contract rather than a mock of it.
 *
 * <p>It also gives platform adapters something to verify against: an adapter author can compare
 * their stream to one produced here without needing a GPU, an account, or a working instance.
 *
 * <p>The timings are synthetic and the emitted stream says so in its metadata, so a file
 * produced here can never be mistaken for a measurement.
 */
public final class SelfTest {

    private SelfTest() {}

    /**
     * @param args {@code <output-dir> [side] [seed]}
     */
    public static void main(String[] args) throws IOException {
        if (args.length < 1) {
            System.err.println("usage: SelfTest <output-dir> [client|server] [seed]");
            System.exit(2);
        }
        Path dir = Path.of(args[0]);
        String side = args.length > 1 ? args[1] : "client";
        long seed = args.length > 2 ? Long.parseLong(args[2]) : 42L;

        Files.createDirectories(dir);
        Path output = dir.resolve("probe.jsonl");

        if (side.equals("server")) {
            generateServerRun(output, seed);
        } else {
            generateClientRun(output, seed);
        }
        System.out.println(output.toAbsolutePath());
    }

    /** Simulates a client run: a JIT warmup ramp settling into steady frames. */
    public static void generateClientRun(Path output, long seed) throws IOException {
        Random random = new Random(seed);
        ProbeConfig config = config("client", 3.0, 8.0, output);

        try (ProbeWriter writer = new ProbeWriter(output);
                ProbeSession session = new ProbeSession(config, writer)) {
            session.start(metadata("client", seed));

            // Warmup: frames start slow and decay toward steady state, which is what JIT
            // compilation and cold caches actually look like.
            double frameMs = 60.0;
            for (int i = 0; i < 900 && !session.isComplete(); i++) {
                frameMs = 16.7 + (frameMs - 16.7) * 0.96;
                session.recordFrame(jitter(random, frameMs, 0.06));
            }

            // Measurement: steady frames with occasional GC-shaped spikes, so the tail metrics
            // and the CDF have something real to describe.
            for (int i = 0; i < 12_000 && !session.isComplete(); i++) {
                double ms = (i % 700 == 699) ? 55.0 : 16.7;
                session.recordFrame(jitter(random, ms, 0.05));
            }
        }
    }

    /** Simulates a server run under tick warp. */
    public static void generateServerRun(Path output, long seed) throws IOException {
        Random random = new Random(seed);
        ProbeConfig config = config("server", 300.0, 2000.0, output);

        try (ProbeWriter writer = new ProbeWriter(output);
                ProbeSession session = new ProbeSession(config, writer)) {
            session.start(metadata("server", seed));

            double tickMs = 40.0;
            for (int i = 0; i < 4000 && !session.isComplete(); i++) {
                tickMs = 12.0 + (tickMs - 12.0) * 0.99;
                session.recordTick(jitter(random, tickMs, 0.05), TickSource.BRACKET);
            }
            for (int i = 0; i < 20_000 && !session.isComplete(); i++) {
                double ms = (i % 500 == 499) ? 34.0 : 12.0;
                session.recordTick(jitter(random, ms, 0.04), TickSource.BRACKET);
            }
            session.addChunksGenerated(1280);
            session.recordFingerprint("selftest-synthetic-world");
        }
    }

    private static ProbeConfig config(String side, double warmup, double duration, Path output) {
        Properties p = new Properties();
        p.setProperty("scenario.id", "selftest-" + side);
        p.setProperty("scenario.version", "1.0.0");
        p.setProperty("scenario.side", side);
        p.setProperty("warmup.min", Double.toString(warmup));
        p.setProperty("warmup.max_multiple", "3");
        p.setProperty("warmup.steady_state_window", side.equals("server") ? "200" : "120");
        p.setProperty("warmup.steady_state_tolerance", "0.05");
        p.setProperty("measurement.duration", Double.toString(duration));
        p.setProperty("output", output.toString());
        return ProbeConfig.of(p, List.of(), List.of(), Path.of("."));
    }

    private static Map<String, String> metadata(String side, long seed) {
        Map<String, String> metadata = new LinkedHashMap<>();
        // Recorded so a self-test stream can never be mistaken for a real measurement.
        metadata.put("synthetic", "true");
        metadata.put("source", "probe-core SelfTest");
        metadata.put("side", side);
        metadata.put("seed", Long.toString(seed));
        metadata.put("java", System.getProperty("java.version", "unknown"));
        return metadata;
    }

    private static long jitter(Random random, double meanMs, double relativeSpread) {
        double value = meanMs * (1.0 + (random.nextDouble() * 2 - 1) * relativeSpread);
        return (long) Math.max(1.0, value * 1_000_000.0);
    }
}
