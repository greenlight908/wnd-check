"""Static gate: does a "what needs doing" check use the typed `wnd_check` SDK?

This is the commit-time twin of a board-time meta-check. `classify()` is meant to be the
ONE detector the estate shares — a pre-commit hook (this package ships `.pre-commit-hooks.yaml`
declaring `wnd-sdk-gate`), a cross-repo board check, and repo-local tests can all call it, so
"what counts as on-the-SDK" is defined in exactly one place and can't drift between them.

Why it exists: the hand-rolled style a check can regress to — build a dict and
`print(json.dumps(...))` by hand — bypasses the slim-OK whitelist and reserved-key guards the
SDK enforces for free, and has shipped schema-invalid board payloads before. The typed SDK
(`@check` + `Result`) makes those unrepresentable, but only for checks that actually use it;
this gate is what stops the *next* check from being hand-rolled again.

Detection is anchored on the STRUCTURAL form, not a bare substring: the SDK import must be a
real `from wnd_check import ...` / `import wnd_check` statement (line-anchored, so a comment or
docstring that merely *mentions* `wnd_check` can't satisfy it), and the decorator a real
`@check` / `@wnd_check.check`. A guard that greps for a word eventually matches that word in
prose — so match the shape of the thing, not the name of it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The SDK import as a real STATEMENT, line-anchored (`^[ \t]*`) so prose can't satisfy it.
_SDK_IMPORT_RE = re.compile(r"^[ \t]*(?:from[ \t]+wnd_check[ \t]+import|import[ \t]+wnd_check)\b", re.M)
# The `@check` / `@wnd_check.check` decorator as a real decorator line, likewise anchored.
_CHECK_DECORATOR_RE = re.compile(r"^[ \t]*@(?:wnd_check\.)?check\b", re.M)


def classify(source: str) -> str | None:
    """Return None if `source` is a check on the typed SDK (imports `wnd_check` AND carries an
    `@check` decorator); otherwise a human-readable reason naming which half is missing."""
    missing: list[str] = []
    if not _SDK_IMPORT_RE.search(source):
        missing.append("no `from wnd_check import`")
    if not _CHECK_DECORATOR_RE.search(source):
        missing.append("no `@check` decorator")
    return " + ".join(missing) if missing else None


def main(argv: list[str] | None = None) -> int:
    """pre-commit entry point: classify each path passed on argv, fail if any is hand-rolled.

    pre-commit invokes this with the staged check files that matched the hook's `files`
    pattern. Unreadable paths are skipped (a deleted-but-staged file shouldn't crash the hook).
    """
    parser = argparse.ArgumentParser(
        prog="wnd-sdk-gate",
        description="Fail if a what-needs-doing check does not use the typed wnd_check SDK.",
    )
    parser.add_argument("paths", nargs="*", help="check files to inspect (pre-commit passes these)")
    args = parser.parse_args(argv)

    offenders: list[tuple[str, str]] = []
    for path in args.paths:
        try:
            source = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        reason = classify(source)
        if reason is not None:
            offenders.append((path, reason))

    if not offenders:
        return 0

    print("These what-needs-doing checks are not on the typed wnd_check SDK:", file=sys.stderr)
    for path, reason in offenders:
        print(f"  {path}: {reason}", file=sys.stderr)
    print(
        "\nPort each to `@check` / `Result` — a hand-built dict + print(json.dumps(...)) bypasses the SDK's slim-OK whitelist and reserved-key guards. See the wnd-check README.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
