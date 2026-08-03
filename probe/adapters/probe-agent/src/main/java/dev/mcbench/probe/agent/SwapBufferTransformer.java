package dev.mcbench.probe.agent;

import java.lang.instrument.ClassFileTransformer;
import java.security.ProtectionDomain;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

/**
 * Injects a {@link FrameHook#onFrame()} call at the head of LWJGL's swap-buffers method.
 *
 * <p>This is the whole trick behind version-independent frame timing. Minecraft presents each
 * frame through LWJGL, and <b>LWJGL is a third-party library, so its class and method names are
 * never obfuscated</b> — they are byte-identical across every Minecraft version, every mod
 * loader, and every mapping scheme. Instrumenting one call in it therefore works everywhere,
 * without the agent referencing a single game class.
 *
 * <p>Two targets cover the entire history:
 *
 * <ul>
 *   <li>{@code org.lwjgl.glfw.GLFW.glfwSwapBuffers} — LWJGL 3, Minecraft 1.13 and later.
 *   <li>{@code org.lwjgl.opengl.Display.update} — LWJGL 2, Minecraft 1.12.2 and earlier.
 * </ul>
 *
 * <p>The injected instruction is a bare {@code INVOKESTATIC} with no arguments, chosen so the
 * transformer never has to reason about the argument types of whichever overload it found —
 * {@code glfwSwapBuffers} takes a window handle whose representation has changed across LWJGL
 * versions, and depending on that would reintroduce exactly the version coupling this exists to
 * avoid.
 *
 * <p>Injection is at method <em>entry</em> rather than exit. Entry is reached exactly once per
 * call; exits are plural once a method has branches, and instrumenting all of them would
 * double-count. Measuring the interval between consecutive entries gives the same quantity as
 * bracketing the call, for one instruction instead of several.
 */
public final class SwapBufferTransformer implements ClassFileTransformer {

    /** Internal name of the class holding the hook, as it appears in bytecode. */
    private static final String HOOK_OWNER = "dev/mcbench/probe/agent/FrameHook";
    private static final String HOOK_METHOD = "onFrame";
    private static final String HOOK_DESCRIPTOR = "()V";

    /** Internal class name to the method name we instrument within it. */
    static final Map<String, String> TARGETS = Map.of(
            "org/lwjgl/glfw/GLFW", "glfwSwapBuffers",
            "org/lwjgl/opengl/Display", "update");

    private final AtomicInteger injections = new AtomicInteger();
    private final AtomicInteger skipped = new AtomicInteger();

    @Override
    public byte[] transform(
            ClassLoader loader,
            String className,
            Class<?> classBeingRedefined,
            ProtectionDomain protectionDomain,
            byte[] classfileBuffer) {
        if (className == null) {
            return null;
        }
        String method = TARGETS.get(className);
        if (method == null) {
            // Returning null means "unchanged", and is the cheap path for the many thousands
            // of classes a modded launch loads.
            return null;
        }
        if (!hookReachableFrom(loader)) {
            // Refuse rather than inject a call that cannot link.
            //
            // The injected instruction names FrameHook directly. If the classloader that owns
            // LWJGL cannot see it, the call resolves to NoClassDefFoundError *inside the
            // render loop* — crashing the client on its first frame. Losing frame timing is
            // recoverable; breaking the game we were asked to measure is not.
            skipped.incrementAndGet();
            System.err.println(
                    "[mcbench] not instrumenting " + className + ": the hook is not visible "
                    + "from its classloader (" + describe(loader) + "). Frame timing is "
                    + "unavailable on this launch; server-side timing is unaffected.");
            return null;
        }
        try {
            return inject(classfileBuffer, method);
        } catch (Throwable t) {
            // A failed transform must leave the class untouched rather than take the JVM down.
            // Losing frame timing is recoverable; a launch that will not start is not.
            return null;
        }
    }

    private byte[] inject(byte[] original, String targetMethod) {
        ClassReader reader = new ClassReader(original);
        // COMPUTE_MAXS because adding a call can raise the operand stack requirement by one,
        // and a stale max would fail verification. COMPUTE_FRAMES is deliberately avoided: it
        // makes ASM load referenced classes to compute the frame types, which during early
        // startup can trigger circular loading in the very classloader we are hooking.
        ClassWriter writer = new ClassWriter(reader, ClassWriter.COMPUTE_MAXS);

        reader.accept(new ClassVisitor(Opcodes.ASM9, writer) {
            @Override
            public MethodVisitor visitMethod(
                    int access, String name, String descriptor,
                    String signature, String[] exceptions) {
                MethodVisitor delegate =
                        super.visitMethod(access, name, descriptor, signature, exceptions);
                if (!targetMethod.equals(name) || delegate == null) {
                    return delegate;
                }
                injections.incrementAndGet();
                return new MethodVisitor(Opcodes.ASM9, delegate) {
                    @Override
                    public void visitCode() {
                        super.visitCode();
                        super.visitMethodInsn(
                                Opcodes.INVOKESTATIC, HOOK_OWNER, HOOK_METHOD,
                                HOOK_DESCRIPTOR, false);
                    }
                };
            }
        }, ClassReader.EXPAND_FRAMES);

        return writer.toByteArray();
    }

    /**
     * Whether the hook class resolves from the loader that owns the target.
     *
     * <p>Checked per classloader rather than assumed, because the answer genuinely differs:
     * a vanilla launch loads LWJGL from the system loader where the agent already lives, while
     * mod loaders use custom loaders whose delegation rules vary between Fabric's Knot and
     * Forge's ModLauncher, and between their versions.
     */
    static boolean hookReachableFrom(ClassLoader loader) {
        try {
            Class<?> found = (loader == null)
                    ? Class.forName(HOOK_OWNER.replace('/', '.'), false, null)
                    : Class.forName(HOOK_OWNER.replace('/', '.'), false, loader);
            // Reachable is not enough — it must be the *same* class the agent initialised,
            // or the injected call would feed a second, never-started copy.
            return found == FrameHook.class;
        } catch (ClassNotFoundException | LinkageError e) {
            return false;
        }
    }

    private static String describe(ClassLoader loader) {
        return loader == null ? "bootstrap" : loader.getClass().getName();
    }

    /** How many methods were instrumented. Zero means the target was never loaded. */
    public int injections() {
        return injections.get();
    }

    /** Targets skipped because the hook was unreachable from their classloader. */
    public int skipped() {
        return skipped.get();
    }
}
