# wnd-check

A small, **zero-dependency** typed SDK for writing health checks — the kind that answer
"what needs doing?" on a status board. Modelled on .NET's `IHealthCheck` / Spring
Actuator's `HealthIndicator` plus Nagios status semantics.

A check is a `() -> Result` function decorated with `@check`. Running it prints one
schema-stable JSON line and exits `0` (fine) or `1` (needs attention) — so a check is
both an importable function (for tests) and a standalone script (for a runner).

## Install

```bash
uv add wnd-check
# or, pinned to a tag straight from git:
uv add "wnd-check @ git+https://github.com/greenlight908/wnd-check@v0.0.1"
```

## Quickstart

```python
from wnd_check import Result, check


@check(name="cpu-load", emoji="🔥", priority=80, karen_worthy=True,
       tags=["host:web01", "needs:proc"])
def cpu_load() -> Result:
    if cannot_sample():
        return Result.skipped("Could not sample /proc")
    if runaway:
        return Result.failed("🔥 runaway pid 123", fix_hint="kill -9 123",
                             data={"processes": [...]})
    return Result.ok("no runaway CPU")


if __name__ == "__main__":
    import sys
    sys.exit(cpu_load.run())
```

## Model

- **`Result.ok` / `Result.skipped`** — not actionable. Serialise to a **slim** `ok: true`
  payload: only a whitelisted set of keys is allowed, so a green check can't smuggle
  diagnostics onto the board.
- **`Result.degraded` / `Result.failed`** — actionable (`ok: false`). May carry
  `fix_hint` and arbitrary `data` diagnostics.
- **`items` / `action`** — *display* payloads (e.g. today's events, a suggested command),
  whitelisted on every path including green.
- **`tags`** — routing labels (`host:<name>`, `needs:<cap>`, `kind:<domain>`) that a
  publisher pipeline can filter on, the analog of ASP.NET Core's
  `HealthCheckRegistration.Tags`.
- **`karen_worthy`** — an escalation flag (paging vs board-only). Overridable per outcome,
  so one check can page only at its critical tier.

The **wire format** (schema-v2 JSON) is the stable contract, not the Python API. `.run()`
serialises to it; DEGRADED and FAILED both collapse to `ok: false` until the wire schema
grows a richer status.

## Networking note

Importing this module installs an **IPv4-first** address-resolution preference
(`install_ipv4_preference`). On a network that advertises IPv6 and then black-holes it,
Python's serial, AAAA-first connection walk burns a full TCP timeout per dead address; a
check on a short runner budget never reaches the A record that answers. Reordering (not
racing — the deterministic half of Happy Eyeballs) tries the working address first. See
the module docstring for the full rationale.
