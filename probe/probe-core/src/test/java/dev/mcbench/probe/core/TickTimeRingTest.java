package dev.mcbench.probe.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

/** Reading Paper's tick-time ring without trusting a tick number. */
class TickTimeRingTest {

    /** Stands in for the server: writes at {@code tickCount % length}, hands out copies. */
    private static final class Ring {
        private final long[] times;
        private int tickCount;

        Ring(int length) {
            this.times = new long[length];
        }

        void tick(long durationNanos) {
            times[tickCount % times.length] = durationNanos;
            tickCount++;
        }

        long[] snapshot() {
            return times.clone();
        }
    }

    private static List<Long> feed(TickTimeRing reader, Ring ring) {
        List<Long> seen = new ArrayList<>();
        reader.accept(ring.snapshot(), seen::add);
        return seen;
    }

    @Test
    void theFirstSnapshotIsOnlyAReference() {
        Ring ring = new Ring(100);
        ring.tick(4_000_000);
        TickTimeRing reader = new TickTimeRing();
        assertTrue(feed(reader, ring).isEmpty(), "the ring's history is not this run's samples");
        assertFalse(reader.calibrated());
    }

    @Test
    void readsEachTickOnce() {
        Ring ring = new Ring(100);
        TickTimeRing reader = new TickTimeRing();
        feed(reader, ring);

        List<Long> seen = new ArrayList<>();
        for (int i = 1; i <= 250; i++) {
            ring.tick(i * 1_000L);
            reader.accept(ring.snapshot(), seen::add);
        }

        assertEquals(250, seen.size());
        for (int i = 0; i < seen.size(); i++) {
            assertEquals((i + 1) * 1_000L, seen.get(i), "durations must arrive in tick order");
        }
    }

    @Test
    void aStartOffsetInTheRingIsIrrelevant() {
        // The server has been up for a while: the write position bears no relation to anything
        // the probe can see, which is exactly what indexing by a tick number got wrong.
        Ring ring = new Ring(100);
        for (int i = 0; i < 1_337; i++) {
            ring.tick(50_000 + i);
        }
        TickTimeRing reader = new TickTimeRing();
        feed(reader, ring);

        ring.tick(9_999_999);
        assertEquals(List.of(9_999_999L), feed(reader, ring));
    }

    @Test
    void ticksMissedBetweenSamplesAreStillRecorded() {
        // A lagging server is when tick durations matter most; dropping the samples taken
        // during the stall would cut exactly the tail being measured.
        Ring ring = new Ring(100);
        TickTimeRing reader = new TickTimeRing();
        feed(reader, ring);
        ring.tick(1_000);
        feed(reader, ring);

        ring.tick(80_000_000);
        ring.tick(70_000_000);
        ring.tick(60_000_000);
        assertEquals(List.of(80_000_000L, 70_000_000L, 60_000_000L), feed(reader, ring));
    }

    @Test
    void aSecondReadWithinOneTickAddsNothing() {
        Ring ring = new Ring(100);
        TickTimeRing reader = new TickTimeRing();
        feed(reader, ring);
        ring.tick(3_000_000);

        assertEquals(List.of(3_000_000L), feed(reader, ring));
        assertTrue(feed(reader, ring).isEmpty(), "no write, no sample");
    }

    @Test
    void aWholeRingReplacedAtOnceIsNotAttributed() {
        // More writes than the ring holds: which entry is newest is unknowable, and inventing
        // an order would put a hundred durations in the wrong sequence.
        Ring ring = new Ring(16);
        TickTimeRing reader = new TickTimeRing();
        feed(reader, ring);
        ring.tick(1_000);
        feed(reader, ring);

        for (int i = 0; i < 40; i++) {
            ring.tick(2_000 + i);
        }
        assertTrue(feed(reader, ring).isEmpty());

        // and it resynchronises on the next tick
        ring.tick(7_777);
        assertEquals(List.of(7_777L), feed(reader, ring));
    }

    @Test
    void aResizedRingResynchronises() {
        TickTimeRing reader = new TickTimeRing();
        Ring small = new Ring(20);
        small.tick(1_000);
        feed(reader, small);
        small.tick(2_000);
        assertEquals(List.of(2_000L), feed(reader, small));

        Ring large = new Ring(100);
        large.tick(3_000);
        assertTrue(feed(reader, large).isEmpty(), "a different ring is a new reference");
        large.tick(4_000);
        assertEquals(List.of(4_000L), feed(reader, large));
    }

    @Test
    void unwrittenSlotsAreNotSamples() {
        // A freshly started server's ring is mostly zeros; a zero-nanosecond tick is not one.
        Ring ring = new Ring(4);
        for (long duration : new long[] {5_000, 6_000, 7_000, 8_000}) {
            ring.tick(duration);
        }
        TickTimeRing reader = new TickTimeRing();
        feed(reader, ring);

        ring.tick(0);
        assertTrue(feed(reader, ring).isEmpty());
        ring.tick(9_000);
        assertEquals(List.of(9_000L), feed(reader, ring), "and the position is still right");
    }

    @Test
    void anEmptyOrAbsentRingIsIgnored() {
        TickTimeRing reader = new TickTimeRing();
        assertEquals(0, reader.accept(null, v -> {}));
        assertEquals(0, reader.accept(new long[0], v -> {}));
    }
}
