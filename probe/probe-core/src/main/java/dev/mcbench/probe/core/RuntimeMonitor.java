package dev.mcbench.probe.core;

import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedQueue;
import javax.management.Notification;
import javax.management.NotificationEmitter;
import javax.management.NotificationListener;
import javax.management.openmbean.CompositeData;

/**
 * Garbage collection and allocation measurement.
 *
 * <p>Two signals used to be published under names they did not have.
 *
 * <p><b>Allocation.</b> It was estimated from positive deltas in heap-used between samples.
 * That is heap <em>growth</em>, not allocation: an object allocated and collected between two
 * samples never appears, and any interval containing a collection was skipped outright. A mod
 * that allocates heavily but lets everything die young — the common case, and precisely the
 * behaviour that costs GC time — measured as allocating nothing. Where the JVM exposes a real
 * allocation counter it is used; where it does not, the number is published as
 * {@code heap_growth_rate_mb_s}, under its own name, rather than as an allocation rate.
 *
 * <p><b>GC pauses.</b> They were emitted as one aggregate delta per sampling interval, so
 * {@code gc_pause_p99} was a percentile over interval totals rather than over pauses. Three
 * short collections in one interval looked like one long pause, and one long pause split across
 * a sample boundary looked like two short ones. Individual collection events are now captured
 * from the JVM's own GC notifications, with the collector name, the duration, and heap usage
 * before and after — which also gives a live-set reading taken <em>at</em> the collection
 * rather than at whatever point the next sample happened to fall.
 *
 * <p>Both improvements degrade rather than fail. Notifications and the allocation counter live
 * in {@code com.sun.management}, which is present on HotSpot and OpenJ9 but is not guaranteed;
 * they are reached reflectively, and the aggregate counters remain as a fallback that is
 * reported honestly for what it is.
 */
public final class RuntimeMonitor {

    /**
     * Ceiling on retained GC events. A long run on a small heap can produce tens of thousands
     * of collections, and events are drained on the flush cadence — this only bounds what can
     * accumulate if a run stops flushing.
     */
    private static final int MAX_PENDING_EVENTS = 100_000;

    private final List<GarbageCollectorMXBean> collectors;
    private final MemoryMXBean memory;
    private final ConcurrentLinkedQueue<GcEvent> pending = new ConcurrentLinkedQueue<>();
    private final List<Runnable> unsubscribe = new ArrayList<>();

    private final Method threadAllocationCounter;
    private final Object threadBean;

    private long lastGcCount;
    private long lastGcTimeMs;
    private long lastHeapUsed;
    private long baselineAllocated;
    private long heapGrowthBytes;
    private volatile boolean listening;
    private volatile boolean collecting;
    private boolean primed;

    public RuntimeMonitor() {
        this.collectors = new ArrayList<>(ManagementFactory.getGarbageCollectorMXBeans());
        this.memory = ManagementFactory.getMemoryMXBean();

        Method counter = null;
        Object bean = null;
        try {
            Class<?> sunThreadBean = Class.forName("com.sun.management.ThreadMXBean");
            Object candidate = ManagementFactory.getThreadMXBean();
            if (sunThreadBean.isInstance(candidate)) {
                Method supported = sunThreadBean.getMethod("isThreadAllocatedMemorySupported");
                Method enable = sunThreadBean.getMethod(
                        "setThreadAllocatedMemoryEnabled", boolean.class);
                if (Boolean.TRUE.equals(supported.invoke(candidate))) {
                    enable.invoke(candidate, true);
                    // The whole-JVM total, not a per-thread figure. Minecraft hands a great
                    // deal of work to worker pools — chunk generation above all, which is
                    // where allocation differences between mods actually show up — and a
                    // server-thread-only counter would miss it entirely.
                    counter = sunThreadBean.getMethod("getTotalThreadAllocatedBytes");
                    bean = candidate;
                }
            }
        } catch (ReflectiveOperationException | RuntimeException ignored) {
            // No allocation counter on this JVM. Heap growth is reported instead, under its
            // own metric name.
        }
        this.threadAllocationCounter = counter;
        this.threadBean = bean;
    }

    /**
     * Whether a real allocation counter is available.
     *
     * <p>The harness needs this to decide which metric name the numbers may be published under,
     * so it travels in the stream rather than staying an implementation detail.
     */
    public boolean hasAllocationCounter() {
        return threadAllocationCounter != null;
    }

    /** Whether individual collection events are being captured. */
    public boolean hasGcEvents() {
        return listening;
    }

    // -- lifecycle -------------------------------------------------------

    /**
     * Subscribe to the JVM's GC notifications.
     *
     * <p>The listener runs on the JVM's notification thread — never on the game thread — and
     * does nothing but enqueue a record, so it cannot lengthen the pause it reports.
     */
    public void startListening() {
        if (listening) {
            return;
        }
        Class<?> infoClass;
        Method from;
        String notificationType;
        try {
            infoClass = Class.forName("com.sun.management.GarbageCollectionNotificationInfo");
            from = infoClass.getMethod("from", CompositeData.class);
            notificationType = (String) infoClass
                    .getField("GARBAGE_COLLECTION_NOTIFICATION").get(null);
        } catch (ReflectiveOperationException | RuntimeException e) {
            // Aggregate counters remain, and the caller reports which source it has.
            return;
        }

        for (GarbageCollectorMXBean collector : collectors) {
            if (!(collector instanceof NotificationEmitter emitter)) {
                continue;
            }
            NotificationListener listener = (notification, handback) -> {
                if (notificationType.equals(notification.getType())) {
                    record(notification, infoClass, from);
                }
            };
            try {
                emitter.addNotificationListener(listener, null, null);
                unsubscribe.add(() -> {
                    try {
                        emitter.removeNotificationListener(listener);
                    } catch (Exception ignored) {
                        // Already removed, or the bean is gone.
                    }
                });
                listening = true;
            } catch (Exception ignored) {
                // A collector that refuses listeners contributes no events.
            }
        }
    }

    /** Unsubscribe. Safe to call more than once. */
    public void stopListening() {
        for (Runnable action : unsubscribe) {
            action.run();
        }
        unsubscribe.clear();
        listening = false;
        collecting = false;
    }

    /** Begin retaining events. Called when the measurement window opens. */
    public void beginCollecting() {
        pending.clear();
        collecting = true;
    }

    private void record(Notification notification, Class<?> infoClass, Method from) {
        if (!collecting || pending.size() >= MAX_PENDING_EVENTS) {
            return;
        }
        try {
            Object info = from.invoke(null, notification.getUserData());
            String action = (String) infoClass.getMethod("getGcAction").invoke(info);
            String name = (String) infoClass.getMethod("getGcName").invoke(info);
            Object gcInfo = infoClass.getMethod("getGcInfo").invoke(info);
            Class<?> gcInfoClass = gcInfo.getClass();
            long duration = (Long) gcInfoClass.getMethod("getDuration").invoke(gcInfo);
            long startTime = (Long) gcInfoClass.getMethod("getStartTime").invoke(gcInfo);

            @SuppressWarnings("unchecked")
            Map<String, MemoryUsage> before = (Map<String, MemoryUsage>) gcInfoClass
                    .getMethod("getMemoryUsageBeforeGc").invoke(gcInfo);
            @SuppressWarnings("unchecked")
            Map<String, MemoryUsage> after = (Map<String, MemoryUsage>) gcInfoClass
                    .getMethod("getMemoryUsageAfterGc").invoke(gcInfo);

            pending.add(new GcEvent(
                    name == null ? "unknown" : name,
                    action == null ? "" : action,
                    startTime,
                    duration,
                    heapOf(before),
                    heapOf(after),
                    stopsTheWorld(name, action)));
        } catch (ReflectiveOperationException | RuntimeException ignored) {
            // A notification we cannot decode is dropped rather than guessed at.
        }
    }

    /**
     * Whether a reported duration is time an application thread actually waited.
     *
     * <p>A concurrent collector's cycle "duration" spans work that ran alongside the
     * application. Counting it as a pause would make a low-pause collector look like the worst
     * one on the machine, and would put a hundreds-of-milliseconds figure into a percentile
     * that is meant to describe hitching.
     */
    private static boolean stopsTheWorld(String name, String action) {
        String lowered = (name == null ? "" : name).toLowerCase();
        if (lowered.contains("concurrent")) {
            return false;
        }
        String performed = (action == null ? "" : action).toLowerCase();
        return performed.contains("minor gc") || performed.contains("major gc")
                || performed.isEmpty();
    }

    private static double heapOf(Map<String, MemoryUsage> usage) {
        if (usage == null) {
            return 0.0;
        }
        long total = 0;
        for (Map.Entry<String, MemoryUsage> entry : usage.entrySet()) {
            String pool = entry.getKey().toLowerCase();
            // Non-heap pools (Metaspace, code cache) are reported alongside heap pools and are
            // not part of the live set an operator sizes a heap against.
            if (pool.contains("metaspace") || pool.contains("code") || pool.contains("class")) {
                continue;
            }
            total += entry.getValue().getUsed();
        }
        return total / (1024.0 * 1024.0);
    }

    /** Drain the collection events observed since the last drain. */
    public List<GcEvent> drainEvents() {
        if (pending.isEmpty()) {
            return Collections.emptyList();
        }
        List<GcEvent> drained = new ArrayList<>();
        GcEvent event;
        while ((event = pending.poll()) != null) {
            drained.add(event);
        }
        return drained;
    }

    // -- sampling --------------------------------------------------------

    /** Reset counters so measurement excludes everything that happened during warmup. */
    public void resetBaseline() {
        lastGcCount = totalGcCount();
        lastGcTimeMs = totalGcTimeMs();
        lastHeapUsed = heapUsedBytes();
        baselineAllocated = allocatedBytesRaw();
        heapGrowthBytes = 0;
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
                heapGrowthBytes += delta;
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
                allocatedBytes(),
                heapGrowthBytes,
                hasAllocationCounter());
    }

    public double heapUsedMb() {
        return heapUsedBytes() / (1024.0 * 1024.0);
    }

    /**
     * Bytes allocated since {@link #resetBaseline()}, or the heap-growth estimate when no
     * allocation counter exists.
     *
     * <p>Which of the two it is travels on the sample, so the harness names the metric
     * correctly instead of publishing heap growth as allocation.
     */
    public long allocatedBytes() {
        if (threadAllocationCounter == null) {
            return heapGrowthBytes;
        }
        long now = allocatedBytesRaw();
        return now > baselineAllocated ? now - baselineAllocated : 0;
    }

    private long allocatedBytesRaw() {
        if (threadAllocationCounter == null) {
            return 0;
        }
        try {
            return (Long) threadAllocationCounter.invoke(threadBean);
        } catch (ReflectiveOperationException | RuntimeException e) {
            return 0;
        }
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
     * One collection, as the JVM reported it.
     *
     * @param collector      the collector's name, e.g. "G1 Young Generation"
     * @param action         what it did, e.g. "end of minor GC"
     * @param startMillis    JVM-relative start time
     * @param durationMs     how long the collection took
     * @param heapBeforeMb   heap in use immediately before
     * @param heapAfterMb    heap in use immediately after — the live set, measured at the
     *                       collection rather than at the next sampling interval
     * @param stopsTheWorld  whether this duration is a pause an application thread waited out
     */
    public record GcEvent(
            String collector,
            String action,
            long startMillis,
            long durationMs,
            double heapBeforeMb,
            double heapAfterMb,
            boolean stopsTheWorld) {}

    /**
     * @param heapMb            heap in use at sample time
     * @param collected         whether a collection occurred since the last sample
     * @param collections       how many
     * @param pauseMs           total pause time added since the last sample
     * @param allocatedBytes    allocation since {@link #resetBaseline()}, or heap growth
     * @param heapGrowthBytes   heap growth since {@link #resetBaseline()}, always
     * @param realAllocation    whether {@code allocatedBytes} came from an allocation counter
     */
    public record Sample(
            double heapMb,
            boolean collected,
            long collections,
            long pauseMs,
            long allocatedBytes,
            long heapGrowthBytes,
            boolean realAllocation) {}
}
