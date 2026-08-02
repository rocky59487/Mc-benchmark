package dev.mcbench.probe.core;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Ordered command supply for the adapter to execute on the game thread.
 *
 * <p>Setup commands drain first and must all complete before warmup begins — they build the
 * world the scenario describes, and that work is untimed by design. Workload commands only
 * become available once measurement starts, and they cycle, because a scenario's load has to be
 * sustainable for the whole window. A one-shot workload would decay to nothing partway through
 * and quietly turn a stress test into an idle measurement.
 */
public final class CommandQueue {

    private final List<String> setup;
    private final List<String> workload;
    private final AtomicInteger setupIndex = new AtomicInteger();
    private final AtomicInteger workloadIndex = new AtomicInteger();
    private volatile boolean workloadActive;

    public CommandQueue(List<String> setup, List<String> workload) {
        this.setup = List.copyOf(setup);
        this.workload = List.copyOf(workload);
    }

    /** Called when measurement begins; workload commands start being served. */
    public void enterWorkload() {
        workloadActive = true;
    }

    /**
     * @return the next command, or {@code null} when there is nothing to run right now
     */
    public String poll() {
        int index = setupIndex.get();
        if (index < setup.size()) {
            // compareAndSet rather than getAndIncrement so a racing second caller cannot skip a
            // setup command; setup must execute exactly once and in order.
            return setupIndex.compareAndSet(index, index + 1) ? setup.get(index) : null;
        }
        if (!workloadActive || workload.isEmpty()) {
            return null;
        }
        int next = workloadIndex.getAndIncrement();
        return workload.get(next % workload.size());
    }

    public boolean setupComplete() {
        return setupIndex.get() >= setup.size();
    }

    public int setupSize() {
        return setup.size();
    }

    public int workloadSize() {
        return workload.size();
    }
}
