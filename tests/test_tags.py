"""Tags serialise on the wire and stay within the slim-OK whitelist."""

from __future__ import annotations

from wnd_check import _SLIM_OK_KEYS, Result, check


@check(name="tagged", emoji="🏷️", priority=50, tags=["host:web01", "kind:cpu"])
def _ok_check() -> Result:
    return Result.ok("fine")


@check(name="tagged-fail", emoji="🏷️", priority=50, tags=["host:web01"])
def _failing_check() -> Result:
    return Result.failed("broke", fix_hint="fix it", data={"count": 1})


@check(name="untagged", emoji="✅", priority=50)
def _untagged_check() -> Result:
    return Result.ok("fine")


def test_tags_serialise_on_ok_true() -> None:
    out = _ok_check().to_dict(_ok_check.spec)
    assert out["ok"] is True
    assert out["tags"] == ["host:web01", "kind:cpu"]
    # Slim contract: every emitted key must be whitelisted on ok=true.
    assert set(out) <= _SLIM_OK_KEYS


def test_tags_in_slim_whitelist() -> None:
    assert "tags" in _SLIM_OK_KEYS


def test_tags_serialise_on_ok_false() -> None:
    out = _failing_check().to_dict(_failing_check.spec)
    assert out["ok"] is False
    assert out["tags"] == ["host:web01"]
    assert out["fix_hint"] == "fix it"
    assert out["count"] == 1


def test_untagged_check_has_no_tags_key() -> None:
    out = _untagged_check().to_dict(_untagged_check.spec)
    assert "tags" not in out
    assert set(out) <= _SLIM_OK_KEYS
