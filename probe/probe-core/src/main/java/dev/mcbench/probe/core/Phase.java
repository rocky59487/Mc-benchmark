package dev.mcbench.probe.core;

/** Run phases. Only {@link #MEASUREMENT} samples reach the statistics. */
public enum Phase {
    /** World generation, chunk load, mod init. Untimed. */
    PROVISION("provision"),
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
