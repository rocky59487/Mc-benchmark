package dev.mcbench.probe.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.StringWriter;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

class ProbeCoreTest {

    @Nested
    class SampleBufferTest {

        @Test
        void recordsAndDrains() {
            SampleBuffer buffer = new SampleBuffer(8);
            buffer.record(10);
            buffer.record(20);
            SampleBuffer.Drained drained = buffer.drain();
            assertEquals(2, drained.count());
            assertEquals(10, drained.values()[0]);
            assertEquals(20, drained.values()[1]);
        }

        @Test
        void drainResetsSizeButKeepsCapacity() {
            SampleBuffer buffer = new SampleBuffer(4);
            buffer.record(1);
            buffer.drain();
            assertEquals(0, buffer.size());
            buffer.record(2);
            assertEquals(1, buffer.size());
        }

        @Test
        void swapsBuffersSoTheProducerNeverWaitsOnIo() {
            // The drained array must not be the one subsequent writes land in, otherwise the
            // consumer would be reading memory the render thread is concurrently overwriting.
            SampleBuffer buffer = new SampleBuffer(4);
            buffer.record(1);
            SampleBuffer.Drained first = buffer.drain();
            buffer.record(99);
            assertEquals(1, first.values()[0]);
        }

        @Test
        void countsOverflowRatherThanSilentlyDropping() {
            // Dropping samples without saying so biases the distribution toward whatever was
            // happening while the buffer still had room.
            SampleBuffer buffer = new SampleBuffer(2);
            buffer.record(1);
            buffer.record(2);
            buffer.record(3);
            buffer.record(4);
            assertTrue(buffer.hasOverflowed());
            assertEquals(2, buffer.overflowed());
        }

        @Test
        void rejectsNonPositiveCapacity() {
            assertThrows(IllegalArgumentException.class, () -> new SampleBuffer(0));
        }
    }

    @Nested
    class SteadyStateTest {

        @Test
        void needsTwoFullWindowsBeforeJudging() {
            SteadyState steady = new SteadyState(4, 0.05);
            for (int i = 0; i < 7; i++) {
                steady.accept(100);
                assertFalse(steady.hasEnoughSamples());
            }
            steady.accept(100);
            assertTrue(steady.hasEnoughSamples());
        }

        @Test
        void flatSeriesIsSteady() {
            SteadyState steady = new SteadyState(4, 0.05);
            for (int i = 0; i < 8; i++) {
                steady.accept(100);
            }
            assertTrue(steady.isSteady());
        }

        @Test
        void risingSeriesIsNotSteady() {
            SteadyState steady = new SteadyState(4, 0.01);
            long value = 100;
            for (int i = 0; i < 8; i++) {
                steady.accept(value);
                value *= 2;
            }
            assertFalse(steady.isSteady());
        }

        @Test
        void settlesAfterAWarmupRamp() {
            SteadyState steady = new SteadyState(4, 0.05);
            for (long v : new long[] {900, 700, 500, 300}) {
                steady.accept(v);
            }
            for (int i = 0; i < 4; i++) {
                steady.accept(100);
            }
            assertFalse(steady.isSteady());
            for (int i = 0; i < 4; i++) {
                steady.accept(100);
            }
            assertTrue(steady.isSteady());
        }

        @Test
        void medianResistsASingleCompilationSpike() {
            // A 500ms JIT pause must not stop warmup from being recognised as settled; a mean
            // would be dragged around by it long after typical frames stabilised.
            SteadyState steady = new SteadyState(5, 0.05);
            for (int i = 0; i < 5; i++) {
                steady.accept(100);
            }
            steady.accept(500_000_000L);
            for (int i = 0; i < 4; i++) {
                steady.accept(100);
            }
            assertTrue(steady.isSteady());
        }

        @Test
        void rejectsDegenerateParameters() {
            assertThrows(IllegalArgumentException.class, () -> new SteadyState(1, 0.05));
            assertThrows(IllegalArgumentException.class, () -> new SteadyState(4, 0));
            assertThrows(IllegalArgumentException.class, () -> new SteadyState(4, 1.5));
        }
    }

    @Nested
    class PhaseControllerTest {

        private PhaseController controller(double min, double multiple, double duration) {
            return new PhaseController(min, multiple, duration, 4, 0.05);
        }

        @Test
        void startsInProvision() {
            assertEquals(Phase.PROVISION, controller(10, 3, 20).phase());
        }

        @Test
        void provisionSamplesAreIgnored() {
            PhaseController c = controller(10, 3, 20);
            assertFalse(c.advance(100, 1000));
            assertEquals(Phase.PROVISION, c.phase());
        }

        @Test
        void requiresBothMinimumDurationAndSteadyState() {
            PhaseController c = controller(10, 100, 20);
            c.beginWarmup();
            // Perfectly steady from the first sample, but the minimum has not elapsed.
            for (int i = 0; i < 9; i++) {
                c.advance(1, 100);
            }
            assertEquals(Phase.WARMUP, c.phase());
            c.advance(1, 100);
            assertEquals(Phase.MEASUREMENT, c.phase());
            assertTrue(c.converged());
        }

        @Test
        void doesNotLeaveWarmupWhileStillTrending() {
            PhaseController c = controller(5, 100, 20);
            c.beginWarmup();
            long value = 100;
            for (int i = 0; i < 40; i++) {
                c.advance(1, value);
                value = (long) (value * 1.5);
            }
            assertEquals(Phase.WARMUP, c.phase());
        }

        @Test
        void ceilingEndsWarmupButMarksItUnconverged() {
            PhaseController c = controller(5, 2, 20);
            c.beginWarmup();
            long value = 100;
            for (int i = 0; i < 20; i++) {
                c.advance(1, value);
                value = (long) (value * 1.5);
            }
            assertEquals(Phase.MEASUREMENT, c.phase());
            assertFalse(c.converged(), "unconverged warmup must be reported, not hidden");
        }

        @Test
        void completesAfterTheMeasurementWindow() {
            PhaseController c = controller(4, 100, 10);
            c.beginWarmup();
            for (int i = 0; i < 8; i++) {
                c.advance(1, 100);
            }
            assertEquals(Phase.MEASUREMENT, c.phase());
            assertFalse(c.isComplete());
            for (int i = 0; i < 10; i++) {
                c.advance(1, 100);
            }
            assertTrue(c.isComplete());
        }

        @Test
        void measurementProgressExcludesWarmup() {
            PhaseController c = controller(4, 100, 10);
            c.beginWarmup();
            for (int i = 0; i < 8; i++) {
                c.advance(1, 100);
            }
            double atStart = c.measurementProgress();
            c.advance(3, 100);
            assertEquals(3.0, c.measurementProgress() - atStart, 1e-9);
        }

        @Test
        void rejectsDegenerateParameters() {
            assertThrows(IllegalArgumentException.class, () -> controller(0, 3, 10));
            assertThrows(IllegalArgumentException.class, () -> controller(10, 0.5, 10));
            assertThrows(IllegalArgumentException.class, () -> controller(10, 3, 0));
        }
    }

    @Nested
    class CommandQueueTest {

        @Test
        void servesSetupBeforeWorkload() {
            CommandQueue q = new CommandQueue(List.of("a", "b"), List.of("w"));
            assertEquals("a", q.poll());
            assertEquals("b", q.poll());
            assertTrue(q.setupComplete());
        }

        @Test
        void withholdsWorkloadUntilMeasurementBegins() {
            CommandQueue q = new CommandQueue(List.of(), List.of("w"));
            assertNull(q.poll());
            q.enterWorkload();
            assertEquals("w", q.poll());
        }

        @Test
        void cyclesWorkloadSoLoadIsSustained() {
            // A one-shot workload would decay to nothing partway through the window and
            // quietly turn a stress test into an idle measurement.
            CommandQueue q = new CommandQueue(List.of(), List.of("x", "y"));
            q.enterWorkload();
            assertEquals("x", q.poll());
            assertEquals("y", q.poll());
            assertEquals("x", q.poll());
            assertEquals("y", q.poll());
        }

        @Test
        void emptyWorkloadYieldsNothing() {
            CommandQueue q = new CommandQueue(List.of(), List.of());
            q.enterWorkload();
            assertNull(q.poll());
        }
    }

    @Nested
    class ProbeWriterTest {

        private ProbeConfig config() {
            Properties p = new Properties();
            p.setProperty("scenario.id", "test");
            p.setProperty("scenario.side", "client");
            p.setProperty("warmup.min", "2");
            p.setProperty("warmup.steady_state_window", "4");
            p.setProperty("measurement.duration", "5");
            return ProbeConfig.of(p, List.of(), List.of(), Path.of("."));
        }

        @Test
        void emitsOneJsonObjectPerLine() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out)) {
                writer.hello("0.1.0", Map.of("loader", "fabric"));
                writer.phase(Phase.MEASUREMENT);
                writer.frames(new long[] {1, 2, 3}, 3);
                writer.bye(1.5, false);
            }
            String[] lines = out.toString().strip().split("\n");
            assertEquals(4, lines.length);
            for (String line : lines) {
                assertTrue(line.startsWith("{") && line.endsWith("}"), line);
            }
        }

        @Test
        void writesProtocolVersionSoMismatchesAreDetectable() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out)) {
                writer.hello("0.1.0", Map.of());
            }
            assertTrue(out.toString().contains("\"protocol\":" + ProbeWriter.PROTOCOL_VERSION));
        }

        @Test
        void emitsOnlyTheValidPrefixOfABatch() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out)) {
                writer.frames(new long[] {7, 8, 999, 999}, 2);
            }
            assertTrue(out.toString().contains("[7,8]"), out.toString());
        }

        @Test
        void skipsEmptyBatchesEntirely() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out)) {
                writer.frames(new long[] {1}, 0);
                writer.ticks(new long[] {1}, 0);
            }
            assertEquals("", out.toString());
        }

        @Test
        void escapesStringsSoOutputStaysParseable() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out)) {
                writer.error("he said \"hi\"\nand\tleft\\");
            }
            String line = out.toString().strip();
            assertTrue(line.contains("\\\""));
            assertTrue(line.contains("\\n"));
            assertTrue(line.contains("\\t"));
            assertTrue(line.contains("\\\\"));
            assertFalse(line.contains("\n"), "raw newline would split one event into two lines");
        }

        @Test
        void replacesNonFiniteNumbersRatherThanEmittingInvalidJson() {
            // JSON has no NaN or Infinity; emitting one would make the whole run unparseable.
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out)) {
                writer.memory(Double.NaN, false, 0);
                writer.bye(Double.POSITIVE_INFINITY, false);
            }
            String text = out.toString();
            assertFalse(text.contains("NaN"));
            assertFalse(text.contains("Infinity"));
        }

        @Test
        void ignoresWritesAfterClose() {
            StringWriter out = new StringWriter();
            ProbeWriter writer = new ProbeWriter(out);
            writer.close();
            writer.error("late");
            assertEquals("", out.toString());
        }
    }

    @Nested
    class ProbeConfigTest {

        private Properties minimal() {
            Properties p = new Properties();
            p.setProperty("scenario.id", "entity-mobcap-saturation");
            p.setProperty("scenario.side", "server");
            p.setProperty("warmup.min", "2000");
            p.setProperty("measurement.duration", "12000");
            return p;
        }

        @Test
        void readsRequiredFields() {
            ProbeConfig c = ProbeConfig.of(minimal(), List.of(), List.of(), Path.of("."));
            assertEquals("entity-mobcap-saturation", c.scenarioId());
            assertEquals(ProbeConfig.Side.SERVER, c.side());
            assertEquals(2000.0, c.warmupMinimum());
            assertEquals(12000.0, c.measurementDuration());
        }

        @Test
        void appliesDefaults() {
            ProbeConfig c = ProbeConfig.of(minimal(), List.of(), List.of(), Path.of("."));
            assertEquals(3.0, c.warmupMaxMultiple());
            assertEquals(0.05, c.steadyStateTolerance());
            assertFalse(c.tickWarp());
        }

        @Test
        void rejectsAMissingRequiredKey() {
            Properties p = minimal();
            p.remove("warmup.min");
            assertThrows(
                    IllegalArgumentException.class,
                    () -> ProbeConfig.of(p, List.of(), List.of(), Path.of(".")));
        }

        @Test
        void rejectsANonPositiveWarmup() {
            Properties p = minimal();
            p.setProperty("warmup.min", "0");
            assertThrows(
                    IllegalArgumentException.class,
                    () -> ProbeConfig.of(p, List.of(), List.of(), Path.of(".")));
        }

        @Test
        void rejectsAnUnknownSide() {
            Properties p = minimal();
            p.setProperty("scenario.side", "sideways");
            assertThrows(
                    IllegalArgumentException.class,
                    () -> ProbeConfig.of(p, List.of(), List.of(), Path.of(".")));
        }

        @Test
        void sideDeterminesWhatIsMeasured() {
            assertTrue(ProbeConfig.Side.CLIENT.measuresFrames());
            assertFalse(ProbeConfig.Side.CLIENT.measuresTicks());
            assertTrue(ProbeConfig.Side.SERVER.measuresTicks());
            assertTrue(ProbeConfig.Side.BOTH.measuresFrames());
            assertTrue(ProbeConfig.Side.BOTH.measuresTicks());
        }
    }

    @Nested
    class RuntimeMonitorTest {

        @Test
        void samplesHeapWithoutTouchingGameClasses() {
            RuntimeMonitor monitor = new RuntimeMonitor();
            monitor.resetBaseline();
            RuntimeMonitor.Sample sample = monitor.sample();
            assertTrue(sample.heapMb() > 0);
            assertTrue(sample.allocatedBytes() >= 0);
        }

        @Test
        void reportsHeapEvenWhenTheMxBeanReportsZero() {
            // Regression test. MemoryMXBean.getHeapMemoryUsage().getUsed() was observed
            // returning 0 on a healthy G1 JVM that had not yet collected, which silently
            // zeroed every heap and allocation metric for the whole run. This assertion
            // previously passed only by luck, depending on whether a GC had happened to run
            // in the test JVM first.
            java.lang.management.MemoryUsage beanUsage =
                    java.lang.management.ManagementFactory.getMemoryMXBean().getHeapMemoryUsage();
            Runtime runtime = Runtime.getRuntime();
            long fromRuntime = runtime.totalMemory() - runtime.freeMemory();

            assertTrue(fromRuntime > 0, "Runtime must always report a running program's heap");

            RuntimeMonitor monitor = new RuntimeMonitor();
            monitor.resetBaseline();
            double heapMb = monitor.sample().heapMb();
            assertTrue(
                    heapMb > 0,
                    "heap must be reported even when the MXBean says "
                            + beanUsage.getUsed());
        }

        @Test
        void tracksAllocationAcrossSamples() {
            RuntimeMonitor monitor = new RuntimeMonitor();
            monitor.resetBaseline();
            long sink = 0;
            for (int i = 0; i < 200_000; i++) {
                sink += new long[16].length;
            }
            assertTrue(sink > 0);
            monitor.sample();
            assertTrue(monitor.allocatedBytes() >= 0);
        }
    }
}
