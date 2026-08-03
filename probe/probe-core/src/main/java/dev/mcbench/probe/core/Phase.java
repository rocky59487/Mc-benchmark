package dev.mcbench.probe.core;

/** Run phases. Only {@link #MEASUREMENT} samples reach the statistics. */
public enum Phase {
    /** Mod init, world load. Untimed, and before any scenario command has run. */
    PROVISION("provision"),
    /**
     * Scenario setup: the commands that build the world the scenario describes.
     *
     * <p>A phase of its own rather than part of warmup, because it used to be part of warmup by
     * accident. {@code start()} began the timed warmup immediately and the adapter then pumped
     * setup commands into it, so setup consumed the warmup budget — and a scenario whose setup
     * ran longer than the budget entered <em>measurement</em> while commands were still
     * changing the world. Two runs of the same scenario received different effective warmup
     * depending on how long their setup happened to take.
     */
    SETUP("setup"),
    /** Timed but discarded: the JIT is still compiling and caches are cold. */
    WARMUP("warmup"),
    /** Retained. */
    MEASUREMENT("measurement");

    private final String wireName;

    Phase(String wireName) {
        this.wireName = wireName;
    }

    public String wireName() {
        return wireName;
    }
}
