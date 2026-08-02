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
 *
 * <p><b>Pacing.</b> An entry of the form {@code @wait N} consumes N polls without issuing a
 * command. Because adapters poll once per server tick, and server ticks run at a fixed 20 Hz
 * for scenarios that are not tick-warped, this makes a workload's pacing a function of game
 * time rather than of how fast the machine renders.
 *
 * <p>That distinction matters more than it looks. If a scripted camera path advanced one step
 * per rendered frame, a faster machine would fly it faster — so the fast configuration would be
 * doing a different amount of work than the slow one, and the comparison would be measuring two
 * different workloads. Pacing on ticks keeps the workload identical across variants, which is
 * the entire premise of the benchmark.
 */
public final class CommandQueue {

    /** Prefix marking a pacing directive rather than a command. */
    public static final String WAIT_PREFIX = "@wait";

    private final List<String> setup;
    private final List<String> workload;
    private final AtomicInteger setupIndex = new AtomicInteger();
    private final AtomicInteger workloadIndex = new AtomicInteger();
    private final AtomicInteger waitRemaining = new AtomicInteger();
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
     * @return the next command, or {@code null} when there is nothing to run right now —
     *     because setup is done and measurement has not begun, because the workload is empty,
     *     or because a pacing directive is still counting down
     */
    public String poll() {
        if (waitRemaining.get() > 0) {
            waitRemaining.decrementAndGet();
            return null;
        }

        int index = setupIndex.get();
        if (index < setup.size()) {
            // compareAndSet rather than getAndIncrement so a racing second caller cannot skip a
            // setup command; setup must execute exactly once and in order.
            if (!setupIndex.compareAndSet(index, index + 1)) {
                return null;
            }
            return interpret(setup.get(index));
        }

        if (!workloadActive || workload.isEmpty()) {
            return null;
        }
        int next = workloadIndex.getAndIncrement();
        return interpret(workload.get(next % workload.size()));
    }

    /**
     * @return the command to run, or {@code null} if the entry was a pacing directive
     */
    private String interpret(String entry) {
        if (!entry.startsWith(WAIT_PREFIX)) {
            return entry;
        }
        String argument = entry.substring(WAIT_PREFIX.length()).trim();
        try {
            int ticks = Integer.parseInt(argument);
            if (ticks > 0) {
                // This poll is itself the first tick of the wait.
                waitRemaining.set(ticks - 1);
            }
        } catch (NumberFormatException e) {
            // A malformed directive is a scenario defect, but dropping the whole run over it
            // would be worse than pacing slightly differently. Treated as a single-tick pause.
        }
        return null;
    }

    /**
     * Whether every setup entry has been consumed.
     *
     * <p>Note this becomes true once the last entry is taken, including a trailing wait — the
     * adapter keeps polling, so a pending wait still elapses before warmup work begins.
     */
    public boolean setupComplete() {
        return setupIndex.get() >= setup.size() && waitRemaining.get() == 0;
    }

    public int setupSize() {
        return setup.size();
    }

    public int workloadSize() {
        return workload.size();
    }
}
