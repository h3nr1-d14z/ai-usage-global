#!/usr/bin/env python3
"""Permanent regression for `engine --add-key NAME` (the panel's Save button).

Contract pinned here:
  - fresh add creates ~/.config/ai-usage/env with 0600
  - replace drops the old assignment and appends the new one (no dupes)
  - merge is LINE-based: comments/blank lines/other keys/order preserved
  - bad names (lowercase, spaces, too short) and empty values rejected,
    exit 1, previous file byte-identical (tmp+replace, no truncation)
  - multi-line paste: only the first line is stored (readline semantics);
    nothing from line 2+ can be injected into the file
  - the value never appears in the command's stdout

Run: python3 tests/test_add_key.py
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine/usage.py"

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="addkey-"))
    try:
        env = dict(os.environ)
        env["AIUSAGE_HOME"] = str(tmp)
        env_path = tmp / ".config/ai-usage/env"

        def addk(name: str, stdin_text: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, str(ENGINE), "--add-key", name],
                input=stdin_text, capture_output=True, text=True, env=env)

        # fresh add + perms ---------------------------------------------------- #
        r = addk("OPENROUTER_API_KEY", "sk-or-v1-aaa\n")
        check("fresh add exits 0", r.returncode == 0, r.stdout + r.stderr[-200:])
        check("fresh add result ok", json.loads(r.stdout).get("ok") is True, r.stdout)
        check("fresh add content", env_path.read_text() == "OPENROUTER_API_KEY=sk-or-v1-aaa\n",
              repr(env_path.read_text()))
        check("fresh add perms 0600",
              stat.S_IMODE(env_path.stat().st_mode) == 0o600,
              oct(env_path.stat().st_mode))

        # replace in place ----------------------------------------------------- #
        addk("KIMI_API_KEY", "sk-kimi-bbb\n")
        addk("OPENROUTER_API_KEY", "sk-or-v1-ccc\n")
        text = env_path.read_text()
        check("replace: no duplicate assignment",
              text.count("OPENROUTER_API_KEY=") == 1, repr(text))
        check("replace: new value stored", "OPENROUTER_API_KEY=sk-or-v1-ccc" in text, repr(text))
        check("replace: other keys untouched", "KIMI_API_KEY=sk-kimi-bbb" in text, repr(text))

        # line-based merge preserves comments/order ---------------------------- #
        env_path.write_text("# my keys\nOPENROUTER_API_KEY=old\n\n# below\nZAI_API_KEY=z-old\n")
        addk("OPENROUTER_API_KEY", "new")
        text = env_path.read_text()
        # Line-based merge drops the replaced assignment and appends the new
        # one at the end; comments, blanks, and other keys keep their order.
        check("merge: comments and blanks preserved",
              text == "# my keys\n\n# below\nZAI_API_KEY=z-old\nOPENROUTER_API_KEY=new\n",
              repr(text))

        # rejections leave the file byte-identical ----------------------------- #
        before = env_path.read_bytes()
        for bad_name in ("lowercase_key", "has space", "X"):
            r = addk(bad_name, "value")
            check(f"reject name {bad_name!r}",
                  r.returncode == 1 and json.loads(r.stdout).get("ok") is False, r.stdout)
        r = addk("OK_KEY", "\n")
        check("reject empty value", r.returncode == 1, r.stdout)
        r = addk("OK_KEY", "")  # EOF, no line at all
        check("reject empty stdin keeps file intact",
              r.returncode == 1 and env_path.read_bytes() == before,
              f"rc={r.returncode} stdout={r.stdout}")

        # multi-line paste: first line only, no injection ---------------------- #
        addk("DEEPSEEK_API_KEY", "sk-first\nINJECTED=evil\n")
        text = env_path.read_text()
        check("multiline: first line stored", "DEEPSEEK_API_KEY=sk-first\n" in text, repr(text))
        check("multiline: nothing injected", "INJECTED" not in text, repr(text))

        # value never echoed ---------------------------------------------------- #
        r = addk("GITHUB_TOKEN", "gho_supersecretvalue123")
        check("no echo of value", "supersecretvalue" not in r.stdout, r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nadd_key regression: ALL PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
