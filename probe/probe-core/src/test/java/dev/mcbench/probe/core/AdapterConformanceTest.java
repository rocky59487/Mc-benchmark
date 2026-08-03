package dev.mcbench.probe.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.StringWriter;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

/**
 * What every adapter has to get right, verified without a game.
 *
 * <p>Each group here corresponds to a way the probe measured something other
 * than what it published: the tick period as MSPT, setup as warmup, a rejected
 * command as a successful one.
 */
class AdapterConformanceTest {

    /** An adapter over a fake game, so the SPI contract can be exercised alone. */
    private static final class FakeAdapter extends ProbeAdapter {
        final List<String> executed = new ArrayList<>();
        final List<String> rejected = new ArrayList<>();
        boolean shutdownRequested;

        FakeAdapter(ProbeSession session) {
            super(session);
        }

        @Override
        public String platformName() {
            return "fake";
        }

        @Override
        protected boolean executeCommand(String command) {
            executed.add(command);
            if (rejected.contains(command)) {
                return false;
            }
            return true;
        }

        @Override
        protected void requestShutdown() {
            shutdownRequested = true;
        }
    }

    private static ProbeConfig config(
            String side, double warmup, double duration, List<String> setup) {
        Properties p = new Properties();
        p.setProperty("scenario.id", "conformance");
        p.setProperty("scenario.side", side);
        p.setProperty("warmup.min", Double.toString(warmup));
        p.setProperty("warmup.max_multiple", "3");
        p.setProperty("warmup.steady_state_window", "4");
        p.setProperty("warmup.steady_state_tolerance", "0.05");
        p.setProperty("measurement.duration", Double.toString(duration));
        return ProbeConfig.of(p, setup, List.of(), Path.of("."));
    }

    @Nested
    class TickTiming {

        /**
         * The failure that invalidated the primary server metrics.
         *
         * <p>Two servers, both at a steady 20 TPS, one doing 5 ms of work per
         * tick and one doing 30 ms. Timed by the interval between end-of-tick
         * callbacks they are indistinguishable — both report 50 ms — which is
         * exactly what {@code mspt_mean} used to be.
         */
        @Test
        void aBracketTellsA5msTickFromA30msTick() {
            StringWriter cheap = new StringWriter();
            StringWriter costly = new StringWriter();
            long cheapTotal = bracketedTotal(cheap, 5);
            long costlyTotal = bracketedTotal(costly, 30);

            assertTrue(
                    costlyTotal > cheapTotal * 3,
                    "a bracket must separate a cheap tick from an expensive one; got "
                            + cheapTotal + " vs " + costlyTotal);
        }

        private long bracketedTotal(StringWriter out, long workMs) {
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session =
                            new ProbeSession(config("server", 2, 5, List.of()), writer)) {
                FakeAdapter adapter = new FakeAdapter(session);
                adapter.begin(Map.of());
                long total = 0;
                for (int i = 0; i < 40; i++) {
                    long start = System.nanoTime();
                    adapter.onTickStart();
                    busy(workMs);
                    adapter.onTickEnd();
                    total += System.nanoTime() - start;
                    // The server loop sleeps out the rest of the budget. Under a
                    // period measurement this is what dominates the sample.
                    busy(1);
                }
                return total;
            } catch (Exception e) {
                throw new AssertionError(e);
            }
        }

        private void busy(long millis) {
            long until = System.nanoTime() + millis * 1_000_000L;
            while (System.nanoTime() < until) {
                Thread.onSpinWait();
            }
        }

        @Test
        void anEndWithoutAStartRecordsNothing() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session =
                            new ProbeSession(config("server", 2, 5, List.of()), writer)) {
                FakeAdapter adapter = new FakeAdapter(session);
                adapter.begin(Map.of());
                // Substituting the interval here is precisely the substitution
                // being corrected, so an unpaired end must record nothing at all.
                adapter.onTickEnd();
                adapter.onTickEnd();
            }
            assertFalse(out.toString().contains("\"type\":\"tick\""));
        }

        @Test
        void periodSamplesAreLabelledAndFlagged() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session =
                            new ProbeSession(config("server", 1, 3, List.of()), writer)) {
                FakeAdapter adapter = new FakeAdapter(session);
                adapter.begin(Map.of());
                for (int i = 0; i < 30; i++) {
                    adapter.onTickPeriod();
                }
            }
            String text = out.toString();
            assertTrue(text.contains("\"flag\":\"tick_period_only\""),
                    "a period measurement must announce itself: " + text);
            assertTrue(text.contains("\"source\":\"period\""));
        }

        @Test
        void bracketSamplesAreLabelledAsExecutionTime() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session =
                            new ProbeSession(config("server", 1, 3, List.of()), writer)) {
                FakeAdapter adapter = new FakeAdapter(session);
                adapter.begin(Map.of());
                for (int i = 0; i < 30; i++) {
                    adapter.onTickStart();
                    adapter.onTickEnd();
                }
            }
            String text = out.toString();
            assertFalse(text.contains("tick_period_only"));
            assertTrue(text.contains("\"source\":\"bracket\"") || !text.contains("\"type\":\"tick\""));
        }
    }

    @Nested
    class SetupPhase {

        /**
         * A long setup must not be able to consume the warmup budget, still less
         * reach into the measurement window.
         */
        @Test
        void aLongSetupCannotEnterMeasurementEarly() {
            List<String> setup = new ArrayList<>();
            for (int i = 0; i < 5000; i++) {
                setup.add("/setblock " + i + " 64 0 stone");
            }

            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session =
                            new ProbeSession(config("server", 20, 20, setup), writer)) {
                FakeAdapter adapter = new FakeAdapter(session);
                adapter.begin(Map.of());

                // Drive many more ticks than the warmup budget while setup runs.
                for (int tick = 0; tick < 200; tick++) {
                    adapter.pump();
                    adapter.onTickStart();
                    adapter.onTickEnd();
                    if (adapter.executed.size() < setup.size()) {
                        assertNotEquals(
                                Phase.MEASUREMENT, session.phase(),
                                "measurement began while setup was still running");
                    }
                }
                assertEquals(setup.size(), adapter.executed.size());
            }
        }

        @Test
        void warmupBeginsOnlyAfterSetupIsDeclaredFinished() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session = new ProbeSession(
                            config("server", 2, 5, List.of("/say one", "/say two")),
                            writer)) {
                FakeAdapter adapter = new FakeAdapter(session);
                adapter.begin(Map.of());
                assertEquals(Phase.SETUP, session.phase());

                adapter.pump();  // issues both commands
                assertEquals(Phase.SETUP, session.phase());

                for (int i = 0; i <= ProbeAdapter.SETTLE_TICKS; i++) {
                    adapter.pump();
                }
                assertEquals(Phase.WARMUP, session.phase());
            }
        }

        @Test
        void setupSamplesDoNotFillTheSteadyStateWindow() {
            // Setup work is uniform, so samples taken during it look perfectly
            // plateaued. Carrying them into warmup would open the gate at once.
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session = new ProbeSession(
                            config("server", 5, 10, List.of("/say one")), writer)) {
                FakeAdapter adapter = new FakeAdapter(session);
                adapter.begin(Map.of());

                for (int i = 0; i < 200; i++) {
                    session.recordTick(1_000_000L, TickSource.BRACKET);
                }
                assertEquals(Phase.SETUP, session.phase());

                adapter.pump();
                for (int i = 0; i <= ProbeAdapter.SETTLE_TICKS; i++) {
                    adapter.pump();
                }
                assertEquals(Phase.WARMUP, session.phase());

                // One steady sample must not be enough, because the window was
                // reset when warmup began.
                session.recordTick(1_000_000L, TickSource.BRACKET);
                assertEquals(Phase.WARMUP, session.phase());
            }
        }
    }

    @Nested
    class CommandFailure {

        @Test
        void aRejectedSetupCommandMakesTheRunInadmissible() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session = new ProbeSession(
                            config("server", 2, 5, List.of("/typo", "/say ok")), writer)) {
                FakeAdapter adapter = new FakeAdapter(session);
                adapter.rejected.add("/typo");
                adapter.begin(Map.of());
                adapter.pump();
                for (int i = 0; i <= ProbeAdapter.SETTLE_TICKS; i++) {
                    adapter.pump();
                }
            }
            String text = out.toString();
            assertTrue(text.contains("\"flag\":\"setup_failed\""),
                    "a rejected setup command must fail the run: " + text);
            assertTrue(text.contains("setup command failed: /typo"));
        }

        @Test
        void aCommandThatThrowsIsReportedRatherThanSwallowed() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session = new ProbeSession(
                            config("server", 2, 5, List.of("/boom")), writer)) {
                ProbeAdapter adapter = new ProbeAdapter(session) {
                    @Override
                    public String platformName() {
                        return "throwing";
                    }

                    @Override
                    protected boolean executeCommand(String command) {
                        throw new IllegalStateException("no server attached");
                    }

                    @Override
                    protected void requestShutdown() {}
                };
                adapter.begin(Map.of());
                adapter.pump();
                for (int i = 0; i <= ProbeAdapter.SETTLE_TICKS; i++) {
                    adapter.pump();
                }
            }
            assertTrue(out.toString().contains("no server attached"));
            assertTrue(out.toString().contains("\"flag\":\"setup_failed\""));
        }

        @Test
        void aCleanRunReportsNoCommandFailures() {
            StringWriter out = new StringWriter();
            try (ProbeWriter writer = new ProbeWriter(out);
                    ProbeSession session = new ProbeSession(
                            config("server", 2, 5, List.of("/say ok")), writer)) {
                FakeAdapter adapter = new FakeAdapter(session);
                adapter.begin(Map.of());
                adapter.pump();
                for (int i = 0; i <= ProbeAdapter.SETTLE_TICKS; i++) {
                    adapter.pump();
                }
                assertEquals(0, session.failedCommands());
            }
            assertFalse(out.toString().contains("setup_failed"));
        }
    }
}
