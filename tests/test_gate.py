"""Tests for the SDK gate (classify + the pre-commit CLI).

Two things must hold and both are proven to fail loudly: a hand-rolled check (dict +
print(json.dumps(...)), no SDK) is flagged, and a real SDK check is not. Plus the control
that keeps it honest — a docstring that DESCRIBES the anti-pattern must not be mistaken for
it ("match the shape, not the name").
"""

from __future__ import annotations

from pathlib import Path

from wnd_check.gate import classify, main

SDK_CHECK = '#!/usr/bin/env python3\nimport sys\nfrom wnd_check import Result, check\n\n@check(name="demo", emoji="OK", priority=50)\ndef demo() -> Result:\n    return Result.ok("fine")\n\nif __name__ == "__main__":\n    sys.exit(demo.run())\n'

HAND_ROLLED = '#!/usr/bin/env python3\nimport json\nprint(json.dumps({"schemaVersion": "2.0.0", "name": "demo", "ok": True}))\n'


# --- classify --------------------------------------------------------------------


def test_accepts_a_real_sdk_check() -> None:
    assert classify(SDK_CHECK) is None


def test_flags_a_hand_rolled_check() -> None:
    reason = classify(HAND_ROLLED)
    assert reason is not None
    assert "wnd_check" in reason and "@check" in reason


def test_is_not_fooled_by_prose() -> None:
    """A comment/docstring mentioning wnd_check or @check must NOT satisfy the gate."""
    prose = '#!/usr/bin/env python3\n"""This check does NOT use wnd_check yet; port it to @check later."""\nimport json\nprint(json.dumps({"ok": True}))\n'
    assert classify(prose) is not None


def test_accepts_dotted_decorator() -> None:
    dotted = "import wnd_check\n\n@wnd_check.check(name='d', emoji='x', priority=1)\ndef d():\n    ...\n"
    assert classify(dotted) is None


def test_reports_missing_decorator_only() -> None:
    imported = "from wnd_check import Result\nresult = Result.ok('x')\n"
    assert classify(imported) == "no `@check` decorator"


def test_reports_missing_import_only() -> None:
    decorated = "@check(name='d', emoji='x', priority=1)\ndef d():\n    ...\n"
    assert classify(decorated) == "no `from wnd_check import`"


# --- the CLI (pre-commit entry point) --------------------------------------------


def test_main_passes_on_sdk_checks(tmp_path: Path) -> None:
    good = tmp_path / "01-good.py"
    good.write_text(SDK_CHECK)
    assert main([str(good)]) == 0


def test_main_fails_on_a_hand_rolled_check(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "02-bad.py"
    bad.write_text(HAND_ROLLED)
    assert main([str(bad)]) == 1
    assert "02-bad.py" in capsys.readouterr().err


def test_main_fails_if_any_of_many_is_hand_rolled(tmp_path: Path, capsys) -> None:
    good = tmp_path / "01-good.py"
    good.write_text(SDK_CHECK)
    bad = tmp_path / "02-bad.py"
    bad.write_text(HAND_ROLLED)
    assert main([str(good), str(bad)]) == 1
    err = capsys.readouterr().err
    assert "02-bad.py" in err and "01-good.py" not in err


def test_main_skips_unreadable_paths(tmp_path: Path) -> None:
    """A staged-but-deleted file (path no longer on disk) must not crash the hook."""
    assert main([str(tmp_path / "gone.py")]) == 0


def test_main_with_no_paths_is_a_noop() -> None:
    assert main([]) == 0
