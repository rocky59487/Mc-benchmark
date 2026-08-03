package dev.mcbench.probe.core;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Nested;
import org.junit.jupiter.api.Test;

/**
 * The documented warmup rule, now that both of its halves are implemented.
 *
 * <p>METHODOLOGY section 2 requires a timing plateau <em>and</em> a compilation
 * plateau. Only the first was ever checked, so the JIT-stability guarantee the
 * documentation makes was not enforced anywhere — a series that looked flat
 * while tiered compilation was still promoting hot methods opened the gate, and
 * the compiler's remaining work was charged to the mod.
 */
class WarmupGateTest {

    /** A compiler counter the test drives directly. */
    private static final class FakeCompiler implements CompilationMonitor.Source {
        final AtomicLong total = new AtomicLong();

        @Override
        public long totalCompilationMs() {
            return total.get();
        }
    }

    private static PhaseController controller(CompilationMonitor monitor) {
        // Minimum 10, ceiling 100x, window 4 so a handful of identical samples
        // satisfies the timing half immediately.
        return new PhaseController(10, 100, 20, 4, 0.05, monitor);
    }

    @Nested
    class CompilationHalf {

        @Test
        void aFlatSeriesDoesNotOpenTheGateWhileCompilationContinues() {
            FakeCompiler compiler = new FakeCompiler();
            PhaseController c = controller(new CompilationMonitor(compiler, 10.0, 3));
            c.beginWarmup();

            // Perfectly steady timing, and the compiler is working hard.
            for (int i = 0; i < 500; i++) {
                compiler.total.addAndGet(50);
                c.advance(1, 100);
            }
            assertEquals(
                    Phase.WARMUP, c.phase(),
                    "a flat series must not open the gate while the JIT is still busy");
        }

        @Test
        void theGateOpensOnceCompilationSettles() {
            FakeCompiler compiler = new FakeCompiler();
            PhaseController c = controller(new CompilationMonitor(compiler, 10.0, 3));
            c.beginWarmup();

            for (int i = 0; i < 200; i++) {
                compiler.total.addAndGet(50);
                c.advance(1, 100);
            }
            assertEquals(Phase.WARMUP, c.phase());

            // The compiler goes quiet. Enough samples for three observations.
            for (int i = 0; i < 4 * PhaseController.OBSERVE_EVERY_SAMPLES; i++) {
                c.advance(1, 100);
            }
            assertEquals(Phase.MEASUREMENT, c.phase());
            assertTrue(c.converged());
            assertTrue(c.gateReason().contains("compilation settled"), c.gateReason());
        }

        @Test
        void aCeilingReachedWithTheCompilerBusySaysSo() {
            FakeCompiler compiler = new FakeCompiler();
            // Ceiling at 2x the minimum, so it is hit quickly.
            PhaseController c = new PhaseController(
                    10, 2, 20, 4, 0.05, new CompilationMonitor(compiler, 10.0, 3));
            c.beginWarmup();
            for (int i = 0; i < 30; i++) {
                compiler.total.addAndGet(500);
                c.advance(1, 100);
            }
            assertEquals(Phase.MEASUREMENT, c.phase());
            assertFalse(c.converged());
            assertTrue(
                    c.gateReason().contains("compilation was still active"),
                    "the reason must name which half failed, so an operator knows what "
                            + "to change: " + c.gateReason());
        }

        @Test
        void anUnavailableCompilerDoesNotHoldTheGateShut() {
            // A JVM with no compilation bean must not pin every run to its
            // ceiling; the run proceeds and records that the half was missing.
            PhaseController c = controller(CompilationMonitor.alwaysSettled());
            c.beginWarmup();
            for (int i = 0; i < 20; i++) {
                c.advance(1, 100);
            }
            assertEquals(Phase.MEASUREMENT, c.phase());
            assertTrue(c.converged());
            assertFalse(c.compilationGateAvailable());
            assertTrue(c.gateReason().contains("unavailable"), c.gateReason());
        }
    }

    @Nested
    class SetupPhase {

        @Test
        void setupProgressIsNotWarmupProgress() {
            PhaseController c = controller(CompilationMonitor.alwaysSettled());
            c.beginSetup();
            for (int i = 0; i < 100; i++) {
                c.advance(1, 100);
            }
            assertEquals(Phase.SETUP, c.phase());

            c.beginWarmup();
            assertEquals(100.0, c.setupDuration(), 1e-9);
            // The warmup minimum is 10 and setup consumed 100 units. If setup
            // counted toward warmup the gate would open on the first sample.
            for (int i = 0; i < 9; i++) {
                c.advance(1, 100);
            }
            assertEquals(Phase.WARMUP, c.phase());
            c.advance(1, 100);
            assertEquals(Phase.MEASUREMENT, c.phase());
            assertEquals(10.0, c.warmupDuration(), 1e-9);
        }

        @Test
        void setupSamplesDoNotFeedTheSteadyStateWindow() {
            PhaseController c = controller(CompilationMonitor.alwaysSettled());
            c.beginSetup();
            for (int i = 0; i < 100; i++) {
                c.advance(1, 100);
            }
            c.beginWarmup();
            // The window is 4 wide over two windows, so eight samples are needed
            // before any judgement can be made. If setup's samples had survived,
            // the window would already be full of identical values.
            for (int i = 0; i < 7; i++) {
                c.advance(2, 100);
            }
            assertEquals(
                    Phase.WARMUP, c.phase(),
                    "warmup must not inherit a window filled during setup");
        }
    }
}
