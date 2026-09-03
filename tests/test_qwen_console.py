#!/usr/bin/env python3
"""Console-gateway branch of fetch_qwen, fully offline.

The corpus validate run must stay on the local census path (no cookie, no
agent.db); this file owns the console protocol ported from OMP
(pi-ai/src/usage/alibaba-token-plan.ts). It runs the real engine subprocess
against its OWN fixture dir + fake home, covering:

  1. international flow: QWEN_PLAN_COOKIE env → session secToken →
     gateway envelope {"Data": "<json string>"} → 5h/7d percents + resets
  2. OMP agent.db auto-detect: {"key":"<inner json>"} rows
  3. most-recent-row-wins, both directions (newest good → console; newest
     bare-key with an older cookie row → census, proving row choice)
  4. China region marker selects the DataV2-unwrapping parse (the HTML
     SEC_TOKEN session branch needs live network; envelope + selection are
     what we pin here)
  4b. live international envelope (2026-09-04): DataV2.data carries a
      msg/code envelope, usage nests one level deeper — plus week-only
      responses (fraction, not percent)
  5. cookie/secToken NEVER appear in the emitted document
  6. console failure (invalid session) → census fallback still exact

Run: python3 tests/test_qwen_console.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine/usage.py"
NOW_MS = 1788436800000  # frozen; matches corpus anchor (2026-09-03T12:00Z)

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def usage_body(p5, pw, r5, rw) -> dict:
    return {"per5HourPercentage": p5, "per5HourResetTime": r5,
            "per1WeekPercentage": pw, "per1WeekResetTime": rw}


def make_fixtures(d: Path, session: dict, usage: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "qwen.session.json").write_text(json.dumps(session))
    (d / "qwen.usage.json").write_text(json.dumps(usage))


def make_home(root: Path, rows: list | None = None,
              transcripts: bool = False) -> Path:
    """Fake home. `rows` = OMP auth_credentials entries in insertion order
    (oldest first); each is a cookie string or None (bare sk- key)."""
    root.mkdir(parents=True, exist_ok=True)
    if transcripts:
        chats = root / ".qwen/projects/-p/chats"
        chats.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"type": "assistant", "timestamp": ts,
                             "model": "qwen3.7-plus",
                             "usageMetadata": {"promptTokenCount": 10,
                                               "candidatesTokenCount": 2}})
                 for ts in ("2026-09-03T11:00:00Z", "2026-09-03T11:30:00Z")]
        (chats / "c.jsonl").write_text("\n".join(lines) + "\n")
    if rows is not None:
        agent = root / ".omp/agent"
        agent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(agent / "agent.db")
        conn.execute("CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY,"
                     " provider TEXT, credential_type TEXT, data TEXT,"
                     " disabled_cause TEXT, identity_key TEXT,"
                     " created_at INT, updated_at INT)")
        for i, cookie in enumerate(rows):
            if cookie is None:
                inner = json.dumps({"key": "sk-bare-no-cookie"})
            else:
                payload = {"token": "sk-fake-token", "cookie": cookie}
                if cookie.startswith("CN:"):
                    payload["cookie"] = cookie[3:]
                    payload["baseUrl"] = ("https://token-plan.cn-beijing"
                                          ".maas.aliyuncs.com/compatible-mode/v1")
                inner = json.dumps({"key": json.dumps(payload)})
            conn.execute("INSERT INTO auth_credentials VALUES (?,?,?,?,NULL,NULL,?,?)",
                         (i + 1, "alibaba-token-plan", "api_key", inner,
                          100 + i, 100 + i))
        conn.commit()
        conn.close()
    return root


def run_engine(home: Path, fixtures: Path, env_extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update({"AIUSAGE_NOW": str(NOW_MS), "AIUSAGE_HOME": str(home),
                "AIUSAGE_FIXTURES": str(fixtures), "TZ": "UTC",
                "PYTHONDONTWRITEBYTECODE": "1"})
    env.pop("AIUSAGE_ENV_FILE", None)
    for k, v in (env_extra or {}).items():
        env[k] = v
    proc = subprocess.run([sys.executable, str(ENGINE), "--settings",
                           json.dumps({"showLocalConsumption": False})],
                          capture_output=True, text=True, env=env, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"engine exit {proc.returncode}: {proc.stderr[-300:]}")
    return json.loads(proc.stdout)


def qwen_of(doc: dict) -> dict:
    return next(p for p in doc["providers"] if p["id"] == "qwen")


def wins_of(rec: dict) -> dict:
    return {x["id"]: x for x in rec["windows"]}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="qwconsole-"))
    try:
        intl_fix = tmp / "fx-intl"
        # 5h reset is a NUMERIC STRING (gateway is sloppy about types;
        # OMP's toNumber tolerates it), 7d reset plain epoch seconds.
        make_fixtures(intl_fix, {"data": {"secToken": "SECTOK", "accountId": "999"}},
                      {"data": {"Data": json.dumps(usage_body(
                          42.5, 71.25, str(NOW_MS + 3600_000),
                          (NOW_MS + 2 * 86400_000) // 1000))}})
        chn_fix = tmp / "fx-chn"
        # China gateway answers reset times as ISO strings, not epoch —
        # parse_ts_ms must accept both (shared with the transcript census).
        make_fixtures(chn_fix, {"data": {"secToken": "SECTOK2"}},
                      {"data": {"DataV2": {"data": usage_body(
                          10, 20, "2026-09-03T12:00:01Z", "2026-09-03T12:00:02Z")}}})

        # 1) env cookie → international console flow ------------------------- #
        doc = run_engine(make_home(tmp / "h-intl"),
                         intl_fix, {"QWEN_PLAN_COOKIE": "login_aliyunid_csrf=TOKcsrf; t=xyz"})
        rec = qwen_of(doc)
        w = wins_of(rec)
        check("env cookie: console 5h percent", w.get("rolling", {}).get("percent") == 42.5, str(w))
        check("env cookie: 7d half-up 71.3", w.get("weekly", {}).get("percent") == 71.3, str(w))
        check("env cookie: resets exact (numeric-string s + epoch-ms)",
              w.get("rolling", {}).get("resetsAt") == "2026-09-03T13:00:00Z"
              and w.get("weekly", {}).get("resetsAt") == "2026-09-05T12:00:00Z",
              str(w))
        check("env cookie: detail says console", rec["detail"].startswith("console"), rec["detail"])
        text = json.dumps(doc)
        check("secrets absent from document",
              all(s not in text for s in ("TOKcsrf", "SECTOK", "sk-fake-token",
                                          "login_aliyunid_csrf")))

        # 2) agent.db auto-detect (single cookie row) ------------------------ #
        doc = run_engine(make_home(tmp / "h-omp1", rows=["a=1; login_aliyunid_csrf=C2"]),
                         intl_fix)
        check("agent.db: auto-detected console mode",
              wins_of(qwen_of(doc)).get("rolling", {}).get("percent") == 42.5,
              qwen_of(doc)["detail"])

        # 3) most-recent-wins, both directions ------------------------------- #
        #    newest has the cookie, older is a bare key → console
        doc = run_engine(make_home(tmp / "h-new-good",
                                   rows=[None, "b=2; login_aliyunid_csrf=C3"]), intl_fix)
        check("newest row wins: good cookie → console",
              qwen_of(doc)["detail"].startswith("console"), qwen_of(doc)["detail"])
        #    newest is a bare key, older holds the cookie → census
        doc = run_engine(make_home(tmp / "h-new-bare",
                                   rows=["c=3; login_aliyunid_csrf=C4", None],
                                   transcripts=True), intl_fix)
        check("newest row wins: bare key → census",
              qwen_of(doc)["detail"].startswith("local"), qwen_of(doc)["detail"])

        # 4) China region marker → DataV2 envelope --------------------------- #
        doc = run_engine(make_home(tmp / "h-chn",
                                   rows=["CN:d=4; login_aliyunid_csrf=D5"]), chn_fix)
        w = wins_of(qwen_of(doc))
        check("china: DataV2 unwrapped 5h", w.get("rolling", {}).get("percent") == 10.0, str(w))
        check("china: DataV2 weekly", w.get("weekly", {}).get("percent") == 20.0, str(w))
        check("china: ISO reset parsed",
              w.get("rolling", {}).get("resetsAt") == "2026-09-03T12:00:01Z",
              str(w.get("rolling")))

        live_fix = tmp / "fx-live"
        # Live international response (home.qwencloud.com, 2026-09-04):
        # DataV2.data is a msg/code envelope; the usage fields sit one
        # level deeper than the China shape (X8r's final .data descent).
        make_fixtures(live_fix, {"data": {"secToken": "SECTOK3"}},
                      {"data": {"DataV2": {"ret": ["SUCCESS::ok"], "data": {
                          "msg": "Success.", "code": "SUCCESS",
                          "requestId": "r-1", "success": True,
                          "data": usage_body(42.5, 71.25, NOW_MS + 3600_000,
                                             NOW_MS + 2 * 86400_000)}},
                          "success": True, "httpStatus": 200}})
        doc = run_engine(make_home(tmp / "h-live",
                                   rows=["e=5; login_aliyunid_csrf=E6"]), live_fix)
        w = wins_of(qwen_of(doc))
        check("live intl: DataV2.data.data 5h",
              w.get("rolling", {}).get("percent") == 42.5, str(w))
        check("live intl: DataV2.data.data 7d",
              w.get("weekly", {}).get("percent") == 71.3, str(w))
        # The live account returned ONLY the week window, as a fraction.
        wo_fix = tmp / "fx-weekonly"
        make_fixtures(wo_fix, {"data": {"secToken": "SECTOK4"}},
                      {"data": {"DataV2": {"data": {"data": usage_body(
                          None, 0.417, None, NOW_MS + 2 * 86400_000)}}}})
        rec = qwen_of(run_engine(make_home(tmp / "h-weekonly",
                                           rows=["f=6; login_aliyunid_csrf=F7"]),
                                 wo_fix))
        check("week-only: fraction 0.417 → console · W 41.7%",
              rec["detail"] == "console · W 41.7%" and rec["label"] == "42%",
              f"{rec['detail']} / {rec['label']}")

        # 5) console failure → census fallback, exact counts ----------------- #
        bad_fix = tmp / "fx-bad"
        bad_fix.mkdir()
        (bad_fix / "qwen.session.json").write_text(json.dumps({"nope": True}))
        doc = run_engine(make_home(tmp / "h-fb", transcripts=True), bad_fix,
                         {"QWEN_PLAN_COOKIE": "x=1"})
        rec = qwen_of(doc)
        check("bad session: census fallback", rec["detail"].startswith("local"), rec["detail"])
        check("bad session: census counted both rows", "2/6000" in rec["detail"], rec["detail"])

        # 6) no credential at all → pure census ------------------------------ #
        doc = run_engine(make_home(tmp / "h-none", transcripts=True), intl_fix)
        check("no credential: census mode", qwen_of(doc)["detail"].startswith("local"),
              qwen_of(doc)["detail"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nqwen console regression: ALL PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
