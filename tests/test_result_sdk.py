"""Unit tests for the typed check SDK.

The real contract: status mapping, slim-OK whitelist enforcement, karenWorthy
serialisation, per-outcome overrides, and the decorator's exit codes. All pure,
network-less, and dependency-free.
"""

from __future__ import annotations

import json

import pytest

from wnd_check import _SLIM_OK_KEYS, CheckSpec, Result, Status, check

SPEC = CheckSpec(name="demo", emoji="✅", priority=50)
KAREN_SPEC = CheckSpec(name="demo", emoji="🔥", priority=80, karen_worthy=True)


def test_status_actionability() -> None:
    assert not Status.OK.is_actionable
    assert not Status.SKIPPED.is_actionable
    assert Status.DEGRADED.is_actionable
    assert Status.FAILED.is_actionable


def test_ok_payload_is_slim() -> None:
    payload = Result.ok("all good").to_dict(SPEC)
    assert payload["ok"] is True
    assert payload["message"] == "all good"
    assert payload["schemaVersion"] == "2.0.0"
    # No diagnostic keys may leak onto a slim ok=true payload.
    assert set(payload) <= _SLIM_OK_KEYS
    assert "fix_hint" not in payload
    assert "status" not in payload
    assert "tags" not in payload


def test_skipped_payload_is_slim() -> None:
    payload = Result.skipped("offline").to_dict(SPEC)
    assert payload["ok"] is True
    assert payload["skipped"] is True
    assert set(payload) <= _SLIM_OK_KEYS


def test_failed_payload_is_rich() -> None:
    payload = Result.failed("boom", fix_hint="turn it off and on", data={"processes": [{"pid": 1}]}).to_dict(SPEC)
    assert payload["ok"] is False
    assert payload["fix_hint"] == "turn it off and on"
    assert payload["processes"] == [{"pid": 1}]


def test_degraded_serialises_to_not_ok() -> None:
    # Staged: DEGRADED has no wire representation yet, collapses to ok=false.
    payload = Result.degraded("slow", data={"latency_ms": 900}).to_dict(SPEC)
    assert payload["ok"] is False
    assert payload["latency_ms"] == 900


def test_karen_worthy_travels_in_json() -> None:
    payload = Result.ok("fine").to_dict(KAREN_SPEC)
    assert payload["karenWorthy"] is True
    assert set(payload) <= _SLIM_OK_KEYS  # karenWorthy is whitelisted
    # Not karen-worthy → key absent entirely (not False).
    assert "karenWorthy" not in Result.ok("fine").to_dict(SPEC)


def test_data_cannot_clobber_reserved_keys() -> None:
    # A stray data={"ok": True} on a failure must not silently emit a
    # contradictory payload — it should fail loud at serialisation.
    with pytest.raises(ValueError):
        Result.failed("boom", data={"ok": True}).to_dict(SPEC)
    with pytest.raises(ValueError):
        Result.failed("boom", data={"message": "sneaky"}).to_dict(SPEC)


def test_url_emitted_when_set() -> None:
    payload = Result.ok("fine", url="https://example.com").to_dict(SPEC)
    assert payload["url"] == "https://example.com"


def test_id_defaults_to_name() -> None:
    # An unset id mirrors the display name, so existing checks correlate by name.
    spec = CheckSpec(name="disk-space", emoji="💽", priority=60)
    assert spec.id == "disk-space"
    payload = Result.ok("fine").to_dict(spec)
    assert payload["id"] == "disk-space"
    assert payload["name"] == "disk-space"


def test_explicit_id_decouples_from_name() -> None:
    # Pinning id lets the display name be reworded without severing history.
    spec = CheckSpec(name="Disk space", emoji="💽", priority=60, id="disk-space")
    payload = Result.ok("fine").to_dict(spec)
    assert payload["id"] == "disk-space"
    assert payload["name"] == "Disk space"


def test_id_is_slim_whitelisted() -> None:
    payload = Result.ok("fine").to_dict(CheckSpec(name="x", emoji="✅", priority=1, id="x-slug"))
    assert "id" in payload
    assert set(payload) <= _SLIM_OK_KEYS


def test_runbook_emitted_when_set_and_slim() -> None:
    # runbook is static per check → travels on every outcome, including green.
    spec = CheckSpec(name="x", emoji="✅", priority=1, runbook="docs/runbooks/x.md")
    payload = Result.ok("fine").to_dict(spec)
    assert payload["runbook"] == "docs/runbooks/x.md"
    assert set(payload) <= _SLIM_OK_KEYS  # runbook is whitelisted


def test_runbook_absent_when_unset() -> None:
    assert "runbook" not in Result.ok("fine").to_dict(SPEC)


def test_items_on_green_stay_slim() -> None:
    # `items` is a display payload (e.g. today's calendar events), whitelisted on
    # the slim ok=true path — a green check may carry it without a violation.
    events = [{"summary": "Standup", "start": "09:00"}]
    payload = Result.ok("2 events today", items=events).to_dict(SPEC)
    assert payload["ok"] is True
    assert payload["items"] == events
    assert set(payload) <= _SLIM_OK_KEYS


def test_action_on_green_stay_slim() -> None:
    # `action` (a suggested command) is whitelisted on green too.
    action = {"type": "suggested", "command": "do-the-thing", "description": "Do the thing"}
    payload = Result.ok("3 waiting", action=action).to_dict(SPEC)
    assert payload["ok"] is True
    assert payload["action"] == action
    assert set(payload) <= _SLIM_OK_KEYS


def test_items_action_absent_when_unset() -> None:
    payload = Result.ok("fine").to_dict(SPEC)
    assert "items" not in payload
    assert "action" not in payload


def test_items_travel_on_skipped_and_failed() -> None:
    # Attachable on every constructor, not just ok().
    skipped = Result.skipped("offline", items=[{"n": 1}]).to_dict(SPEC)
    assert skipped["items"] == [{"n": 1}]
    assert set(skipped) <= _SLIM_OK_KEYS
    failed = Result.failed("boom", items=[{"n": 2}], data={"count": 1}).to_dict(SPEC)
    assert failed["ok"] is False
    assert failed["items"] == [{"n": 2}]


def test_karen_worthy_override_escalates_on_result() -> None:
    # A check that is NOT karen-worthy by default can page on a specific tier
    # (e.g. critical) via a per-Result override.
    warn = Result.degraded("warn").to_dict(SPEC)  # SPEC is not karen_worthy
    assert "karenWorthy" not in warn
    crit = Result.failed("critical", karen_worthy=True).to_dict(SPEC)
    assert crit["karenWorthy"] is True


def test_karen_worthy_override_can_deescalate() -> None:
    # Inverse: a karen-worthy check can suppress paging on a soft outcome.
    soft = Result.degraded("soft", karen_worthy=False).to_dict(KAREN_SPEC)
    assert "karenWorthy" not in soft
    # Unset override still inherits the spec default.
    hard = Result.failed("hard").to_dict(KAREN_SPEC)
    assert hard["karenWorthy"] is True


def test_data_cannot_clobber_items() -> None:
    # items set both first-class and via data on the actionable path is a
    # contradiction — fail loud rather than silently pick one.
    with pytest.raises(ValueError):
        Result.failed("boom", items=[{"n": 1}], data={"items": [{"n": 2}]}).to_dict(SPEC)


def test_spec_validation() -> None:
    with pytest.raises(ValueError):
        CheckSpec(name="x", emoji="✅", priority=0)  # priority must be >= 1
    with pytest.raises(ValueError):
        CheckSpec(name="x", emoji="✅", priority=1, category="bogus")
    with pytest.raises(ValueError):
        CheckSpec(name="x", emoji="", priority=1)


def test_decorator_exit_codes_and_print(capsys: pytest.CaptureFixture[str]) -> None:
    @check(name="green", emoji="✅", priority=10)
    def green() -> Result:
        return Result.ok("yep")

    @check(name="red", emoji="🚨", priority=90)
    def red() -> Result:
        return Result.failed("nope", fix_hint="fix it")

    assert green.run() == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok == {
        "schemaVersion": "2.0.0",
        "name": "green",
        "id": "green",  # defaults to name
        "emoji": "✅",
        "category": "assertion",
        "priority": 10,
        "ok": True,
        "message": "yep",
    }

    assert red.run() == 1
    out_bad = json.loads(capsys.readouterr().out)
    assert out_bad["ok"] is False
    assert out_bad["fix_hint"] == "fix it"

    # Callable for in-process use returns the Result without printing.
    assert green().status is Status.OK


def test_decorator_rejects_non_result() -> None:
    @check(name="bad", emoji="✅", priority=10)
    def bad() -> Result:
        return {"ok": True}  # type: ignore[return-value]

    with pytest.raises(TypeError):
        bad.run()
