package dev.mcbench.probe.core;

/**
 * Double-buffered primitive sample store for the measurement hot path.
 *
 * <p>{@link #record(long)} is called once per rendered frame — sixty times a second at a
 * minimum, and far more on a fast machine. Anything it does becomes part of what we measure,
 * so the constraints are strict: no allocation, no I/O, and no blocking on the writer thread.
 * A probe that perturbs the frame it is timing produces numbers about the probe.
 *
 * <p>The design is a pair of preallocated {@code long[]} buffers. Producers append to the
 * active one; the flusher swaps them under a lock held only for the duration of a reference
 * swap. The producer therefore never waits on file I/O, and the only contention is a briefly
 * held, essentially always-uncontended monitor.
 *
 * <p>Overflow is counted rather than silently dropped. Losing samples without saying so would
 * bias the distribution toward whatever was happening when the buffer had room, which is
 * exactly the sort of invisible error this project exists to eliminate.
 */
public final class SampleBuffer {

    private final int capacity;
    private long[] active;
    private long[] standby;
    private int size;
    private long overflowed;

    public SampleBuffer(int capacity) {
        if (capacity < 1) {
            throw new IllegalArgumentException("capacity must be positive, got " + capacity);
        }
        this.capacity = capacity;
        this.active = new long[capacity];
        this.standby = new long[capacity];
    }

    /**
     * Append one sample. Hot path: allocation-free and never blocks on I/O.
     */
    public synchronized void record(long value) {
        if (size < capacity) {
            active[size++] = value;
        } else {
            overflowed++;
        }
    }

    /**
     * Swap buffers and return the filled one, together with how many entries are valid.
     *
     * <p>The returned array is the internal buffer, not a copy — copying a 65k-element array
     * on every flush would be pure waste. It stays valid until the next drain, which is why
     * the caller must consume it before draining again.
     */
    public synchronized Drained drain() {
        long[] filled = active;
        int count = size;
        active = standby;
        standby = filled;
        size = 0;
        return new Drained(filled, count);
    }

    public synchronized int size() {
        return size;
    }

    /** Samples dropped because the buffer was full between flushes. */
    public synchronized long overflowed() {
        return overflowed;
    }

    public synchronized boolean hasOverflowed() {
        return overflowed > 0;
    }

    /** A drained buffer: {@code values[0..count)} are valid. */
    public record Drained(long[] values, int count) {
        public boolean isEmpty() {
            return count == 0;
        }
    }
}
