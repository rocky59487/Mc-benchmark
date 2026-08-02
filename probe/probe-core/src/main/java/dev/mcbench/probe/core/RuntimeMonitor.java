package dev.mcbench.probe.core;

import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.util.ArrayList;
import java.util.List;

/**
 * Garbage collection and heap sampling via JMX.
 *
 * <p>Uses only {@code java.lang.management}, so it works on every JVM, every Minecraft version,
 * and every mod loader without touching a single game class.
 *
 * <p>GC pauses are derived from cumulative collector counters rather than notification
 * listeners. Notifications give per-event detail but require {@code com.sun.management}
 * internals that are not guaranteed to be present, and registering listeners inside a running
 * game adds callback work on GC completion. Differencing counters costs two field reads.
 *
 * <p>Allocation rate is tracked separately from pause time because they fail differently: a mod
 * that doubles allocation without lengthening measured pauses has not become free, it has moved
 * the cost onto whoever runs a smaller heap or a different collector.
 */
public final class RuntimeMonitor {

    private final List<GarbageCollectorMXBean> collectors;
    private final MemoryMXBean memory;

    private long lastGcCount;
    private long lastGcTimeMs;
    private long lastHeapUsed;
    private long allocatedBytes;
    private boolean primed;

    public RuntimeMonitor() {
        this.collectors = new ArrayList<>(ManagementFactory.getGarbageCollectorMXBeans());
        this.memory = ManagementFactory.getMemoryMXBean();
    }

    /** Reset counters so measurement excludes everything that happened during warmup. */
    public void resetBaseline() {
        lastGcCount = totalGcCount();
        lastGcTimeMs = totalGcTimeMs();
        lastHeapUsed = heapUsedBytes();
        allocatedBytes = 0;
        primed = true;
    }

    /**
     * Sample the runtime.
     *
     * @return a snapshot describing what changed since the previous sample
     */
    public Sample sample() {
        long gcCount = totalGcCount();
        long gcTimeMs = totalGcTimeMs();
        long heapUsed = heapUsedBytes();

        long newCollections = gcCount - lastGcCount;
        long newPauseMs = gcTimeMs - lastGcTimeMs;

        if (primed) {
            long delta = heapUsed - lastHeapUsed;
            if (delta > 0) {
                // Heap growth between samples approximates allocation. When a collection
                // intervenes the heap shrinks and the delta goes negative; that interval's
                // allocation is unrecoverable from this signal, so it is skipped rather than
                // guessed at. Sampling well above GC frequency keeps the omission small, and
                // undercounting is the safe direction — it never invents allocation a mod did
                // not perform.
                allocatedBytes += delta;
            }
        }

        lastGcCount = gcCount;
        lastGcTimeMs = gcTimeMs;
        lastHeapUsed = heapUsed;
        primed = true;

        return new Sample(
                heapUsed / (1024.0 * 1024.0),
                newCollections > 0,
                newCollections,
                newPauseMs,
                allocatedBytes);
    }

    public double heapUsedMb() {
        return heapUsedBytes() / (1024.0 * 1024.0);
    }

    public long allocatedBytes() {
        return allocatedBytes;
    }

    /**
     * Heap in use, in bytes.
     *
     * <p>{@link Runtime} is the primary source and {@link MemoryMXBean} only a cross-check,
     * which is the opposite of the obvious choice. The MXBean is the more precise API on paper,
     * but it has been observed reporting {@code used == 0} on a perfectly healthy G1 JVM that
     * had not yet run a collection — while {@code Runtime} reported the correct few megabytes.
     *
     * <p>That failure mode is the dangerous kind: not a crash, just silently zero heap and
     * allocation metrics for the whole run. {@code totalMemory() - freeMemory()} is heap-only,
     * available on every JVM ever shipped, and cannot report zero for a running program.
     *
     * <p>The larger of the two is taken so that neither source reporting low can cost us a
     * real measurement.
     */
    private long heapUsedBytes() {
        Runtime runtime = Runtime.getRuntime();
        long fromRuntime = runtime.totalMemory() - runtime.freeMemory();
        long fromBean = 0;
        try {
            MemoryUsage usage = memory.getHeapMemoryUsage();
            fromBean = usage.getUsed();
        } catch (RuntimeException ignored) {
            // Some restricted or embedded JVMs refuse the call outright.
        }
        return Math.max(fromRuntime, fromBean);
    }

    private long totalGcCount() {
        long total = 0;
        for (GarbageCollectorMXBean bean : collectors) {
            long count = bean.getCollectionCount();
            if (count > 0) {
                total += count;
            }
        }
        return total;
    }

    private long totalGcTimeMs() {
        long total = 0;
        for (GarbageCollectorMXBean bean : collectors) {
            long time = bean.getCollectionTime();
            if (time > 0) {
                total += time;
            }
        }
        return total;
    }

    /**
     * @param heapMb           heap in use at sample time
     * @param collected        whether a collection occurred since the last sample
     * @param collections      how many
     * @param pauseMs          total pause time added since the last sample
     * @param allocatedBytes   cumulative allocation since {@link #resetBaseline()}
     */
    public record Sample(
            double heapMb,
            boolean collected,
            long collections,
            long pauseMs,
            long allocatedBytes) {}
}
