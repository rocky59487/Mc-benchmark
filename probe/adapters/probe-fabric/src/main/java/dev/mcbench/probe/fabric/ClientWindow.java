package dev.mcbench.probe.fabric;

import net.minecraft.client.MinecraftClient;

/**
 * The size the client is actually rendering at.
 *
 * <p>Held in its own class so the reference to {@code MinecraftClient} is only ever resolved
 * on a client. A dedicated server never calls this and so never loads it.
 */
final class ClientWindow {

    private ClientWindow() {
    }

    /**
     * The framebuffer size as {@code WIDTHxHEIGHT}, or empty when there is no window.
     *
     * <p>The framebuffer rather than the window rectangle, because it is the count of pixels
     * being drawn and that is what a frame time is a function of. On a scaled display the two
     * differ, and the harness is recording this to say what was measured.
     */
    static String describe() {
        MinecraftClient client = MinecraftClient.getInstance();
        if (client == null || client.getWindow() == null) {
            return "";
        }
        int width = client.getWindow().getFramebufferWidth();
        int height = client.getWindow().getFramebufferHeight();
        return width > 0 && height > 0 ? width + "x" + height : "";
    }
}
