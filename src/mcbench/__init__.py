"""mcbench — a controlled, reproducible performance benchmark for Minecraft mods.

The project's premise is that almost every performance claim in the Minecraft
ecosystem is made from single unrepeated runs, in blocked execution order, with
no variance estimate — which cannot distinguish a real effect from thermal
drift. mcbench exists to make that distinction, for client and server alike.

Start with docs/METHODOLOGY.md; it is the specification this code implements.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
