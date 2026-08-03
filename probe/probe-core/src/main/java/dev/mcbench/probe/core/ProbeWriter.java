package dev.mcbench.probe.core;

import java.io.BufferedWriter;
import java.io.IOException;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Emits the probe wire protocol: newline-delimited JSON, one event per line.
 *
 * <p>This is the Java half of the contract defined in {@code src/mcbench/runner/protocol.py}.
 * The two must agree exactly, so both sides are versioned by {@link #PROTOCOL_VERSION} and the
 * harness refuses a stream whose version it does not recognise rather than guessing at field
 * meanings.
 *
 * <p>Three properties matter more than compactness:
 *
 * <ul>
 *   <li><b>Append-only, flushed per event.</b> A run killed mid-measurement still leaves a
 *       readable prefix, which is what makes a crashed run diagnosable instead of lost.
 *   <li><b>Raw durations, never pre-aggregated.</b> Every statistic is computed by the
 *       harness. If the probe averaged anything, changing the methodology would mean
 *       redeploying a Java mod, and existing results could never be re-analysed under the
 *       new rule.
 *   <li><b>Nanosecond integers.</b> At 60 fps a millisecond-resolution timer carries about 6%
 *       quantisation error, which is larger than most effects worth resolving.
 * </ul>
 *
 * <p>JSON is written by hand rather than through a library. The output is a handful of flat
 * shapes, and hand-writing keeps probe-core free of dependencies that could collide with
 * whatever the game already has on its classpath.
 */
public final class ProbeWriter implements AutoCloseable {

    /** Must match {@code PROTOCOL_VERSION} in the Python harness. */
    public static final int PROTOCOL_VERSION = 1;

    private final Writer writer;
    private final Object lock = new Object();
    private boolean closed;

    public ProbeWriter(Path output) throws IOException {
        Path parent = output.getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        this.writer = new BufferedWriter(
                Files.newBufferedWriter(
                        output,
                        StandardCharsets.UTF_8,
                        StandardOpenOption.CREATE,
                        StandardOpenOption.WRITE,
                        StandardOpenOption.TRUNCATE_EXISTING));
    }

    /** Test seam: write to any {@link Writer}. */
    public ProbeWriter(Writer writer) {
        this.writer = writer;
    }

    // -- events ----------------------------------------------------------

    public void hello(String probeVersion, Map<String, String> metadata) {
        StringBuilder json = begin("hello");
        json.append(",\"protocol\":").append(PROTOCOL_VERSION);
        json.append(",\"probe_version\":").append(quote(probeVersion));
        json.append(",\"metadata\":{");
        boolean first = true;
        for (Map.Entry<String, String> entry : metadata.entrySet()) {
            if (!first) {
                json.append(',');
            }
            first = false;
            json.append(quote(entry.getKey())).append(':').append(quote(entry.getValue()));
        }
        json.append("}}");
        emit(json);
    }

    public void phase(Phase phase) {
        StringBuilder json = begin("phase");
        json.append(",\"phase\":").append(quote(phase.wireName())).append('}');
        emit(json);
    }

    /** A batch of frame durations. {@code values[0..count)} are used. */
    public void frames(long[] values, int count) {
        emitDurations("frame", values, count, null);
    }

    /**
     * A batch of server tick durations. {@code values[0..count)} are used.
     *
     * <p>The source travels with every batch: period and execution time are different
     * quantities and the harness publishes them under different names.
     */
    public void ticks(long[] values, int count, TickSource source) {
        emitDurations("tick", values, count, source.wireName());
    }

    /**
     * Individual garbage collections.
     *
     * <p>One event per collection, so a pause percentile is a percentile over pauses rather
     * than over per-interval sums.
     */
    public void gcEvents(java.util.List<RuntimeMonitor.GcEvent> events) {
        if (events.isEmpty()) {
            return;
        }
        StringBuilder json = new StringBuilder(events.size() * 160 + 48);
        json.append("{\"type\":").append(quote("gc"));
        json.append(",\"source\":").append(quote("events"));
        json.append(",\"events\":[");
        boolean first = true;
        for (RuntimeMonitor.GcEvent event : events) {
            if (!first) {
                json.append(',');
            }
            first = false;
            json.append("{\"collector\":").append(quote(event.collector()));
            json.append(",\"action\":").append(quote(event.action()));
            json.append(",\"start_ms\":").append(event.startMillis());
            json.append(",\"duration_ms\":").append(event.durationMs());
            json.append(",\"heap_before_mb\":").append(number(event.heapBeforeMb()));
            json.append(",\"heap_after_mb\":").append(number(event.heapAfterMb()));
            json.append(",\"stop_the_world\":").append(event.stopsTheWorld());
            json.append('}');
        }
        json.append("]}");
        emit(json);
    }

    /**
     * Total collection time since the last sample, when per-event data is unavailable.
     *
     * <p>Marked as an aggregate so the harness reports the total and declines to derive a
     * percentile from it.
     */
    public void gcAggregate(long totalPauseMs, long collections) {
        if (totalPauseMs <= 0) {
            return;
        }
        StringBuilder json = begin("gc");
        json.append(",\"source\":").append(quote("aggregate"));
        json.append(",\"total_pause_ms\":").append(totalPauseMs);
        json.append(",\"collections\":").append(collections).append('}');
        emit(json);
    }

    /**
     * A heap sample.
     *
     * <p>Both figures are reported, and {@code real_allocation} says whether the first came
     * from an allocation counter or is a copy of the second.
     */
    public void memory(
            double heapMb,
            boolean postGc,
            long allocatedBytes,
            long heapGrowthBytes,
            boolean realAllocation) {
        StringBuilder json = begin("memory");
        json.append(",\"heap_mb\":").append(number(heapMb));
        json.append(",\"post_gc\":").append(postGc);
        json.append(",\"allocated_bytes\":").append(allocatedBytes);
        json.append(",\"heap_growth_bytes\":").append(heapGrowthBytes);
        json.append(",\"real_allocation\":").append(realAllocation).append('}');
        emit(json);
    }

    public void chunks(long generated, long loaded) {
        StringBuilder json = begin("chunk");
        json.append(",\"generated\":").append(generated);
        json.append(",\"loaded\":").append(loaded).append('}');
        emit(json);
    }

    public void fingerprint(String sha256) {
        StringBuilder json = begin("fingerprint");
        json.append(",\"sha256\":").append(quote(sha256)).append('}');
        emit(json);
    }

    public void flag(String flag) {
        StringBuilder json = begin("flag");
        json.append(",\"flag\":").append(quote(flag)).append('}');
        emit(json);
    }

    public void error(String message) {
        StringBuilder json = begin("error");
        json.append(",\"message\":").append(quote(message)).append('}');
        emit(json);
    }

    /**
     * How the run reached measurement, and what it could and could not measure.
     *
     * @param setupDuration      time spent running scenario setup, in the phase's own unit
     * @param warmupDuration     time spent in warmup, likewise
     * @param gateReason         why warmup ended, in words
     * @param converged          whether the gate opened or the ceiling was hit
     * @param compilationGate    whether the JVM could report compilation time at all
     * @param tickSource         what tick samples measured
     * @param failedSetup        setup commands the game rejected
     * @param failedWorkload     workload commands the game rejected
     * @param realAllocation     whether allocation came from a counter or from heap growth
     * @param gcEvents           whether individual collections were captured
     */
    public record RunSummary(
            double setupDuration,
            double warmupDuration,
            String gateReason,
            boolean converged,
            boolean compilationGate,
            TickSource tickSource,
            int failedSetup,
            int failedWorkload,
            boolean realAllocation,
            boolean gcEvents) {}

    /**
     * Final event. Its absence is how the harness knows a run did not finish, so it is written
     * and flushed before anything else is torn down.
     */
    public void bye(double measurementDurationSeconds, boolean saturated, RunSummary summary) {
        StringBuilder json = begin("bye");
        json.append(",\"measurement_duration_s\":").append(number(measurementDurationSeconds));
        json.append(",\"saturated\":").append(saturated);
        if (summary != null) {
            json.append(",\"setup_duration\":").append(number(summary.setupDuration()));
            json.append(",\"warmup_duration\":").append(number(summary.warmupDuration()));
            json.append(",\"warmup_gate\":").append(quote(summary.gateReason()));
            json.append(",\"warmup_converged\":").append(summary.converged());
            json.append(",\"compilation_gate\":").append(summary.compilationGate());
            json.append(",\"tick_source\":").append(quote(summary.tickSource().wireName()));
            json.append(",\"failed_setup_commands\":").append(summary.failedSetup());
            json.append(",\"failed_workload_commands\":").append(summary.failedWorkload());
            json.append(",\"real_allocation\":").append(summary.realAllocation());
            json.append(",\"gc_events\":").append(summary.gcEvents());
        }
        json.append('}');
        emit(json);
    }

    // -- internals -------------------------------------------------------

    private void emitDurations(String type, long[] values, int count, String source) {
        if (count <= 0) {
            return;
        }
        // Sized up front: a 65k-sample batch would otherwise trigger repeated array copies
        // inside StringBuilder while the game is running.
        StringBuilder json = new StringBuilder(count * 9 + 64);
        json.append("{\"type\":").append(quote(type));
        if (source != null) {
            json.append(",\"source\":").append(quote(source));
        }
        json.append(",\"durations_ns\":[");
        for (int i = 0; i < count; i++) {
            if (i > 0) {
                json.append(',');
            }
            json.append(values[i]);
        }
        json.append("]}");
        emit(json);
    }

    private static StringBuilder begin(String type) {
        return new StringBuilder(96).append("{\"type\":").append(quote(type));
    }

    private void emit(CharSequence json) {
        synchronized (lock) {
            if (closed) {
                return;
            }
            try {
                writer.append(json).append('\n');
                // Flushed per event on purpose. The cost is negligible at flush cadence — this
                // is never called from the frame thread — and it is what guarantees a killed
                // run still leaves a usable prefix.
                writer.flush();
            } catch (IOException e) {
                // Never propagate: the probe must not be able to crash the game it is
                // measuring. A truncated stream is already handled by the harness, which flags
                // the run rather than trusting it.
                closed = true;
            }
        }
    }

    private static String number(double value) {
        if (!Double.isFinite(value)) {
            // JSON has no NaN or Infinity. Emitting one would produce a stream the harness
            // cannot parse, losing the whole run over a single bad sample.
            return "0";
        }
        return Double.toString(value);
    }

    private static String quote(String raw) {
        StringBuilder out = new StringBuilder(raw.length() + 16).append('"');
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                default -> {
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        return out.append('"').toString();
    }

    public static Map<String, String> metadata() {
        return new LinkedHashMap<>();
    }

    @Override
    public void close() {
        synchronized (lock) {
            if (closed) {
                return;
            }
            closed = true;
            try {
                writer.flush();
                writer.close();
            } catch (IOException ignored) {
                // Nothing useful to do; the stream is already on disk up to the last flush.
            }
        }
    }
}
