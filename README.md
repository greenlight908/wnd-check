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
- **`Result.degraded` / `Result.failed`** — a *problem* (`ok: false`). May carry
  `fix_hint` and arbitrary `data` diagnostics; sets a failing exit code.
- **`Result.opportunity`** — `ok: false`, but **not a failure**: "nothing is broken,
  there's something worth your time." Buckets separately (`kind: opportunity`, so it
  doesn't inflate the count of things to fix) and is ranked by **`leverage`** (value) —
  a distinct axis from `priority` (urgency). Exits `0`.
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

## Enforcing the SDK (pre-commit hook)

Using `@check` is the *right* way to write a check, but nothing stops the next one being
hand-rolled (a dict + `print(json.dumps(...))`), which bypasses the slim-OK whitelist and
reserved-key guards the SDK enforces for free. This package ships a pre-commit hook that
fails a commit introducing a hand-rolled check — enable it in a consumer repo:

```yaml
# .pre-commit-config.yaml
- repo: https://github.com/greenlight908/wnd-check
  rev: v0.3.0
  hooks:
    - id: wnd-sdk-gate
```

It runs on `scripts/what-needs-doing/**/NN-*.py` and fails any that lacks **both** a real
`from wnd_check import` statement **and** an `@check` decorator (matched structurally, so a
docstring that merely *mentions* them doesn't count). The same detector is exposed as
`wnd_check.gate.classify(source) -> str | None`, so a board-time meta-check and repo-local
tests can share one definition of "on the SDK" — the commit-time and board-time gates then
can't drift apart.
