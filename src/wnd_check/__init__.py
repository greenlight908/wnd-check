"""wnd-check: a typed check SDK for "what-needs-doing"-style health checks.

The legacy way to write a check is to hand-build a dict, `print(json.dumps(...))`,
return `0 if ok else 1`, and stash metadata like `karenWorthy` in a `# [check-meta]`
comment block that a *second* consumer re-parses out of the source. Three smells:
untyped output, a binary ok/not-ok status, and metadata that lives in a comment.

This module is the first-class replacement (modelled on .NET `IHealthCheck` /
Spring Actuator `HealthIndicator` + Nagios status semantics). A check becomes:

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

`.run()` serialises to the schema-v2 JSON the runner ingests — the wire format is
the stable contract, not the Python API.

Deliberately staged:
  * `Status` carries a 3-state model (OK / DEGRADED / FAILED) plus SKIPPED, but
    the wire schema is still binary `ok`, so DEGRADED and FAILED both serialise
    to `ok: false` for now. A richer wire status is a later schema bump.
  * `tags` now travel on the wire (whitelisted for ok=true), consumed by the
    publisher pipeline's predicates — the analog of ASP.NET Core's
    `HealthCheckOptions.Predicate` / `IHealthCheckPublisher` filtering by
    `HealthCheckRegistration.Tags`. Vocabulary: `host:<name>`, `needs:<cap/cred>`,
    `kind:<domain>` (plus the always-present predicate inputs
    `category`/`karenWorthy`/`priority`/`ok`/`name`).
  * `karenWorthy` *is* a whitelisted key, so it travels in the JSON from day one.

`Result` carries per-outcome overrides for `karen_worthy` and `priority` (both
`None` → inherit the static `CheckSpec`). They let one check rank its own
outcomes: a lingering PR stays at its usual priority, an unreachable upstream
escalates.
"""

from __future__ import annotations

import functools
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

SCHEMA_VERSION = "2.0.0"


# --- Address-family ordering (prefer IPv4) --------------------------------
#
# Python connects to resolved addresses **serially, in resolver order**, and the
# resolver returns AAAA (IPv6) records first. `socket.create_connection` — and so
# `requests`, `httplib2`/`googleapiclient`, and every check built on them — walks
# that list one address at a time. On a network that advertises IPv6 and then
# black-holes it, each dead AAAA costs a full TCP connect timeout (~28s observed).
# Four dead AAAAs ahead of the first working A record is ~112s of dead air, and a
# runner that kills the check at 60s never reaches the address that answers in
# 0.22s. Several healthy cloud services were reported red on exactly this.
#
# curl, browsers and git are structurally immune because they implement **Happy
# Eyeballs** (RFC 8305): they *race* the families concurrently and take whichever
# answers first, so a dead IPv6 path loses by ~250ms and is never noticed. That is
# also why every hand-probe of the broken network came back clean, and why the bug
# was invisible until reproduced inside Python.
#
# We take the cheaper, deterministic half of the same idea — the "prefer A records"
# variant, equivalent to Node's `dns.setDefaultResultOrder("ipv4first")`: keep the
# resolution, just hand IPv4 back first. No threads, no races, no cancellation.
#   - Broken-IPv6 network: the working A record is tried first -> milliseconds.
#   - IPv6-only network:   the A records fail fast (no route -> immediate
#                          ENETUNREACH), then IPv6 is tried and works.
#   - Dual-stack network:  IPv4 is preferred. We lose IPv6's marginal benefits;
#                          in this environment that is a trade worth making, because
#                          a check that cannot report is worse than one that reports
#                          over IPv4.
#
# This is **opt-in**: importing this module has no side effects. Call
# `install_ipv4_preference()` once, early in your entrypoint (before any host is
# resolved), to enable it for the process. Call it before building API clients that
# resolve hosts at module scope — the reorder only affects resolutions after the call.
#
# NOTE: it cannot help a process that shells out to a subprocess (a separate Python
# process with its own socket module) — call it there too.


def _prefer_ipv4(getaddrinfo: Callable[..., list]) -> Callable[..., list]:
    """Wrap `getaddrinfo` so IPv4 results come first, preserving order within a family."""

    @functools.wraps(getaddrinfo)
    def ordered(*args: object, **kwargs: object) -> list:
        infos = getaddrinfo(*args, **kwargs)
        return sorted(infos, key=lambda info: info[0] is not socket.AF_INET)

    return ordered


def install_ipv4_preference() -> None:
    """Idempotently make this process resolve IPv4-first. See the note above."""
    if getattr(socket.getaddrinfo, "__wnd_ipv4_first__", False):
        return
    ordered = _prefer_ipv4(socket.getaddrinfo)
    ordered.__wnd_ipv4_first__ = True  # type: ignore[attr-defined]
    socket.getaddrinfo = ordered  # type: ignore[assignment]


# Categories the wire schema accepts (check-schema.json `category` enum).
_CATEGORIES = ("assertion", "other")

# Keys the schema permits on a slim ok=true v2 payload. Extra diagnostic keys
# (fix_hint, counts, etc.) are only legal on the ok=false path; emitting them on
# an ok=true check is a schema violation. The SDK enforces this so a check can't
# reintroduce the class of bug where a green check carried diagnostic keys it
# shouldn't.
_SLIM_OK_KEYS = frozenset(
    {
        "action",
        "category",
        "emoji",
        "hold_reason",
        "hold_until",
        "id",
        "items",
        "karenWorthy",
        "message",
        "name",
        "ok",
        "on_hold",
        "priority",
        "runbook",
        "schemaVersion",
        "skipped",
        "tags",
        "url",
    }
)


class Status(StrEnum):
    """Check outcome. Nagios-style, with an explicit SKIPPED for "couldn't run".

    `is_actionable` is the single source of truth for what counts as needing
    attention: OK and SKIPPED do not, DEGRADED and FAILED do. The wire `ok`
    boolean is simply `not is_actionable`.
    """

    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def is_actionable(self) -> bool:
        return self in (Status.DEGRADED, Status.FAILED)


@dataclass(frozen=True)
class CheckSpec:
    """Static metadata about a check — its identity, not its outcome.

    `id` is the *stable correlation slug* — the identity the board keys on across
    machines and runs. `name` is the mutable display label. They default to the
    same value, so renaming `name` for clarity doesn't sever a check's history as
    long as `id` is pinned. (Same split as a Sentry fingerprint vs. its title, or
    a Prometheus metric name vs. a human label.)
    """

    name: str
    emoji: str
    priority: int
    category: str = "assertion"
    karen_worthy: bool = False
    tags: tuple[str, ...] = ()
    id: str = ""
    runbook: str = ""

    def __post_init__(self) -> None:
        if self.category not in _CATEGORIES:
            raise ValueError(f"category must be one of {_CATEGORIES}, got {self.category!r}")
        if self.priority < 1:
            raise ValueError(f"priority must be >= 1, got {self.priority}")
        if not self.emoji:
            raise ValueError("emoji is required")
        # Default the stable id to the display name (frozen dataclass → setattr).
        if not self.id:
            object.__setattr__(self, "id", self.name)


@dataclass
class Result:
    """The outcome of running a check. Build via the classmethods, not directly.

    `items`/`action` are *display* payloads, not diagnostics: they carry the
    content a green check legitimately shows (today's calendar events, pending
    downloads, a suggested command) and are both whitelisted on the slim ok=true
    path (see `_SLIM_OK_KEYS` + check-schema.json). They're distinct from `data`,
    which is arbitrary diagnostics only legal on the actionable ok=false path.
    """

    status: Status
    summary: str
    fix_hint: str | None = None
    url: str | None = None
    items: list | None = None
    action: dict | None = None
    # Per-outcome escalation override. None → inherit the check's static
    # `karen_worthy` (the common case). Set True on a specific actionable result
    # to page only on that tier — the analog of a monitor that warns on the board
    # but pages only at the critical threshold (e.g. a memory check: warn at <4GB,
    # page at <2GB). The paging publisher escalates on `karenWorthy and not ok`.
    karen_worthy: bool | None = None
    # Per-outcome board-ordering override, same None-inherits shape as
    # `karen_worthy`. A check whose severity depends on *which* assertion failed
    # ranks itself accordingly (a status check: a lingering PR sits at its usual
    # 60, an unreachable upstream jumps to 70 because it blocks the whole workflow).
    priority: int | None = None
    data: dict = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        summary: str,
        *,
        url: str | None = None,
        items: list | None = None,
        action: dict | None = None,
    ) -> Result:
        return cls(Status.OK, summary, url=url, items=items, action=action)

    @classmethod
    def degraded(
        cls,
        summary: str,
        *,
        fix_hint: str | None = None,
        url: str | None = None,
        items: list | None = None,
        action: dict | None = None,
        karen_worthy: bool | None = None,
        priority: int | None = None,
        data: dict | None = None,
    ) -> Result:
        return cls(
            Status.DEGRADED,
            summary,
            fix_hint=fix_hint,
            url=url,
            items=items,
            action=action,
            karen_worthy=karen_worthy,
            priority=priority,
            data=data or {},
        )

    @classmethod
    def failed(
        cls,
        summary: str,
        *,
        fix_hint: str | None = None,
        url: str | None = None,
        items: list | None = None,
        action: dict | None = None,
        karen_worthy: bool | None = None,
        priority: int | None = None,
        data: dict | None = None,
    ) -> Result:
        return cls(
            Status.FAILED,
            summary,
            fix_hint=fix_hint,
            url=url,
            items=items,
            action=action,
            karen_worthy=karen_worthy,
            priority=priority,
            data=data or {},
        )

    @classmethod
    def skipped(
        cls,
        summary: str,
        *,
        url: str | None = None,
        items: list | None = None,
        action: dict | None = None,
    ) -> Result:
        return cls(Status.SKIPPED, summary, url=url, items=items, action=action)

    def to_dict(self, spec: CheckSpec) -> dict:
        """Serialise to a schema-v2 payload, merging in the check's static spec.

        Slim when not actionable (only whitelisted keys); rich on the actionable
        path (fix_hint + arbitrary `data` allowed). DEGRADED and FAILED both
        serialise to `ok: false` until the wire schema grows a real status.
        """
        actionable = self.status.is_actionable
        if self.priority is not None and self.priority < 1:
            raise ValueError(f"priority must be >= 1, got {self.priority}")
        # Per-outcome override wins over the static spec default (None → inherit).
        effective_priority = self.priority if self.priority is not None else spec.priority
        payload: dict = {
            "schemaVersion": SCHEMA_VERSION,
            "name": spec.name,
            "id": spec.id,
            "emoji": spec.emoji,
            "category": spec.category,
            "priority": effective_priority,
            "ok": not actionable,
            "message": self.summary,
        }
        # Per-outcome override wins over the static spec default (None → inherit).
        effective_karen = self.karen_worthy if self.karen_worthy is not None else spec.karen_worthy
        if effective_karen:
            payload["karenWorthy"] = True
        if spec.tags:
            # Routing tags travel on BOTH the slim (ok=true) and actionable
            # paths — the publisher pipeline routes green checks too, so `tags`
            # is whitelisted for ok=true (see _SLIM_OK_KEYS + the schema).
            payload["tags"] = list(spec.tags)
        if spec.runbook:
            # Static playbook pointer — travels on every outcome (green too), so
            # the board can link the fix doc whether the check is passing or not.
            payload["runbook"] = spec.runbook
        if self.status is Status.SKIPPED:
            payload["skipped"] = True
        if self.url:
            payload["url"] = self.url
        # Display payloads — whitelisted on every path (slim ok=true included),
        # so a green check can carry its content (events, downloads, a suggested
        # action) without tripping the slim-OK guard.
        if self.items is not None:
            payload["items"] = self.items
        if self.action is not None:
            payload["action"] = self.action
        if actionable:
            # Diagnostics are only schema-legal on the ok=false path.
            if self.fix_hint:
                payload["fix_hint"] = self.fix_hint
            # `data` must not be able to clobber a computed key (e.g. a stray
            # data={"ok": True} on a failure). Fail loud rather than silently
            # emit a contradictory payload — the SDK's value is guarantees you
            # can't violate by accident.
            clashes = set(self.data) & set(payload)
            if clashes:
                raise ValueError(f"Result.data may not override reserved keys: {sorted(clashes)}")
            payload.update(self.data)
        return payload


class Check:
    """A check function bound to its spec. Returned by the `@check` decorator.

    Callable (returns the `Result` for in-process use, e.g. tests) and exposes
    `.run()` for the standalone `__main__` entry point.
    """

    def __init__(self, fn: Callable[[], Result], spec: CheckSpec) -> None:
        self._fn = fn
        self.spec = spec
        functools.update_wrapper(self, fn)

    def __call__(self) -> Result:
        return self._fn()

    def run(self) -> int:
        """Run the check, print its schema-v2 JSON, return a shell exit code."""
        result = self._fn()
        if not isinstance(result, Result):
            raise TypeError(f"check {self.spec.name!r} must return a Result, got {type(result).__name__}")
        print(json.dumps(result.to_dict(self.spec)))
        return 1 if result.status.is_actionable else 0


def check(
    *,
    name: str,
    emoji: str,
    priority: int,
    category: str = "assertion",
    karen_worthy: bool = False,
    tags: tuple[str, ...] | list[str] = (),
    id: str = "",
    runbook: str = "",
) -> Callable[[Callable[[], Result]], Check]:
    """Decorate a `() -> Result` function as a check.

    `id` is the stable correlation slug; omit it to default to `name`. Pin it
    explicitly when you expect to reword the display `name` later. `runbook` is a
    repo-relative path to the check's fix playbook (e.g. docs/runbooks/<id>.md),
    which the board links to the source blob.
    """

    def decorator(fn: Callable[[], Result]) -> Check:
        spec = CheckSpec(
            name=name,
            emoji=emoji,
            priority=priority,
            category=category,
            karen_worthy=karen_worthy,
            tags=tuple(tags),
            id=id,
            runbook=runbook,
        )
        return Check(fn, spec)

    return decorator
