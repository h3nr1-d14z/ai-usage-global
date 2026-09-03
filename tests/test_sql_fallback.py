#!/usr/bin/env python3
"""Regression: the guarded fast-path query must never double-count.

scan_opencode runs a fast GROUP BY query without json_valid; one malformed
row aborts it (fetchall raises before ANY row reaches Python) and a fallback
rescan WITH json_valid takes over. If that abort ever happened mid-consumption,
the retry would re-fold rows already added to the accumulators — this test
pins the exact aggregates for a corrupt row placed MID-TABLE with valid rows
before and after it, plus the clean fast path, so both branches are covered.

Runnable standalone: python3 tests/test_sql_fallback.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
os.environ.setdefault("AIUSAGE_NOW", "1788681600000")
import usage  # noqa: E402

NOW = 1788681600000  # ms, fixed


def make_db(path: str, rows: list[tuple]) -> None:
    if os.path.exists(path):
        os.unlink(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,"
                 " time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.executemany("INSERT INTO message VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def assistant(row_id: str, model: str, inp: int, out: int) -> tuple:
    data = json.dumps({"role": "assistant", "modelID": model,
                       "tokens": {"input": inp, "output": out}, "cost": 0.1},
                      separators=(",", ":"))
    return (row_id, "s1", NOW - 3600_000, NOW - 3600_000, data)


def user_row(row_id: str) -> tuple:
    data = json.dumps({"role": "user", "modelID": "x"}, separators=(",", ":"))
    return (row_id, "s1", NOW - 3600_000, NOW - 3600_000, data)


CORRUPT = ("m99", "s1", NOW - 3600_000, NOW - 3600_000,
           '{"role":"assistant","modelID":"boom","tokens":{tru')


def scan(db_path: str):
    home = Path(db_path).parent
    ctx = {"nowMs": NOW, "caps": {}, "home": home}
    os.environ["OPENCODE_DB"] = db_path
    days = usage.DayIndex(NOW)
    models, dtok, dreq = {}, [0] * 7, [0] * 7
    src = usage.scan_opencode(ctx, models, days, dtok, dreq)
    return src, models


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aiusage-fallback-")
    db = os.path.join(tmp, "opencode.db")
    failures = []

    def expect(label: str, cond: bool, detail=""):
        print(f"[{'ok  ' if cond else 'FAIL'}] {label}" + ("" if cond else f" — {detail}"))
        if not cond:
            failures.append(label)

    # 1. Clean DB: fast path serves it, exact numbers.
    clean = ([assistant(f"a{i}", "fast-model", 100 + i, 10) for i in range(50)]
             + [user_row("u1"), user_row("u2")])
    make_db(db, clean)
    src, models = scan(db)
    expect("clean fast path: 50 requests", src and src["requests"] == 50, str(src))
    b = models.get("fast-model", {})
    expect("clean fast path: tokens exact",
           b.get("inputTokens") == sum(100 + i for i in range(50))
           and b.get("outputTokens") == 500, str(b))

    # 2. Corrupt row MID-TABLE: 50 valid assistants before it, 40 after,
    #    one user row. The abort (if it were mid-consumption) or a partial
    #    fold would show as != 90 / != 2 user-rejected / double-counted sums.
    mid = ([assistant(f"a{i}", "mix-model", 10, 1) for i in range(50)]
           + [CORRUPT]
           + [assistant(f"b{i}", "mix-model", 10, 1) for i in range(40)]
           + [user_row("u1")])
    make_db(db, mid)
    src, models = scan(db)
    expect("mid-table corrupt: fallback exact 90 requests",
           src and src["requests"] == 90, str(src))
    b = models.get("mix-model", {})
    expect("mid-table corrupt: no double-count (900 input tokens)",
           b.get("inputTokens") == 900 and b.get("requests") == 90, str(b))
    expect("mid-table corrupt: corrupt model never appears",
           "boom" not in models, str(sorted(models)))
    days = usage.DayIndex(NOW)
    dtok2, dreq2 = [0] * 7, [0] * 7
    ctx2 = {"nowMs": NOW, "caps": {}, "home": Path(db).parent}
    usage.scan_opencode(ctx2, {}, days, dtok2, dreq2)
    expect("mid-table corrupt: today bucket = all 90 requests",
           dreq2[6] == 90 and dtok2[6] == 990, f"dreq={dreq2} dtok={dtok2}")

    # 3. Corrupt FIRST row (abort before any aggregation could occur).
    first = [CORRUPT] + [assistant(f"a{i}", "head-model", 5, 5) for i in range(10)]
    make_db(db, first)
    src, models = scan(db)
    expect("corrupt first row: exact 10 requests",
           src and src["requests"] == 10, str(src))

    # 4. Corrupt LAST row.
    last = [assistant(f"a{i}", "tail-model", 5, 5) for i in range(10)] + [CORRUPT]
    make_db(db, last)
    src, models = scan(db)
    expect("corrupt last row: exact 10 requests",
           src and src["requests"] == 10, str(src))

    print(f"\n{len(failures)} failure(s)" if failures else "\nSQL fallback regression: ALL PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
