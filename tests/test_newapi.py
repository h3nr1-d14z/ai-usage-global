#!/usr/bin/env python3
"""New-API gateway (QuantumNous/new-api) branch, fully offline.

The corpus validate run pins the golden record end-to-end; this file owns
the protocol edge cases against its own fixture dir + fake home:

  1. discovery: models.yml providers → env-name key resolved from
     ~/.omp/agent/.env (the file OMP loads); host dedup across providers;
     static-registry id collision skip; literal apiKey fallback
  2. /api/status signature gate: non-new-api hosts, unreachable hosts and
     wrong-shaped answers all drop out of the document entirely
  3. billing: total_usage (US cents) → lifetime $ label; hard_limit_usd
     1e8 sentinel → uncapped (no window); a real cap → spend window with
     percent + "of $cap" detail
  4. today's spend from the persisted history snapshot (yesterday diff);
     token rotation (lifetime went backwards) → unknown, never negative
  5. no resolvable key → error record "no-key" (panel paste-row contract)
  6. the gateway key NEVER appears in the emitted document

Run: python3 tests/test_newapi.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine/usage.py"
NOW_MS = 1788436800000  # 2026-09-03T12:00Z → today 09-03, yesterday 09-02 (TZ=UTC)
STATE_REL = ".local/state/h3nr1.d14z.ai-usage/history.json"

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


MODELS_YML = (
    "providers:\n"
    "  agentrouter:\n"
    "    baseUrl: https://gw.fixture.invalid\n"
    "    api: anthropic-messages\n"
    "    apiKey: AGENTROUTER_API_KEY\n"
    "  trollllm:\n"
    "    baseUrl: https://chat.fixture.invalid/v1\n"
    "    apiKey: TROLLLLM_API_KEY\n"
    "  trollllm-anthropic:\n"
    "    baseUrl: https://chat.fixture.invalid\n"
    "    apiKey: TROLLLLM_API_KEY\n"
    "  qwen:\n"
    "    baseUrl: https://qwen.fixture.invalid\n"
    "    apiKey: sk-literal-qwen-fixture\n"
)


def make_fixtures(d: Path, *, status: object = None, cap: float = 100000000,
                  total_cents: float = 3792.6399) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    if status is None:
        status = {"data": {"system_name": "Fixture Router",
                           "version": "init-fixture"}}
    (d / "newapi-agentrouter.status.json").write_text(json.dumps(status))
    (d / "newapi-agentrouter.subscription.json").write_text(json.dumps(
        {"object": "billing_subscription", "has_payment_method": True,
         "soft_limit_usd": cap, "hard_limit_usd": cap,
         "system_hard_limit_usd": cap, "access_until": 0}))
    (d / "newapi-agentrouter.usage.json").write_text(json.dumps(
        {"object": "list", "total_usage": total_cents}))
    return d


def make_home(root: Path, models_yml: str | None = MODELS_YML,
              dotenv: str | None = "AGENTROUTER_API_KEY=sk-fixtured-newapi-key\n"
                                   "TROLLLLM_API_KEY=sk-fixtured-trollllm-key\n",
              history: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if models_yml is not None:
        agent = root / ".omp/agent"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "models.yml").write_text(models_yml)
        if dotenv is not None:
            (agent / ".env").write_text(dotenv)
    if history is not None:
        state = root / STATE_REL
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(history))
    return root


def run_engine(home: Path, fixtures: Path, env_extra: dict | None = None,
               local: bool = False) -> dict:
    env = dict(os.environ)
    env.update({"AIUSAGE_NOW": str(NOW_MS), "AIUSAGE_HOME": str(home),
                "AIUSAGE_FIXTURES": str(fixtures), "TZ": "UTC",
                "PYTHONDONTWRITEBYTECODE": "1"})
    # The suite may itself run inside an OMP subprocess that injects the
    # user's real gateway keys — pop them so the no-key cases stay honest.
    for k in ("AIUSAGE_ENV_FILE", "AGENTROUTER_API_KEY", "TROLLLLM_API_KEY",
              "QWEN_PLAN_COOKIE"):
        env.pop(k, None)
    for k, v in (env_extra or {}).items():
        env[k] = v
    proc = subprocess.run([sys.executable, str(ENGINE), "--settings",
                           json.dumps({"showLocalConsumption": local})],
                          capture_output=True, text=True, env=env, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"engine exit {proc.returncode}: {proc.stderr[-300:]}")
    return json.loads(proc.stdout)


def gw_of(doc: dict) -> dict | None:
    return next((p for p in doc["providers"] if p["id"] == "agentrouter"), None)


def yesterday_history(spend: float | None) -> dict:
    day = {"tokens": 1, "requests": 1}
    if spend is not None:
        day["spend"] = {"agentrouter": spend}
    return {"days": {"2026-09-02": day}}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="newapi-"))
    try:
        # 1) happy path: env-name key from OMP's own .env --------------------- #
        fx = make_fixtures(tmp / "fx")
        doc = run_engine(make_home(tmp / "h1"), fx)
        rec = gw_of(doc)
        check("gateway record exact (uncapped balance)",
              rec == {"id": "agentrouter", "name": "Fixture Router",
                      "display": "FR", "configured": True, "kind": "balance",
                      "label": "$37.93", "value": 37.93, "currency": "$",
                      "detail": "new-api · Fixture Router", "windows": [],
                      "error": None, "keyEnv": "AGENTROUTER_API_KEY",
                      "newapi": True},
              json.dumps(rec))
        ids = [p["id"] for p in doc["providers"]]
        check("static registry intact, gateway appended last",
              ids[-1] == "agentrouter" and len(ids) == 8, str(ids))
        check("non-new-api host dropped (trollllm)",
              not any(p["id"].startswith("trollllm") for p in doc["providers"]))
        check("host dedup: one spawn per host", ids.count("agentrouter") == 1)
        check("static-id collision skipped (one qwen)",
              ids.count("qwen") == 1)
        text = json.dumps(doc)
        check("gateway keys absent from document",
              all(s not in text for s in ("sk-fixtured-newapi-key",
                                          "sk-fixtured-trollllm-key",
                                          "sk-literal-qwen-fixture")))

        # 2) today's spend: yesterday's history snapshot diffs ----------------- #
        fx2 = make_fixtures(tmp / "fx2")
        doc = run_engine(make_home(tmp / "h2",
                                   history=yesterday_history(37.40)), fx2)
        check("today spend = lifetime − yesterday",
              gw_of(doc)["detail"] == "new-api · Fixture Router · $0.53 today",
              gw_of(doc)["detail"])
        # rotation: lifetime went backwards → unknown, never negative
        doc = run_engine(make_home(tmp / "h3",
                                   history=yesterday_history(99.0)), fx2)
        check("rotation (lifetime < baseline) → no today figure",
              gw_of(doc)["detail"] == "new-api · Fixture Router",
              gw_of(doc)["detail"])
        # no spend key on yesterday → no today figure
        doc = run_engine(make_home(tmp / "h4",
                                   history=yesterday_history(None)), fx2)
        check("yesterday without spend → no today figure",
              gw_of(doc)["detail"] == "new-api · Fixture Router",
              gw_of(doc)["detail"])

        # 3) key resolution ------------------------------------------------------ #
        # no .env, no panel env → no-key error record (paste-row contract)
        doc = run_engine(make_home(tmp / "h5", dotenv=None), fx)
        rec = gw_of(doc)
        check("unresolvable env-name key → no-key",
              rec is not None and rec["error"] == "no-key"
              and rec["configured"] is False and rec["keyEnv"] == "AGENTROUTER_API_KEY",
              json.dumps(rec))
        # panel env store fallback (env_keys) when OMP's .env is absent
        doc = run_engine(make_home(tmp / "h6", dotenv=None), fx,
                         {"AGENTROUTER_API_KEY": "sk-panel-env-key"})
        check("panel env fallback resolves key", gw_of(doc)["configured"] is True)
        # literal apiKey with derived keyEnv
        lit = MODELS_YML.replace("apiKey: AGENTROUTER_API_KEY",
                                 "apiKey: sk-literal-gateway-key")
        doc = run_engine(make_home(tmp / "h7", models_yml=lit, dotenv=None), fx)
        rec = gw_of(doc)
        check("literal apiKey works, keyEnv derived",
              rec["configured"] is True and rec["keyEnv"] == "AGENTROUTER_API_KEY"
              and json.dumps(doc).find("sk-literal-gateway-key") < 0,
              json.dumps(rec))

        # 4) status gate ---------------------------------------------------------- #
        # unreachable host: fixture dir without any status file
        empty = tmp / "fx-empty"
        empty.mkdir()
        doc = run_engine(make_home(tmp / "h8"), empty)
        check("unreachable host → dropped", gw_of(doc) is None)
        # wrong shape: HTML-ish / plain JSON without the new-api envelope
        fx3 = make_fixtures(tmp / "fx3", status={"data": {"chats": []}})
        doc = run_engine(make_home(tmp / "h9"), fx3)
        check("non-new-api shape → dropped", gw_of(doc) is None)
        # missing system_name
        fx4 = make_fixtures(tmp / "fx4", status={"data": {"version": "x"}})
        doc = run_engine(make_home(tmp / "h10"), fx4)
        check("missing system_name → dropped", gw_of(doc) is None)
        # no models.yml at all → no gateway, static plane untouched
        doc = run_engine(make_home(tmp / "h11", models_yml=None), fx)
        check("no models.yml → no gateway",
              gw_of(doc) is None and len(doc["providers"]) == 7)

        # 5) capped gateway -------------------------------------------------------- #
        fx5 = make_fixtures(tmp / "fx5", cap=50)
        doc = run_engine(make_home(tmp / "h12"), fx5)
        rec = gw_of(doc)
        wins = rec["windows"]
        check("capped gateway: spend window with percent",
              len(wins) == 1 and wins[0]["percent"] == 75.9
              and wins[0]["used"] == 37.93 and wins[0]["total"] == 50.0
              and wins[0]["unit"] == "$", json.dumps(wins))
        check("capped gateway: detail carries of-cap",
              rec["detail"] == "new-api · Fixture Router · $37.93 of $50",
              rec["detail"])

        # 6) spend snapshot persisted for tomorrow's diff --------------------------- #
        home13 = make_home(tmp / "h13")
        run_engine(home13, fx, local=True)
        state = json.loads((home13 / STATE_REL).read_text(encoding="utf-8"))
        spent = state["days"].get("2026-09-03", {}).get("spend")
        check("history persists today's lifetime spend",
              spent == {"agentrouter": 37.93}, str(spent))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
