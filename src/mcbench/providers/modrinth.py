"""Modrinth mod resolution.

mcbench never redistributes mod jars (docs/LICENSING.md). Mods are named by
coordinates in a suite manifest and resolved here, at run time, on the
operator's own machine.

Modrinth is the default provider because its terms suit automated use: read-only
queries need no authentication, the rate limit is documented and generous, and
downloads are permitted. The one hard requirement is a uniquely identifying
User-Agent — generic library agents risk being blocked, and sending one would be
both rude and fragile.

Standard library only, so that resolving mods needs no dependency stack.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__

__all__ = ["ResolvedMod", "ModrinthError", "ModrinthClient"]

API_ROOT = "https://api.modrinth.com/v2"
PROJECT_URL = "https://github.com/mcbench/mcbench"

#: Hosts a download may come from.
#:
#: The sha512 check already guarantees we benchmark the bytes we expected, but
#: it runs *after* the request. A compromised or spoofed API response could
#: point the URL at an internal address, and the request itself — issued from
#: inside whatever network the harness runs on — is the attack. Pinning the host
#: closes that before anything is fetched.
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "cdn.modrinth.com",
    "cdn-raw.modrinth.com",
    "api.modrinth.com",
})

# Documented limit is 300 requests/minute per IP. We stay well inside it: a
# suite resolves a handful of mods and has no reason to run hot.
MIN_REQUEST_INTERVAL_S = 0.25
MAX_RETRIES = 4


class ModrinthError(RuntimeError):
    """A Modrinth request failed, or returned something unusable."""


@dataclass(frozen=True)
class ResolvedMod:
    """A concrete, downloadable mod file.

    ``sha512`` is recorded in every result's provenance. Two runs whose mod
    hashes differ did not measure the same software, whatever their version
    strings claimed.
    """

    project_id: str
    project_slug: str
    version_id: str
    version_number: str
    filename: str
    url: str
    sha512: str
    size: int
    loaders: tuple[str, ...]
    game_versions: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.project_slug}@{self.version_number} ({self.filename})"


class ModrinthClient:
    """Minimal Modrinth v2 client with polite rate limiting.

    Args:
        contact: Optional contact string appended to the User-Agent. Modrinth
            recommends it so they can reach operators generating unusual load.
        cache_dir: Where downloaded jars are stored. Always outside version
            control — jars must never be committed.
    """

    def __init__(
        self,
        *,
        contact: str | None = None,
        cache_dir: str | Path = "cache/mods",
        timeout: float = 30.0,
    ) -> None:
        self.user_agent = f"mcbench/{__version__} (+{PROJECT_URL}" + (
            f"; {contact})" if contact else ")"
        )
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self._last_request = 0.0

    # -- HTTP ------------------------------------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < MIN_REQUEST_INTERVAL_S:
            time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)
        self._last_request = time.monotonic()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{API_ROOT}{path}"
        if params:
            # Modrinth expects JSON-encoded array parameters.
            encoded = {
                k: json.dumps(v) if isinstance(v, (list, tuple)) else str(v)
                for k, v in params.items()
            }
            url = f"{url}?{urllib.parse.urlencode(encoded)}"

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self._throttle()
            request = urllib.request.Request(
                url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise ModrinthError(f"not found: {url}") from exc
                if exc.code == 429:
                    # Honour the server's own reset hint rather than guessing.
                    wait = float(exc.headers.get("X-Ratelimit-Reset", 2 ** attempt))
                    last_error = exc
                    time.sleep(min(wait, 60.0))
                    continue
                if 500 <= exc.code < 600:
                    last_error = exc
                    time.sleep(2 ** attempt)
                    continue
                raise ModrinthError(f"HTTP {exc.code} for {url}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                time.sleep(2 ** attempt)

        raise ModrinthError(
            f"giving up on {url} after {MAX_RETRIES} attempts: {last_error}"
        )

    # -- Resolution ------------------------------------------------------

    def resolve(
        self,
        project: str,
        *,
        game_version: str,
        loader: str,
        version: str | None = None,
    ) -> ResolvedMod:
        """Resolve a project to one concrete file.

        ``version`` may be a Modrinth version id or a version number. When it is
        omitted the newest compatible release is chosen and the caller is
        expected to pin the result — an unpinned suite is not reproducible and
        is refused entry to the public corpus.
        """
        versions = self._get(
            f"/project/{urllib.parse.quote(project)}/version",
            {"loaders": [loader], "game_versions": [game_version]},
        )
        if not isinstance(versions, list) or not versions:
            raise ModrinthError(
                f"{project}: no versions for {loader} {game_version}. "
                f"The mod may not support this combination."
            )

        if version is not None:
            matched = [
                v for v in versions
                if v.get("id") == version or v.get("version_number") == version
            ]
            if not matched:
                available = ", ".join(
                    sorted({str(v.get("version_number")) for v in versions})[:8]
                )
                raise ModrinthError(
                    f"{project}: version {version!r} not found for "
                    f"{loader} {game_version}. Available: {available}"
                )
            chosen = matched[0]
        else:
            # Prefer a stable release over alpha/beta when one exists; a
            # benchmark corpus built on prereleases ages badly.
            releases = [v for v in versions if v.get("version_type") == "release"]
            chosen = (releases or versions)[0]

        files = chosen.get("files") or []
        primary = next((f for f in files if f.get("primary")), files[0] if files else None)
        if primary is None:
            raise ModrinthError(f"{project}: version {chosen.get('id')} has no files")

        hashes = primary.get("hashes") or {}
        if "sha512" not in hashes:
            raise ModrinthError(
                f"{project}: file {primary.get('filename')} has no sha512; "
                f"cannot verify provenance"
            )

        return ResolvedMod(
            project_id=str(chosen.get("project_id", "")),
            project_slug=project,
            version_id=str(chosen.get("id", "")),
            version_number=str(chosen.get("version_number", "")),
            filename=str(primary.get("filename", "")),
            url=str(primary.get("url", "")),
            sha512=str(hashes["sha512"]),
            size=int(primary.get("size", 0)),
            loaders=tuple(chosen.get("loaders", ())),
            game_versions=tuple(chosen.get("game_versions", ())),
        )

    def resolve_all(
        self,
        projects: Sequence[tuple[str, str | None]],
        *,
        game_version: str,
        loader: str,
    ) -> list[ResolvedMod]:
        """Resolve several mods, reporting every failure rather than the first.

        A suite that names eight mods and has three broken pins should tell the
        operator about all three, not make them discover each in turn across
        three round trips.
        """
        resolved: list[ResolvedMod] = []
        failures: list[str] = []
        for project, version in projects:
            try:
                resolved.append(
                    self.resolve(
                        project, game_version=game_version, loader=loader, version=version
                    )
                )
            except ModrinthError as exc:
                failures.append(str(exc))
        if failures:
            raise ModrinthError(
                f"{len(failures)} mod(s) could not be resolved:\n  "
                + "\n  ".join(failures)
            )
        return resolved

    # -- Download --------------------------------------------------------

    @staticmethod
    def _check_download_url(url: str) -> None:
        """Refuse a download URL that is not plainly a Modrinth CDN address."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise ModrinthError(
                f"refusing a non-HTTPS download URL ({parsed.scheme!r}); "
                f"mod jars are executed, so their transport must be authenticated"
            )
        host = (parsed.hostname or "").lower()
        if host not in ALLOWED_DOWNLOAD_HOSTS:
            raise ModrinthError(
                f"refusing a download from unexpected host {host!r}. Expected one "
                f"of {sorted(ALLOWED_DOWNLOAD_HOSTS)}. A URL pointing elsewhere "
                f"means the API response was not what we think it was."
            )

    @staticmethod
    def _safe_cache_name(mod: ResolvedMod) -> str:
        """Build a cache filename that cannot escape the cache directory.

        The filename comes from the API and is therefore attacker-influenced.
        A value like ``../../../.bashrc`` would otherwise write outside the
        cache, so only the basename is kept and anything unusual is stripped.
        """
        base = Path(mod.filename).name
        cleaned = re.sub(r"[^A-Za-z0-9._+-]", "_", base)[:120]
        return f"{mod.sha512[:16]}-{cleaned or 'mod.jar'}"

    def download(self, mod: ResolvedMod) -> Path:
        """Download a resolved mod, verifying its hash. Cached by hash.

        The cache is keyed on sha512 rather than filename so that two versions
        sharing a filename cannot collide, and a corrupted or truncated download
        can never be served from cache as if it were valid.
        """
        self._check_download_url(mod.url)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / self._safe_cache_name(mod)

        if target.exists() and _sha512(target) == mod.sha512:
            return target

        self._throttle()
        request = urllib.request.Request(
            mod.url, headers={"User-Agent": self.user_agent}
        )
        # Write to a temporary path and move into place, so an interrupted
        # download never leaves a file that looks cached.
        staging = target.with_suffix(target.suffix + ".partial")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                staging.write_bytes(response.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            staging.unlink(missing_ok=True)
            raise ModrinthError(f"download failed for {mod}: {exc}") from exc

        actual = _sha512(staging)
        if actual != mod.sha512:
            staging.unlink(missing_ok=True)
            raise ModrinthError(
                f"hash mismatch for {mod}: expected {mod.sha512[:16]}..., "
                f"got {actual[:16]}...  Refusing to benchmark unverified bytes."
            )

        staging.replace(target)
        return target


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
