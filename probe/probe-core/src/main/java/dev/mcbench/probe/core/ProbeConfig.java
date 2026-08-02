package dev.mcbench.probe.core;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;

/**
 * The probe's execution plan, handed over by the harness.
 *
 * <p>Deliberately a flat {@code .properties} file plus a plain-text command list rather than the
 * scenario JSON itself. Two reasons:
 *
 * <ul>
 *   <li><b>Methodology stays in one place.</b> The harness already validates scenarios and owns
 *       every rule in {@code docs/METHODOLOGY.md}. If the probe reinterpreted the scenario, the
 *       two could drift, and a disagreement about what a scenario means is far harder to notice
 *       than a crash.
 *   <li><b>No parser, no dependency.</b> {@code java.util.Properties} is in the JDK. Pulling a
 *       JSON library into a running Minecraft JVM risks colliding with whatever version the
 *       game or another mod already loaded — a failure mode that appears only in the field.
 * </ul>
 */
public final class ProbeConfig {

    private final String scenarioId;
    private final String scenarioVersion;
    private final String contentHash;
    private final Side side;
    private final double warmupMinimum;
    private final double warmupMaxMultiple;
    private final double steadyStateTolerance;
    private final int steadyStateWindow;
    private final double measurementDuration;
    private final boolean tickWarp;
    private final boolean saturated;
    private final Path output;
    private final List<String> setupCommands;
    private final List<String> workloadCommands;

    public enum Side {
        CLIENT,
        SERVER,
        BOTH;

        static Side parse(String raw) {
            return switch (raw == null ? "" : raw.trim().toLowerCase()) {
                case "client" -> CLIENT;
                case "server" -> SERVER;
                case "both" -> BOTH;
                default -> throw new IllegalArgumentException("unknown side: " + raw);
            };
        }

        public boolean measuresFrames() {
            return this == CLIENT || this == BOTH;
        }

        public boolean measuresTicks() {
            return this == SERVER || this == BOTH;
        }
    }

    private ProbeConfig(Properties p, List<String> setup, List<String> workload, Path base) {
        this.scenarioId = required(p, "scenario.id");
        this.scenarioVersion = p.getProperty("scenario.version", "0.0.0");
        this.contentHash = p.getProperty("scenario.content_hash", "");
        this.side = Side.parse(required(p, "scenario.side"));
        this.warmupMinimum = positiveDouble(p, "warmup.min");
        this.warmupMaxMultiple = doubleOr(p, "warmup.max_multiple", 3.0);
        this.steadyStateTolerance = doubleOr(p, "warmup.steady_state_tolerance", 0.05);
        this.steadyStateWindow = intOr(p, "warmup.steady_state_window", 240);
        this.measurementDuration = positiveDouble(p, "measurement.duration");
        this.tickWarp = boolOr(p, "measurement.tick_warp", false);
        this.saturated = boolOr(p, "measurement.saturated", false);
        String out = p.getProperty("output", "mcbench/probe.jsonl");
        Path candidate = Path.of(out);
        this.output = candidate.isAbsolute() ? candidate : base.resolve(candidate);
        this.setupCommands = List.copyOf(setup);
        this.workloadCommands = List.copyOf(workload);
    }

    /**
     * Load a config and its adjacent command files.
     *
     * <p>{@code setup.txt} and {@code workload.txt} are resolved next to the properties file, so
     * the whole plan is one self-describing directory the harness writes and the probe reads.
     */
    public static ProbeConfig load(Path propertiesFile) throws IOException {
        Properties properties = new Properties();
        try (InputStream in = Files.newInputStream(propertiesFile)) {
            properties.load(in);
        }
        Path base = propertiesFile.toAbsolutePath().getParent();
        if (base == null) {
            base = Path.of(".");
        }
        return new ProbeConfig(
                properties,
                readLines(base.resolve("setup.txt")),
                readLines(base.resolve("workload.txt")),
                base);
    }

    /** Test seam. */
    public static ProbeConfig of(Properties p, List<String> setup, List<String> workload, Path base) {
        return new ProbeConfig(p, setup, workload, base);
    }

    private static List<String> readLines(Path path) throws IOException {
        if (!Files.exists(path)) {
            return List.of();
        }
        List<String> out = new ArrayList<>();
        for (String line : Files.readAllLines(path)) {
            String trimmed = line.strip();
            // '#' starts a comment so the harness can annotate generated command files, which
            // makes a failed run far easier to read after the fact.
            if (!trimmed.isEmpty() && !trimmed.startsWith("#")) {
                out.add(trimmed);
            }
        }
        return out;
    }

    private static String required(Properties p, String key) {
        String value = p.getProperty(key);
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("probe config is missing required key: " + key);
        }
        return value.trim();
    }

    private static double positiveDouble(Properties p, String key) {
        double value = Double.parseDouble(required(p, key));
        if (!(value > 0)) {
            throw new IllegalArgumentException(key + " must be positive, got " + value);
        }
        return value;
    }

    private static double doubleOr(Properties p, String key, double fallback) {
        String value = p.getProperty(key);
        return (value == null || value.isBlank()) ? fallback : Double.parseDouble(value.trim());
    }

    private static int intOr(Properties p, String key, int fallback) {
        String value = p.getProperty(key);
        return (value == null || value.isBlank()) ? fallback : Integer.parseInt(value.trim());
    }

    private static boolean boolOr(Properties p, String key, boolean fallback) {
        String value = p.getProperty(key);
        return (value == null || value.isBlank()) ? fallback : Boolean.parseBoolean(value.trim());
    }

    public String scenarioId() {
        return scenarioId;
    }

    public String scenarioVersion() {
        return scenarioVersion;
    }

    public String contentHash() {
        return contentHash;
    }

    public Side side() {
        return side;
    }

    public double warmupMinimum() {
        return warmupMinimum;
    }

    public double warmupMaxMultiple() {
        return warmupMaxMultiple;
    }

    public double steadyStateTolerance() {
        return steadyStateTolerance;
    }

    public int steadyStateWindow() {
        return steadyStateWindow;
    }

    public double measurementDuration() {
        return measurementDuration;
    }

    public boolean tickWarp() {
        return tickWarp;
    }

    public boolean saturated() {
        return saturated;
    }

    public Path output() {
        return output;
    }

    public List<String> setupCommands() {
        return setupCommands;
    }

    public List<String> workloadCommands() {
        return workloadCommands;
    }
}
