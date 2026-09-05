#!/usr/bin/env python3
"""New-API gateway (QuantumNous/new-api) branch, fully offline.

The corpus validate run pins the token-level golden end-to-end; this file
owns the protocol edge cases against its own fixture dir + fake home:

  1. discovery: models.yml providers → env-name key resolved from
     ~/.omp/agent/.env (the file OMP loads); host dedup across providers;
     static-registry id collision skip; literal apiKey fallback
  2. /api/status signature gate: non-new-api hosts, unreachable hosts and
     wrong-shaped answers all drop out of the document entirely
  3. token-level billing: total_usage (US cents) → lifetime $ label; the
     1e8 unlimited sentinel → uncapped + "token only" scope annotation
     (billing.go: 1e8 only happens on the token branch); a real cap →
     spend window with percent + "of $cap" detail
  4. user-level upgrade: {ID}_ACCESS_TOKEN + {ID}_USER_ID → /api/user/self
     (forks require the New-Api-User header) → balance label, used detail,
     used-of-total meter; any failure falls back to token level
  5. today's spend from the persisted history snapshots, scope-tagged;
     token/user scope changes and counter resets → unknown, never negative
  6. no resolvable key → error record "no-key" (panel paste-row contract)
  7. no credential ever appears in the emitted document

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

# quota 463590000 / 500000 = $927.18 remaining, used 23910000 / 500000 =
# $47.82 — the shape /api/user/self returns (raw quota units).
USER_FIXTURE = {"success": True, "data": {"username": "github_fixture",
                                          "quota": 463590000,
                                          "used_quota": 23910000}}


def make_fixtures(d: Path, *, status: object = None, cap: float = 100000000,
                  total_cents: float = 3792.6399,
                  user: object | None = None) -> Path:
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
    if user is not None:
        (d / "newapi-agentrouter.user.json").write_text(json.dumps(user))
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
              "AGENTROUTER_ACCESS_TOKEN", "AGENTROUTER_USER_ID",
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


def yesterday_history(spend) -> dict:
    """spend: number (legacy token-scope) or {'usd','scope'} dict or None."""
    day = {"tokens": 1, "requests": 1}
    if spend is not None:
        day["spend"] = {"agentrouter": spend}
    return {"days": {"2026-09-02": day}}


CRED = {"AGENTROUTER_ACCESS_TOKEN": "pat-fixtured-access-token",
        "AGENTROUTER_USER_ID": "44499"}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="newapi-"))
    try:
        # 1) token-level happy path: env-name key from OMP's own .env ------ #
        fx = make_fixtures(tmp / "fx")
        doc = run_engine(make_home(tmp / "h1"), fx)
        rec = gw_of(doc)
        check("gateway record exact (token-level, uncapped)",
              rec == {"id": "agentrouter", "name": "Fixture Router",
                      "display": "FR", "configured": True, "kind": "balance",
                      "label": "$37.93", "value": 37.93, "currency": "$",
                      "detail": "new-api · Fixture Router · token only",
                      "windows": [], "error": None,
                      "keyEnv": "AGENTROUTER_API_KEY", "spendScope": "token",
                      "newapi": True},
              json.dumps(rec))
        ids = [p["id"] for p in doc["providers"]]
        check("static registry intact, gateway appended last",
              ids[-1] == "agentrouter" and len(ids) == 9, str(ids))
        tl = next(p for p in doc["providers"] if p["id"] == "trollllm")
        check("non-new-api host dropped; trollllm is the static record",
              not any(p["id"] == "trollllm-anthropic"
                      for p in doc["providers"])
              and ids.count("trollllm") == 1
              and tl["keyEnv"] == "TROLLLLM_COOKIE"
              and tl["error"] == "no-cookie", str(tl.get("error")))
        check("host dedup: one spawn per host", ids.count("agentrouter") == 1)
        check("static-id collision skipped (one qwen)",
              ids.count("qwen") == 1)
        text = json.dumps(doc)
        check("gateway keys absent from document",
              all(s not in text for s in ("sk-fixtured-newapi-key",
                                          "sk-fixtured-trollllm-key",
                                          "sk-literal-qwen-fixture")))

        # user fixture with bad shape (no quota fields) → annotated fallback
        fxbad = make_fixtures(tmp / "fxbad", user={"success": True, "data": {}})
        doc = run_engine(make_home(tmp / "h6b"), fxbad, CRED)
        check("bad-shaped user response → token fallback annotated",
              gw_of(doc)["detail"] == "new-api · Fixture Router · token only · user fetch failed",
              gw_of(doc)["detail"])

        # 2) user-level upgrade: PAT + uid → dashboard numbers -------------- #
        fxu = make_fixtures(tmp / "fxu", user=USER_FIXTURE)
        doc = run_engine(make_home(tmp / "h2"), fxu, CRED)
        rec = gw_of(doc)
        check("user-level record exact (balance + used meter)",
              rec == {"id": "agentrouter", "name": "Fixture Router",
                      "display": "FR", "configured": True, "kind": "balance",
                      "label": "$927.18", "value": 47.82, "currency": "$",
                      "detail": "new-api · Fixture Router · used $47.82",
                      "windows": [{"id": "spend", "label": "used", "spanMs": 0,
                                   "percent": 4.9, "used": 47.82, "total": 975.0,
                                   "unit": "$", "resetsAt": None}],
                      "error": None, "keyEnv": "AGENTROUTER_API_KEY",
                      "spendScope": "user", "newapi": True},
              json.dumps(rec))
        text = json.dumps(doc)
        check("access token + uid absent from document",
              "pat-fixtured-access-token" not in text and "44499" not in text)

        # user-level today diff
        doc = run_engine(make_home(tmp / "h3",
                                   history=yesterday_history({"usd": 47.40,
                                                              "scope": "user"})),
                         fxu, CRED)
        check("user-level today spend",
              gw_of(doc)["detail"] == "new-api · Fixture Router · used $47.82 · $0.42 today",
              gw_of(doc)["detail"])
        # scope change (token yesterday, user today) → no figure
        doc = run_engine(make_home(tmp / "h4",
                                   history=yesterday_history({"usd": 47.40,
                                                              "scope": "token"})),
                         fxu, CRED)
        check("scope change → no today figure",
              gw_of(doc)["detail"] == "new-api · Fixture Router · used $47.82",
              gw_of(doc)["detail"])
        # legacy bare-number snapshot reads as token scope
        doc = run_engine(make_home(tmp / "h5",
                                   history=yesterday_history(47.40)), fxu, CRED)
        check("legacy snapshot → token scope → no user-level figure",
              gw_of(doc)["detail"] == "new-api · Fixture Router · used $47.82",
              gw_of(doc)["detail"])

        # user fetch failure (no fixture) → token fallback, annotated
        doc = run_engine(make_home(tmp / "h6"), fx, CRED)
        check("user fetch failed → token fallback annotated",
              gw_of(doc)["detail"] == "new-api · Fixture Router · token only · user fetch failed",
              gw_of(doc)["detail"])
        # PAT without uid → no user attempt, plain token level
        doc = run_engine(make_home(tmp / "h7"), fx,
                         {"AGENTROUTER_ACCESS_TOKEN": "pat-fixtured-access-token"})
        check("PAT without uid → token level, no failure note",
              gw_of(doc)["detail"] == "new-api · Fixture Router · token only",
              gw_of(doc)["detail"])
        # credentials from OMP's own .env also work
        home8 = make_home(tmp / "h8", dotenv=(
            "AGENTROUTER_API_KEY=sk-fixtured-newapi-key\n"
            "AGENTROUTER_ACCESS_TOKEN=pat-fixtured-access-token\n"
            "AGENTROUTER_USER_ID=44499\n"))
        doc = run_engine(home8, fxu)
        check("PAT+uid from OMP .env → user level",
              gw_of(doc)["label"] == "$927.18", gw_of(doc)["label"])

        # 3) today's spend: token scope, yesterday's history snapshot ------- #
        fx2 = make_fixtures(tmp / "fx2")
        doc = run_engine(make_home(tmp / "h9",
                                   history=yesterday_history({"usd": 37.40,
                                                              "scope": "token"})), fx2)
        check("token today spend = lifetime − yesterday",
              gw_of(doc)["detail"] == "new-api · Fixture Router · token only · $0.53 today",
              gw_of(doc)["detail"])
        # counter reset (lifetime < baseline) → unknown, never negative
        doc = run_engine(make_home(tmp / "h10",
                                   history=yesterday_history({"usd": 99.0,
                                                              "scope": "token"})), fx2)
        check("counter reset → no today figure",
              gw_of(doc)["detail"] == "new-api · Fixture Router · token only",
              gw_of(doc)["detail"])

        # 4) key resolution --------------------------------------------------- #
        # no .env, no panel env → no-key error record (paste-row contract)
        doc = run_engine(make_home(tmp / "h11", dotenv=None), fx)
        rec = gw_of(doc)
        check("unresolvable env-name key → no-key",
              rec is not None and rec["error"] == "no-key"
              and rec["configured"] is False and rec["keyEnv"] == "AGENTROUTER_API_KEY",
              json.dumps(rec))
        # panel env store fallback (env_keys) when OMP's .env is absent
        doc = run_engine(make_home(tmp / "h12", dotenv=None), fx,
                         {"AGENTROUTER_API_KEY": "sk-panel-env-key"})
        check("panel env fallback resolves key", gw_of(doc)["configured"] is True)
        # literal apiKey with derived keyEnv
        lit = MODELS_YML.replace("apiKey: AGENTROUTER_API_KEY",
                                 "apiKey: sk-literal-gateway-key")
        doc = run_engine(make_home(tmp / "h13", models_yml=lit, dotenv=None), fx)
        rec = gw_of(doc)
        check("literal apiKey works, keyEnv derived",
              rec["configured"] is True and rec["keyEnv"] == "AGENTROUTER_API_KEY"
              and json.dumps(doc).find("sk-literal-gateway-key") < 0,
              json.dumps(rec))

        # 5) status gate -------------------------------------------------------- #
        # unreachable host: fixture dir without any status file
        empty = tmp / "fx-empty"
        empty.mkdir()
        doc = run_engine(make_home(tmp / "h14"), empty)
        check("unreachable host → dropped", gw_of(doc) is None)
        # wrong shape: HTML-ish / plain JSON without the new-api envelope
        fx3 = make_fixtures(tmp / "fx3", status={"data": {"chats": []}})
        doc = run_engine(make_home(tmp / "h15"), fx3)
        check("non-new-api shape → dropped", gw_of(doc) is None)
        # missing system_name
        fx4 = make_fixtures(tmp / "fx4", status={"data": {"version": "x"}})
        doc = run_engine(make_home(tmp / "h16"), fx4)
        check("missing system_name → dropped", gw_of(doc) is None)
        # no models.yml at all → no gateway, static plane untouched
        doc = run_engine(make_home(tmp / "h17", models_yml=None), fx)
        check("no models.yml → no gateway",
              gw_of(doc) is None and len(doc["providers"]) == 8)

        # 6) capped gateway -------------------------------------------------------- #
        fx5 = make_fixtures(tmp / "fx5", cap=50)
        doc = run_engine(make_home(tmp / "h18"), fx5)
        rec = gw_of(doc)
        wins = rec["windows"]
        check("capped gateway: spend window with percent",
              len(wins) == 1 and wins[0]["percent"] == 75.9
              and wins[0]["used"] == 37.93 and wins[0]["total"] == 50.0
              and wins[0]["unit"] == "$", json.dumps(wins))
        check("capped gateway: finite cap → no token-only annotation",
              rec["detail"] == "new-api · Fixture Router · $37.93 of $50",
              rec["detail"])
        # capped + user-level: meter becomes used-of-account-total
        fx6 = make_fixtures(tmp / "fx6", cap=50, user=USER_FIXTURE)
        doc = run_engine(make_home(tmp / "h19"), fx6, CRED)
        check("user level wins over finite token cap",
              gw_of(doc)["label"] == "$927.18"
              and gw_of(doc)["windows"][0]["total"] == 975.0,
              json.dumps(gw_of(doc)))

        # 7) spend snapshot persisted for tomorrow's diff --------------------------- #
        home20 = make_home(tmp / "h20")
        run_engine(home20, fx, local=True)
        state = json.loads((home20 / STATE_REL).read_text(encoding="utf-8"))
        spent = state["days"].get("2026-09-03", {}).get("spend")
        check("history persists token-scope snapshot",
              spent == {"agentrouter": {"usd": 37.93, "scope": "token"}},
              str(spent))
        home21 = make_home(tmp / "h21")
        run_engine(home21, fxu, CRED, local=True)
        state = json.loads((home21 / STATE_REL).read_text(encoding="utf-8"))
        spent = state["days"].get("2026-09-03", {}).get("spend")
        check("history persists user-scope snapshot",
              spent == {"agentrouter": {"usd": 47.82, "scope": "user"}},
              str(spent))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
