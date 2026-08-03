package dev.mcbench.probe.core;

import java.util.function.LongConsumer;

/**
 * Reads new entries out of a platform's ring buffer of tick durations.
 *
 * <p>Paper keeps the last hundred tick durations in an array indexed by the server's tick
 * counter. The obvious way to read the freshest entry is to index it by a tick number, and on
 * Paper that is wrong: {@code Bukkit.getCurrentTick()} returns {@code MinecraftServer.currentTick},
 * which the tick loop derives from wall-clock nanoseconds, while the ring is written at
 * {@code tickCount % length}. The two counters share no origin and drift apart whenever the
 * server misses its budget, so the computed slot is arbitrary — and an arbitrary slot still
 * holds a real duration from some recent tick, which is why the mistake produces plausible
 * numbers rather than an error.
 *
 * <p>So no tick number is used. Consecutive snapshots are diffed and whatever changed is what
 * was just written. That needs no knowledge of how the platform indexes its ring, survives a
 * counter that resets, and re-establishes itself after a gap.
 */
public final class TickTimeRing {

    private long[] previous;
    private int newest = -1;

    /**
     * Feed every duration written since the previous snapshot, oldest first.
     *
     * @param times a snapshot of the platform's ring; retained by value, never aliased
     * @param sink  receives each newly written duration
     * @return how many durations were fed
     */
    public int accept(long[] times, LongConsumer sink) {
        if (times == null || times.length == 0) {
            return 0;
        }
        if (previous == null || previous.length != times.length) {
            // Nothing to diff against. The first snapshot is a reference point only: its
            // entries are ticks that finished before this reader started looking.
            previous = times.clone();
            newest = -1;
            return 0;
        }

        int changes = countChanges(times);
        int start = -1;
        int count = 0;
        if (changes == 1) {
            // The ordinary case, and also how position is recovered after a gap.
            start = firstChange(times);
            count = 1;
        } else if (changes > 1) {
            // Several ticks passed between snapshots. They are attributable only as an
            // unbroken run starting where the next write was expected; anything else and the
            // position is lost, so the next snapshot re-establishes it rather than guessing.
            int expected = newest < 0 ? -1 : (newest + 1) % times.length;
            if (expected >= 0 && changes < times.length && isRun(times, expected, changes)) {
                start = expected;
                count = changes;
            } else {
                newest = -1;
            }
        }
        // Zero changes is a tick that produced no write, or a duration that repeated to the
        // nanosecond. Neither is a sample: re-recording a value already reported is worse than
        // missing one.
        previous = times.clone();

        int fed = 0;
        for (int k = 0; k < count; k++) {
            int index = (start + k) % times.length;
            newest = index;
            long duration = times[index];
            if (duration > 0) {
                sink.accept(duration);
                fed++;
            }
        }
        return fed;
    }

    private int countChanges(long[] times) {
        int changes = 0;
        for (int i = 0; i < times.length; i++) {
            if (times[i] != previous[i]) {
                changes++;
            }
        }
        return changes;
    }

    private int firstChange(long[] times) {
        for (int i = 0; i < times.length; i++) {
            if (times[i] != previous[i]) {
                return i;
            }
        }
        return 0;
    }

    /** Whether {@code length} slots from {@code start} all changed. */
    private boolean isRun(long[] times, int start, int length) {
        for (int k = 0; k < length; k++) {
            int index = (start + k) % times.length;
            if (times[index] == previous[index]) {
                return false;
            }
        }
        return true;
    }

    /** Whether a position in the ring is known, so the next write can be predicted. */
    public boolean calibrated() {
        return newest >= 0;
    }
}
