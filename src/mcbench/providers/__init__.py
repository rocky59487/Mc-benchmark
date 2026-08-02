"""Mod resolution providers.

Providers turn the coordinates in a suite manifest into concrete, hash-verified
files on the operator's own machine. mcbench never redistributes mod jars; see
docs/LICENSING.md for the constraint and for why Modrinth is the default while
CurseForge is opt-in.
"""

from .modrinth import ModrinthClient, ModrinthError, ResolvedMod

__all__ = ["ModrinthClient", "ModrinthError", "ResolvedMod"]
