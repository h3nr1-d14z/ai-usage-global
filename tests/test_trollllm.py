#!/usr/bin/env python3
"""fetch_trollllm — the dashboard-cookie branch, fully offline.

TrollLLM (chat.trollllm.xyz) exposes no quota endpoints for sk- keys; the
dual ledger (plan daily credits + PAYG wallet) comes from trollllm.xyz's
dashboard API under a browser session cookie (TROLLLLM_COOKIE). These
tests pin: the happy-path record math (credits, half-up percents, label,
detail), the stale-reset roll-forward, the no-cookie paste-row state, the
expired-cookie failure, a pure-PAYG account, and that the OMP
trollllm-anthropic gateway entry still drops (trollllm is the static
record, never a gateway)."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
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


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def me_body(alloc=50, used=5.25, reset_ms=None, tier="lite") -> dict:
    return {"username": "t", "tier": tier, "creditPriority": "plan_first",
            "planDailyAllocation": alloc, "planDailyUsed": used,
            "planDailyResetDate": iso(reset_ms if reset_ms is not None
                                      else NOW_MS + 11 * 3600_000),
            "dailyResetHour": 23, "dailyResetMinute": 0}


def res_body(remaining=12.5, spent=3.61, active=1) -> dict:
    return {"totalRemaining": remaining, "totalOriginal": remaining + spent,
            "totalUsed": spent, "activeCount": active, "resources": []}


def make_fixtures(d: Path, me: object, res: object) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "trollllm.me.json").write_text(json.dumps(me))
    (d / "trollllm.resources.json").write_text(json.dumps(res))


def make_home(root: Path) -> Path:
    """Fake home with the OMP models.yml trollllm entries — proves the
    static record wins and the same-host anthropic twin drops as a
    non-new-api gateway."""
    root.mkdir(parents=True, exist_ok=True)
    agent = root / ".omp/agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "models.yml").write_text(
        "providers:\n"
        "  trollllm:\n"
        "    baseUrl: https://chat.fx.invalid/v1\n"
        "    apiKey: TROLLLLM_API_KEY\n"
        "  trollllm-anthropic:\n"
        "    baseUrl: https://chat.fx.invalid\n"
        "    apiKey: TROLLLLM_API_KEY\n", encoding="utf-8")
    (agent / ".env").write_text("TROLLLLM_API_KEY=sk-fx-trollllm\n",
                                encoding="utf-8")
    return root


def run_engine(home: Path, fixtures: Path, env_extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update({"AIUSAGE_NOW": str(NOW_MS), "AIUSAGE_HOME": str(home),
                "AIUSAGE_FIXTURES": str(fixtures), "TZ": "UTC",
                "PYTHONDONTWRITEBYTECODE": "1"})
    env.pop("AIUSAGE_ENV_FILE", None)
    env.pop("TROLLLLM_COOKIE", None)
    for k, v in (env_extra or {}).items():
        env[k] = v
    proc = subprocess.run([sys.executable, str(ENGINE), "--settings",
                           json.dumps({"showLocalConsumption": False})],
                          capture_output=True, text=True, env=env, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"engine exit {proc.returncode}: {proc.stderr[-300:]}")
    return json.loads(proc.stdout)


def tl_of(doc: dict) -> dict:
    return next(p for p in doc["providers"] if p["id"] == "trollllm")

def wins_of(rec: dict) -> dict:
    return {x["id"]: x for x in rec["windows"]}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="trollllm-"))
    try:
        cookie = "cf_clearance=tst-clearance; admin_session=tst.jwt; session_present=1"
        good = tmp / "fx-good"
        make_fixtures(good, me_body(), res_body())

        # 1) happy path: dual ledger, exact math ---------------------------- #
        doc = run_engine(make_home(tmp / "h1"), good, {"TROLLLLM_COOKIE": cookie})
        rec = tl_of(doc)
        w = wins_of(rec)
        check("configured, balance kind",
              rec["configured"] and rec["kind"] == "balance"
              and rec["error"] is None, str(rec["error"]))
        check("label = plan remaining 44.8 cr", rec["label"] == "44.8 cr", rec["label"])
        check("value = plan used 10.5%", rec["value"] == 10.5, str(rec["value"]))
        check("plan window exact", w.get("plan") == {
            "id": "plan", "label": "daily", "spanMs": 86400000,
            "percent": 10.5, "used": 5.25, "total": 50.0, "unit": "cr",
            "resetsAt": iso(NOW_MS + 11 * 3600_000)}, str(w.get("plan")))
        check("wallet window exact (used vs lifetime)",
              w.get("payg") == {"id": "payg", "label": "wallet", "spanMs": 0,
                                "percent": 22.4, "used": 3.61, "total": 16.11,
                                "unit": "cr", "resetsAt": None}, str(w.get("payg")))
        check("detail line exact",
              rec["detail"] == "lite · daily 5.25/50 cr · wallet 12.50 cr",
              rec["detail"])
        check("static record wins, anthropic twin dropped",
              [p["id"] for p in doc["providers"]].count("trollllm") == 1
              and not any(p["id"] == "trollllm-anthropic"
                          for p in doc["providers"]))
        check("keyEnv contract", rec["keyEnv"] == "TROLLLLM_COOKIE")
        text = json.dumps(doc)
        check("cookie never enters the document",
              all(s not in text for s in ("tst-clearance", "tst.jwt",
                                          "cf_clearance", "sk-fx-trollllm")))

        # 2) stale reset date (in the past) rolls forward a day -------------- #
        stale = tmp / "fx-stale"
        make_fixtures(stale, me_body(reset_ms=NOW_MS - 2 * 3600_000), res_body())
        rec = tl_of(run_engine(make_home(tmp / "h2"), stale,
                               {"TROLLLLM_COOKIE": cookie}))
        check("stale reset rolls +24h",
              wins_of(rec).get("plan", {}).get("resetsAt")
              == iso(NOW_MS + 22 * 3600_000),
              str(wins_of(rec).get("plan")))

        # 3) no cookie → paste-row state -------------------------------------- #
        rec = tl_of(run_engine(make_home(tmp / "h3"), good))
        check("no cookie: benign error, unconfigured",
              rec["error"] == "no-cookie" and not rec["configured"]
              and rec["keyEnv"] == "TROLLLLM_COOKIE", str(rec["error"]))

        # 4) expired cookie / transport failure → fetch-failed ---------------- #
        # (no trollllm fixtures: the transport raises, exactly like a 401
        # from an expired session does live)
        empty = tmp / "fx-empty"
        empty.mkdir()
        rec = tl_of(run_engine(make_home(tmp / "h4"), empty,
                               {"TROLLLLM_COOKIE": cookie}))
        check("expired cookie: fetch-failed", rec["error"] == "fetch-failed",
              str(rec["error"]))

        # 5) pure PAYG account (no plan): wallet is the headline -------------- #
        payg = tmp / "fx-payg"
        make_fixtures(payg, me_body(alloc=None, used=None, tier=None),
                      res_body(remaining=7.5, spent=2.5))
        rec = tl_of(run_engine(make_home(tmp / "h5"), payg,
                               {"TROLLLLM_COOKIE": cookie}))
        w = wins_of(rec)
        check("pure PAYG: wallet-only windows",
              set(w) == {"payg"} and w["payg"]["percent"] == 25.0, str(w))
        check("pure PAYG: label/value from wallet",
              rec["label"] == "7.50 cr" and rec["value"] == 25.0
              and rec["detail"] == "wallet 7.50 cr",
              f'{rec["label"]} / {rec["value"]} / {rec["detail"]}')

        # 6) both ledgers absent → honest failure ------------------------------ #
        bare = tmp / "fx-bare"
        make_fixtures(bare, me_body(alloc=0, used=0),
                      res_body(remaining=0, spent=0))
        rec = tl_of(run_engine(make_home(tmp / "h6"), bare,
                               {"TROLLLLM_COOKIE": cookie}))
        check("no ledgers at all: fetch-failed", rec["error"] == "fetch-failed",
              str(rec["error"]))

        # 6b) all-expired wallet: no false 100% meter, detail keeps the
        #     ledger visible (the live account state 2026-09-05) --------- #
        gone = tmp / "fx-gone"
        make_fixtures(gone, me_body(), res_body(remaining=0, spent=3.61,
                                                active=0))
        rec = tl_of(run_engine(make_home(tmp / "h6b"), gone,
                               {"TROLLLLM_COOKIE": cookie}))
        check("expired wallet: plan-only windows",
              set(wins_of(rec)) == {"plan"}, str(wins_of(rec)))
        check("expired wallet: detail still shows the ledger",
              rec["detail"] == "lite · daily 5.25/50 cr · wallet 0.00 cr",
              rec["detail"])

        # 7) bad shape (me is a list) → fetch-failed --------------------------- #
        bad = tmp / "fx-bad"
        make_fixtures(bad, [], res_body())
        rec = tl_of(run_engine(make_home(tmp / "h7"), bad,
                               {"TROLLLLM_COOKIE": cookie}))
        check("bad shape: fetch-failed", rec["error"] == "fetch-failed",
              str(rec["error"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES
          else "\ntrollllm regression: ALL PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
