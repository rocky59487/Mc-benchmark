package dev.mcbench.probe.agent;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.stream.Stream;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Runs the agent in a real JVM, attached the way an operator would attach it.
 *
 * <p>The unit tests prove the transformer rewrites bytecode correctly. They cannot prove the
 * agent *works*, because everything that makes an agent hard happens at runtime: which
 * classloader owns which class, whether the injected call links, whether the shutdown hook
 * flushes. The first version of this agent passed every unit test and then died on launch with
 * an {@code IllegalAccessError}, because appending the agent jar to the bootstrap search had
 * quietly created a second copy of its own classes on another loader.
 *
 * <p>So this forks a JVM with {@code -javaagent}, drives a stand-in swap-buffers call, and
 * checks that a parseable stream comes out the other side. A stand-in is used rather than real
 * LWJGL because the genuine class loads native libraries in its static initialiser; the class
 * name and method signature are what the agent keys on, and those are reproduced exactly.
 */
class AgentIntegrationTest {

    private Path agentJar() {
        Path built = Path.of("build/libs/mcbench-probe-agent-0.1.0.jar");
        Assumptions.assumeTrue(
                Files.exists(built),
                "agent jar not built; run :shadowJar first (skipped, not failed)");
        return built.toAbsolutePath();
    }

    private void compile(Path sourceDir, Path classesDir, List<Path> sources) throws Exception {
        List<String> command = new ArrayList<>(
                List.of(javaHomeTool("javac"), "-d", classesDir.toString()));
        sources.forEach(s -> command.add(s.toString()));
        Process process = new ProcessBuilder(command).redirectErrorStream(true).start();
        String output = new String(process.getInputStream().readAllBytes());
        assertTrue(process.waitFor(120, TimeUnit.SECONDS), "javac timed out");
        assertEquals(0, process.exitValue(), "javac failed: " + output);
    }

    private static String javaHomeTool(String name) {
        return Path.of(System.getProperty("java.home"), "bin", name).toString();
    }

    private Path writeStandIn(Path root) throws IOException {
        Path pkg = root.resolve("src/org/lwjgl/glfw");
        Files.createDirectories(pkg);
        // Exactly the fully-qualified name and signature the transformer targets.
        Files.writeString(pkg.resolve("GLFW.java"), """
                package org.lwjgl.glfw;
                public final class GLFW {
                    private static long sink;
                    public static void glfwSwapBuffers(long window) { sink += window; }
                    public static long sink() { return sink; }
                }
                """);
        Path harness = root.resolve("src/Harness.java");
        Files.writeString(harness, """
                import org.lwjgl.glfw.GLFW;
                public final class Harness {
                    public static void main(String[] a) throws Exception {
                        long frames = Long.parseLong(a[0]);
                        for (long i = 0; i < frames; i++) {
                            GLFW.glfwSwapBuffers(1L);
                            Thread.sleep(0, 900_000);
                        }
                    }
                }
                """);
        return pkg.resolve("GLFW.java");
    }

    private void writePlan(Path planDir) throws IOException {
        Files.createDirectories(planDir);
        // Short windows so the fork finishes quickly; a loose steady-state tolerance because
        // Thread.sleep jitter is far noisier than a real render loop.
        Files.writeString(planDir.resolve("probe.properties"), """
                scenario.id=agent-integration
                scenario.version=1.0.0
                scenario.side=client
                warmup.min=0.3
                warmup.max_multiple=3
                warmup.steady_state_tolerance=0.5
                warmup.steady_state_window=60
                measurement.duration=0.8
                output=probe.jsonl
                """);
        Files.writeString(planDir.resolve("setup.txt"), "");
        Files.writeString(planDir.resolve("workload.txt"), "");
    }

    @Test
    void producesAParseableStreamFromARealJvm(@TempDir Path temp) throws Exception {
        Path agent = agentJar();
        Path planDir = temp.resolve("plan");
        writePlan(planDir);
        writeStandIn(temp);

        Path classes = temp.resolve("classes");
        Files.createDirectories(classes);
        try (Stream<Path> sources = Files.walk(temp.resolve("src"))) {
            compile(temp.resolve("src"), classes,
                    sources.filter(p -> p.toString().endsWith(".java")).toList());
        }

        ProcessBuilder builder = new ProcessBuilder(
                javaHomeTool("java"),
                "-javaagent:" + agent,
                "-cp", classes.toString(),
                "Harness", "4000");
        builder.environment().put(
                "MCBENCH_PROBE_CONFIG", planDir.resolve("probe.properties").toString());
        builder.redirectErrorStream(true);

        Process process = builder.start();
        String output = new String(process.getInputStream().readAllBytes());
        assertTrue(process.waitFor(180, TimeUnit.SECONDS), "forked JVM timed out");

        // The failure this test exists for: the agent aborting during premain.
        assertFalse(
                output.contains("IllegalAccessError"),
                "agent hit a classloader split:\n" + output);
        assertFalse(output.contains("agent disabled"), "agent disabled:\n" + output);
        assertTrue(output.contains("agent active"), "agent never installed:\n" + output);
        assertEquals(0, process.exitValue(), "forked JVM failed:\n" + output);

        Path stream = planDir.resolve("probe-agent.jsonl");
        assertTrue(Files.exists(stream), "no agent stream written:\n" + output);

        List<String> lines = Files.readAllLines(stream);
        assertTrue(lines.size() > 3, "stream too short: " + lines.size());
        assertTrue(lines.get(0).contains("\"type\":\"hello\""));
        assertTrue(lines.get(0).contains("\"platform\":\"jvm-agent\""));
        assertTrue(
                lines.get(lines.size() - 1).contains("\"type\":\"bye\""),
                "stream did not close cleanly; the shutdown hook must flush");

        long frameEvents = lines.stream().filter(l -> l.contains("\"type\":\"frame\"")).count();
        assertTrue(frameEvents > 0, "no frames recorded — the hook never fired");
    }

    @Test
    void staysInertWithoutAConfig(@TempDir Path temp) throws Exception {
        // Harmless to leave on a launch command permanently, which is the only way anyone
        // will actually keep it there.
        Path agent = agentJar();
        writeStandIn(temp);
        Path classes = temp.resolve("classes");
        Files.createDirectories(classes);
        try (Stream<Path> sources = Files.walk(temp.resolve("src"))) {
            compile(temp.resolve("src"), classes,
                    sources.filter(p -> p.toString().endsWith(".java")).toList());
        }

        ProcessBuilder builder = new ProcessBuilder(
                javaHomeTool("java"), "-javaagent:" + agent,
                "-cp", classes.toString(), "Harness", "50");
        builder.environment().remove("MCBENCH_PROBE_CONFIG");
        builder.redirectErrorStream(true);

        Process process = builder.start();
        String output = new String(process.getInputStream().readAllBytes());
        assertTrue(process.waitFor(120, TimeUnit.SECONDS));
        assertEquals(0, process.exitValue(), output);
        assertFalse(output.contains("agent active"), "should not have installed:\n" + output);
    }
}
