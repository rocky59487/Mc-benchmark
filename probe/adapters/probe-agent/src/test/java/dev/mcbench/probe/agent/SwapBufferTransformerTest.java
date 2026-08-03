package dev.mcbench.probe.agent;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.io.InputStream;
import org.junit.jupiter.api.Test;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

/**
 * Verifies the transformer against the <em>real</em> LWJGL class it will meet in the field.
 *
 * <p>Testing only against a hand-made stand-in would prove the transformer can rewrite a class
 * shaped the way we imagined LWJGL to be. The claim being made is stronger than that — that
 * LWJGL's names are stable and instrumentable — so the real artefact is pulled from Maven and
 * rewritten here.
 */
class SwapBufferTransformerTest {

    /** The loader that owns FrameHook, which is what the agent sees in the field. */
    private ClassLoader loader() {
        return getClass().getClassLoader();
    }

    private byte[] classBytes(String internalName) throws IOException {
        try (InputStream in = getClass().getClassLoader()
                .getResourceAsStream(internalName + ".class")) {
            assertNotNull(in, "not on the test classpath: " + internalName);
            return in.readAllBytes();
        }
    }

    /** Whether a method in the given class contains a call to the hook. */
    private boolean callsHook(byte[] bytecode, String method) {
        boolean[] found = {false};
        new ClassReader(bytecode).accept(new ClassVisitor(Opcodes.ASM9) {
            @Override
            public MethodVisitor visitMethod(
                    int access, String name, String descriptor,
                    String signature, String[] exceptions) {
                if (!method.equals(name)) {
                    return null;
                }
                return new MethodVisitor(Opcodes.ASM9) {
                    @Override
                    public void visitMethodInsn(
                            int opcode, String owner, String name2,
                            String descriptor2, boolean isInterface) {
                        if (opcode == Opcodes.INVOKESTATIC
                                && owner.equals("dev/mcbench/probe/agent/FrameHook")
                                && name2.equals("onFrame")) {
                            found[0] = true;
                        }
                    }
                };
            }
        }, 0);
        return found[0];
    }

    @Test
    void instrumentsTheRealLwjglGlfwClass() throws IOException {
        byte[] original = classBytes("org/lwjgl/glfw/GLFW");
        assertFalse(callsHook(original, "glfwSwapBuffers"), "precondition: not yet hooked");

        SwapBufferTransformer transformer = new SwapBufferTransformer();
        byte[] transformed = transformer.transform(
                loader(), "org/lwjgl/glfw/GLFW", null, null, original);

        assertNotNull(transformed, "the real LWJGL class should have been transformed");
        assertTrue(transformer.injections() > 0);
        assertTrue(
                callsHook(transformed, "glfwSwapBuffers"),
                "glfwSwapBuffers must call the hook after transformation");
    }

    @Test
    void transformedRealClassStillVerifies() throws IOException {
        // A class that fails verification takes the JVM down at link time, which for a
        // benchmarking tool means breaking the game it was asked to measure.
        byte[] transformed = new SwapBufferTransformer().transform(
                loader(), "org/lwjgl/glfw/GLFW", null, null, classBytes("org/lwjgl/glfw/GLFW"));

        ClassLoader loader = new ClassLoader(null) {
            Class<?> defineIt(byte[] bytes) {
                return defineClass(null, bytes, 0, bytes.length);
            }
        };
        // defineClass performs format and structural checks; a malformed rewrite throws here.
        java.lang.reflect.Method define;
        try {
            define = loader.getClass().getDeclaredMethod("defineIt", byte[].class);
            define.setAccessible(true);
            assertNotNull(define.invoke(loader, (Object) transformed));
        } catch (ReflectiveOperationException e) {
            throw new AssertionError("transformed class failed to define: " + e.getCause(), e);
        }
    }

    @Test
    void leavesUnrelatedClassesUntouched() throws IOException {
        // Returning null means "unchanged" and is the cheap path for the many thousands of
        // classes a modded launch loads.
        SwapBufferTransformer transformer = new SwapBufferTransformer();
        assertNull(transformer.transform(
                loader(), "java/lang/String", null, null, new byte[] {1, 2, 3}));
        assertNull(transformer.transform(loader(), null, null, null, new byte[0]));
        assertEquals(0, transformer.injections());
    }

    @Test
    void malformedBytecodeIsLeftAloneRatherThanCrashingTheLaunch() {
        // Losing frame timing is recoverable; a launch that will not start is not.
        SwapBufferTransformer transformer = new SwapBufferTransformer();
        assertNull(transformer.transform(
                loader(), "org/lwjgl/glfw/GLFW", null, null, new byte[] {0x00, 0x01}));
    }

    @Test
    void refusesToInstrumentWhenTheHookIsUnreachable() throws IOException {
        // The safety property that keeps a failed agent from breaking the game.
        //
        // The injected instruction names FrameHook directly, so if the loader owning LWJGL
        // cannot see it the call resolves to NoClassDefFoundError *inside the render loop*.
        // Passing the bootstrap loader (null) reproduces exactly that: the hook is not there.
        SwapBufferTransformer transformer = new SwapBufferTransformer();
        byte[] result = transformer.transform(
                null, "org/lwjgl/glfw/GLFW", null, null, classBytes("org/lwjgl/glfw/GLFW"));

        assertNull(result, "must decline rather than inject an unlinkable call");
        assertEquals(0, transformer.injections());
        assertEquals(1, transformer.skipped());
    }

    @Test
    void anIsolatedLoaderIsAlsoRefused() throws IOException {
        // A loader with no parent cannot see the hook however the agent was launched.
        ClassLoader isolated = new ClassLoader(null) {};
        SwapBufferTransformer transformer = new SwapBufferTransformer();
        assertNull(transformer.transform(
                isolated, "org/lwjgl/glfw/GLFW", null, null, classBytes("org/lwjgl/glfw/GLFW")));
        assertEquals(1, transformer.skipped());
    }

    @Test
    void coversBothLwjglGenerations() {
        // LWJGL 3 for 1.13+, LWJGL 2 for 1.12.2 and earlier — between them, every version
        // anyone still runs.
        assertEquals("glfwSwapBuffers", SwapBufferTransformer.TARGETS.get("org/lwjgl/glfw/GLFW"));
        assertEquals("update", SwapBufferTransformer.TARGETS.get("org/lwjgl/opengl/Display"));
    }

    @Test
    void injectsAtEntryNotExit() throws IOException {
        // Entry is reached exactly once per call; exits are plural once a method branches,
        // and instrumenting all of them would double-count frames.
        byte[] transformed = new SwapBufferTransformer().transform(
                loader(), "org/lwjgl/glfw/GLFW", null, null, classBytes("org/lwjgl/glfw/GLFW"));

        int[] hookCalls = {0};
        new ClassReader(transformed).accept(new ClassVisitor(Opcodes.ASM9) {
            @Override
            public MethodVisitor visitMethod(int a, String name, String d, String s, String[] e) {
                if (!"glfwSwapBuffers".equals(name)) {
                    return null;
                }
                return new MethodVisitor(Opcodes.ASM9) {
                    @Override
                    public void visitMethodInsn(int op, String owner, String n, String d2, boolean i) {
                        if (op == Opcodes.INVOKESTATIC
                                && owner.equals("dev/mcbench/probe/agent/FrameHook")) {
                            hookCalls[0]++;
                        }
                    }
                };
            }
        }, 0);
        // One call per overload of glfwSwapBuffers, never more.
        assertTrue(hookCalls[0] >= 1);
        assertEquals(new SwapBufferTransformer().transform(
                loader(), "org/lwjgl/glfw/GLFW", null, null,
                classBytes("org/lwjgl/glfw/GLFW")).length, transformed.length);
    }
}
