# Security model

mcbench downloads code from the internet and executes it. That is not a flaw to
be mitigated — it is the entire function of the tool. Everything below follows
from taking that seriously.

## What is trusted, and what is not

| Input | Trust | Why it matters |
|---|---|---|
| Mod jars | **Untrusted** | Executed with full JVM privileges by design |
| Scenario files | **Untrusted** | Meant to be shared and committed; compiled into commands |
| Suite manifests | **Untrusted** | Name mods to download and run |
| Result documents | **Untrusted** | Parsed by `analyse`, ingested into a corpus |
| Modrinth API responses | **Untrusted** | Determine what gets downloaded and from where |
| The harness itself | Trusted | It is the thing making the decisions |

The uncomfortable one is the second row. Scenarios look like configuration, so
they invite being treated as data — but they compile into commands that run
inside the game. A shared scenario is code.

## The central limitation, stated plainly

**mcbench does not sandbox mods, and cannot.** Benchmarking a mod means running
it in a real JVM with real filesystem and network access. A malicious mod jar can
do anything the user running it can do. No amount of care inside this codebase
changes that.

So the operator's responsibility is real and cannot be delegated to the tool:

- Benchmark mods from sources you would install on your own machine.
- For unknown or adversarial jars, run in a throwaway VM or container with no
  credentials, no host mounts, and no network path to anything private.
- CI runners that benchmark community submissions must be treated as
  compromised-by-default and isolated accordingly.

Everything mcbench *can* do is reduce the ways an attacker reaches that point
without the operator choosing it.

## Vulnerabilities found in this codebase

Each of these was present, was found by attacking the code rather than reasoning
about it, and has a regression test in `tests/test_security.py`.

### Command injection through scenario files

The probe plan is a newline-delimited file. A scenario action of

```json
{"op": "command", "value": "say hello\nop @a"}
```

compiled to one entry in memory and **three commands on disk**, because the
writer appends a newline per entry. A shared scenario — which this project
actively encourages — could therefore run commands it did not appear to declare.

Fixed by rejecting control characters in any emitted command, and by sweeping
*every* emitted line rather than only the pass-through `command` op: structure
templates, dialect spellings and interpolated coordinates all reach the same
file, and one unchecked path defeats the check. Over-long commands are refused
too, since Minecraft truncates them and would silently run something else.

The compiler **refuses rather than escapes**. A scenario doing this is either
malicious or broken, and rewriting it into something that looks valid hides both.

### Zip bombs in mod inspection

`mcbench inspect` reads every mixin class in a jar. A 204 KB archive whose
entries expand to 200 MB exhausted memory — and inspection is often the *first*
thing run against a pack of unknown origin, so it must survive a hostile jar
rather than be the thing that falls over.

Fixed with per-entry, per-jar and entry-count limits, plus a compression-ratio
check. Sizes are checked against the `ZipInfo` header *before* reading, and the
read is bounded again afterwards because the declared size is attacker
controlled. Metadata is still returned; only the hostile entry is skipped, and
the skip is reported rather than silently swallowed.

### SSRF and path traversal in the downloader

Downloads are hash-verified, but **the hash check runs after the request**. A
compromised or spoofed API response pointing at `169.254.169.254` or an internal
host would have issued that request from inside whatever network the harness runs
on. Separately, the cache filename came from the API, so `../../../.bashrc` would
have written outside the cache directory.

Fixed by pinning the download host and scheme before fetching, and by reducing
the cache name to a sanitised basename prefixed with the content hash. Host
matching parses the URL rather than matching substrings, so
`https://cdn.modrinth.com@evil.example.com/x` is correctly refused.

## Deliberate design decisions

**Hash verification is mandatory, not optional.** A resolved mod without a
sha512 is refused rather than downloaded unverified. Two runs whose mod hashes
differ did not measure the same software, whatever their version strings said —
so this is a correctness property as much as a security one.

**Credentials are never handled.** Mojang authentication belongs to HeadlessMC.
mcbench never reads, stores, or forwards a token, and the preflight account check
only observes whether HeadlessMC has a session.

**No arbitrary code execution from configuration.** Suites are TOML, scenarios
are JSON, plans are properties files and command lists. Nothing is `eval`'d, and
no format supports references, includes, or object instantiation.

**Downloads land outside version control.** Everything fetched goes under a
gitignored working directory, so a jar cannot be committed by accident — which is
a licensing requirement as well (see [LICENSING.md](LICENSING.md)).

## Reporting a vulnerability

Open a private security advisory on the repository rather than a public issue.
Include the version, a reproduction, and what an attacker gains.

Findings that would let a **scenario, suite, or result document** cause
unintended execution, network access, or filesystem writes are in scope and
treated as high severity — those are the inputs users are encouraged to share.

A malicious *mod jar* misbehaving when benchmarked is **not** in scope: running
it is the tool's purpose, and the sandboxing guidance above is the only real
answer. What *is* in scope is any path by which mcbench runs a jar the operator
did not choose — a resolver pointed at the wrong host, a hash check that can be
bypassed, a cache poisoned across suites.

## Checklist for contributors

- [ ] Does this read attacker-controlled bytes? Bound the read before making it.
- [ ] Does this build a filesystem path from external input? Reduce it to a
      sanitised basename.
- [ ] Does this fetch a URL from an API response? Validate scheme and host first.
- [ ] Does this emit a line into a delimited file? Reject the delimiter.
- [ ] Does it fail loudly? Silently skipping hostile input produces a result that
      looks valid, which is worse than an error.
