#!/usr/bin/env python3
"""Golden correctness invariants for engine/usage.py.

Runs the engine against the corpus (fixture quota transport + frozen clock)
and fails loudly on any contract violation. autoresearch.sh runs this before
timing: an optimization that breaks correctness is not an optimization.

Checks:
  1.  stdout is one valid JSON line; schema==1; no NaN/Infinity (would blank
      the QML panel on JSON.parse).
  2.  All 7 providers present, ordered, canonical record fields.
  3.  Windows sane: percent within 0..999, resetsAt ISO-8601 or null.
  4.  Quota values match fixture math exactly (round-half-up display).
  5.  Local consumption equals the corpus manifest EXACTLY — the scanner must
      neither invent nor drop tokens/requests/cost (per-model buckets, totals,
      per-day arrays; engine PRICING vs generator PRICING cross-checked).
  6.  The qwen plan census reproduces the corpus 5h/week/month request counts.
  7.  Error strings / document never leak credential-shaped tokens NOR any
      of the corpus fixture secrets actually written to disk.
  8.  Determinism: reruns are byte-identical modulo `timings` (the metric).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

UTC = dt.timezone.utc

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "bench/corpus"

# All date math here is tz-aware UTC (no os.environ/tzset games — nothing
# the calling shell can silently override). The engine's DayIndex is
# host-local BY DESIGN (user wall clocks), so its subprocess gets TZ=UTC
# below to agree with the UTC-pinned goldens.

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok  ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def strip_timings(text: str) -> str:
    doc = json.loads(text)
    doc.pop("timings", None)
    # NO sort_keys: insertion order is itself a determinism contract
    # (scanner fold order, provider order). Sorting would hide reorders.
    return json.dumps(doc)


def main() -> int:
    corpus = json.loads((CORPUS / "corpus.json").read_text(encoding="utf-8"))
    now_ms = corpus["nowMs"]
    env = dict(os.environ)
    env.update({
        "AIUSAGE_NOW": str(now_ms),
        "AIUSAGE_HOME": str(CORPUS / "home"),
        "AIUSAGE_FIXTURES": str(CORPUS / "fixtures"),
        "AIUSAGE_ENV_FILE": str(CORPUS / "env"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
    })
    # An ambient QWEN_PLAN_COOKIE in the developer's shell would flip the
    # corpus qwen record from census to console mode and break the count
    # checks — the console branch belongs to test_qwen_console.py only.
    env.pop("QWEN_PLAN_COOKIE", None)

    def run_engine() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "engine/usage.py"),
             "--settings", str(ROOT / "bench/settings.json")],
            capture_output=True, text=True, env=env, timeout=300)

    proc = run_engine()
    check("engine exits 0", proc.returncode == 0, proc.stderr[-400:])
    if proc.returncode != 0:
        return 1
    text = proc.stdout
    check("single-line JSON document", text.count("\n") == 1 and text.endswith("\n"))
    check("no NaN/Infinity in output", "NaN" not in text and "Infinity" not in text)
    try:
        doc = json.loads(text)
    except ValueError as exc:
        check("stdout parses as JSON", False, str(exc))
        return 1
    check("schema == 1", doc.get("schema") == 1)

    # ---- structure ---------------------------------------------------------- #
    providers = doc.get("providers", [])
    ids = [p["id"] for p in providers]
    check("8 providers, ordered",
          ids == ["opencode", "openrouter", "kimi", "zai", "deepseek", "copilot",
                  "qwen", "agentrouter"],
          str(ids))
    required = {"id", "name", "display", "configured", "kind", "label", "value",
                "currency", "detail", "windows", "error"}
    check("record fields complete", all(required <= set(p) for p in providers))
    # keyEnv is the panel's paste-key contract: the UI stores under exactly
    # this name, and the adapter's get_key() chain must read it back. A
    # rename on one side without the other silently breaks key adding.
    keyenv = {p["id"]: p.get("keyEnv") for p in providers}
    check("keyEnv contract pinned", keyenv == {
        "opencode": "OPENCODE_GO_API_KEY", "openrouter": "OPENROUTER_API_KEY",
        "kimi": "KIMI_API_KEY", "zai": "ZAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY", "copilot": "GITHUB_TOKEN",
        "qwen": "QWEN_PLAN_COOKIE", "agentrouter": "AGENTROUTER_API_KEY"},
        str(keyenv))
    iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    windows_ok = all(
        (w["percent"] is None or 0 <= w["percent"] <= 999)
        and (w["resetsAt"] is None or iso_re.match(str(w["resetsAt"])))
        for p in providers for w in p["windows"])
    check("windows sane (percent range, ISO resets)", windows_ok)

    # ---- quota math vs fixtures --------------------------------------------- #
    by_id = {p["id"]: p for p in providers}

    oc = by_id["opencode"]
    check("opencode configured", oc["configured"] and oc["error"] is None)
    ocw = {w["id"]: w for w in oc["windows"]}
    check("opencode headline = weekly 78.3 (half-up)", oc["value"] == 78.3, str(oc["value"]))
    check("opencode 5h window", ocw.get("rolling", {}).get("percent") == 42.5)
    check("opencode month window", ocw.get("monthly", {}).get("percent") == 15.0)
    check("opencode reset parsed", bool(ocw.get("weekly", {}).get("resetsAt")))

    orp = by_id["openrouter"]
    check("openrouter remaining $28.63", orp["value"] == 28.63, str(orp["value"]))
    orw = {w["id"]: w for w in orp["windows"]}
    check("openrouter key-limit 85.5%", orw.get("keylimit", {}).get("percent") == 85.5,
          str(orw))

    ki = by_id["kimi"]
    check("kimi balance 128.44", ki["value"] == 128.44 and ki["currency"] == "CNY")
    kw = {w["id"]: w for w in ki["windows"]}
    check("kimi used-ratio window", kw.get("billing", {}).get("percent") == 74.3, str(kw))

    za = by_id["zai"]
    zaw = {w["label"]: w for w in za["windows"]}
    check("zai 5h=61.5%", zaw.get("5h", {}).get("percent") == 61.5, str(zaw))
    check("zai W=34.3 (half-up)", zaw.get("W", {}).get("percent") == 34.3, str(zaw))

    ds = by_id["deepseek"]
    check("deepseek balance 142.50", ds["value"] == 142.5, str(ds["value"]))
    check("deepseek ratio window 35.2",
          bool(ds["windows"]) and abs(ds["windows"][0]["percent"] - 35.2) <= 0.1,
          str(ds["windows"]))

    cp = by_id["copilot"]
    cpw = {w["id"]: w for w in cp["windows"]}
    check("copilot premium 75%", cpw.get("premium", {}).get("percent") == 75.0, str(cpw))
    check("copilot monthly window present", cpw.get("month", {}).get("percent") is not None,
          str(cpw))

    qw = by_id["qwen"]
    check("qwen configured from local census", qw["configured"], str(qw["error"]))
    plan = next((s.get("planWindow") for s in corpus["expected"]["sources"]
                 if s.get("source") == "qwen"), None)
    if plan:
        qww = {w["id"]: w for w in qw["windows"]}
        check("qwen 5h count exact", qww.get("rolling", {}).get("used") == plan["5h"],
              f'{qww.get("rolling")} vs {plan}')
        check("qwen week count exact", qww.get("weekly", {}).get("used") == plan["week"])
        check("qwen month count exact", qww.get("monthly", {}).get("used") == plan["month"])
        # Self-documenting boundary coverage: the corpus MUST contain qwen
        # requests past the 30-day month cap, or the month<total inequality
        # (and hence the census boundary fix) silently loses its trap.
        qwen_total = next((s["requests"] for s in corpus["expected"]["sources"]
                           if s.get("source") == "qwen"), 0)
        check("qwen month boundary exercised",
              0 < plan["month"] < qwen_total,
              f'month={plan["month"]} total={qwen_total} — no >30d rows?')

    # ---- consumption vs corpus manifest (EXACT) ------------------------------ #
    expected = corpus["expected"]
    local = doc.get("local", {})
    check("local plane present", isinstance(local, dict) and bool(local))
    check("totalTokens exact", local.get("totalTokens") == expected["totalTokens"],
          f'{local.get("totalTokens")} vs {expected["totalTokens"]}')
    check("totalRequests exact", local.get("totalRequests") == expected["totalRequests"],
          f'{local.get("totalRequests")} vs {expected["totalRequests"]}')
    model_diff = []
    for model, exp_b in expected["models"].items():
        got_b = local.get("models", {}).get(model)
        if not got_b:
            model_diff.append(f"{model}:missing")
            continue
        for field in ("inputTokens", "outputTokens", "cacheReadTokens",
                      "cacheWriteTokens", "reasoningTokens", "requests"):
            if got_b.get(field) != exp_b[field]:
                model_diff.append(f"{model}.{field} {got_b.get(field)}!={exp_b[field]}")
        # cost: independent tables (engine PRICING vs generator PRICING) —
        # a pricing regression on either side fails here. Tolerance covers
        # float accumulation order only; 1e-4$ is far below any real drift.
        if abs(got_b.get("cost", -1) - exp_b["cost"]) > 1e-4:
            model_diff.append(f"{model}.cost {got_b.get('cost')}!={exp_b['cost']}")
    extra = set(local.get("models", {})) - set(expected["models"])
    if extra:
        model_diff.append(f"extra:{sorted(extra)}")
    check("per-model buckets exact (incl. cost)", not model_diff,
          "; ".join(model_diff[:6]))

    days = local.get("recentDays", [])
    check("recentDays = 7", len(days) == 7)
    # EXACT per-day golden: tokens and requests for every one of the 7 days.
    # (A shifted/off-by-one day mapping satisfies the old coherence-only
    # checks; it cannot survive this.)
    expected_days = expected.get("recentDays")
    if expected_days:  # corpus v4+
        day_ok = (len(days) == len(expected_days)
                  and all(a["date"] == b["date"] and a["tokens"] == b["tokens"]
                          and a["requests"] == b["requests"]
                          for a, b in zip(days, expected_days)))
        first_bad = next((f'{a["date"]} got {a["tokens"]}/{a["requests"]} '
                          f'want {b["tokens"]}/{b["requests"]}'
                          for a, b in zip(days, expected_days or [])
                          if a != b), "")
        check("recentDays exact per-day golden", day_ok, first_bad)
    else:
        check("recentDays exact per-day golden", False,
              "corpus lacks v4 expected.recentDays — regenerate corpus")
    week_sum = sum(d["tokens"] for d in days)
    check("weekTokens == sum(recentDays)", local.get("weekTokens") == week_sum,
          f'{local.get("weekTokens")} vs {week_sum}')
    check("week <= total", week_sum <= expected["totalTokens"])
    today = dt.datetime.fromtimestamp(now_ms / 1000.0, UTC).strftime("%Y-%m-%d")
    today_row = next((d for d in days if d["date"] == today), None)
    check("today coherent", today_row is not None
          and local.get("todayTokens") == today_row["tokens"]
          and local.get("todayRequests") == today_row["requests"])
    # today must carry a non-trivial share of the week (frozen NOW is
    # mid-day with traffic generated up to it) — catches all-zero-day bugs.
    check("today non-trivial", today_row is not None and today_row["requests"] > 0)

    sources = {s["source"] for s in local.get("sources", [])}
    check("all 5 sources reported",
          sources >= {"opencode", "claude", "codex", "qwen", "omp"}, str(sources))
    # omp lane attribution: main sessions vs advisor vs spawned subagents.
    omp_src = next((s for s in local.get("sources", []) if s["source"] == "omp"), None)
    omp_exp = next((s for s in corpus["expected"]["sources"] if s["source"] == "omp"),
                   None)
    check("omp lanes golden (main/advisor/subagent)",
          omp_src is not None and omp_exp is not None
          and omp_src.get("lanes") == omp_exp.get("lanes"),
          f'{omp_src and omp_src.get("lanes")} vs {omp_exp and omp_exp.get("lanes")}')
    hist = local.get("history")
    check("history 30 days", isinstance(hist, list) and len(hist) == 30,
          f"len={len(hist) if isinstance(hist, list) else 'missing'}")
    check("history today is live", isinstance(hist, list) and len(hist) == 30
          and hist[-1]["date"] == today
          and hist[-1]["tokens"] == local.get("todayTokens"),
          str(hist[-1]) if isinstance(hist, list) and hist else "none")

    # ---- new-api gateway ------------------------------------------------------ #
    # Golden record from the manifest; trollllm proves both the host-dedup
    # skip and the "not new-api → drop" path (no status fixture); the spend
    # snapshot proves update_history persists gateway lifetime for
    # tomorrow's today-figure.
    gw = next((p for p in providers if p.get("id") == "agentrouter"), None)
    gw_exp = corpus["expected"].get("newapi")
    check("newapi gateway golden (agentrouter)", gw == gw_exp,
          f"{gw} vs {gw_exp}")
    check("newapi non-gateway hosts dropped",
          not any(p.get("id") in ("trollllm", "trollllm-anthropic")
                  for p in providers))
    try:
        state = json.loads((CORPUS / "home/.local/state/h3nr1.d14z.ai-usage"
                            "/history.json").read_text(encoding="utf-8"))
        spent = state["days"][today].get("spend")
    except (OSError, ValueError, KeyError, TypeError):
        spent = None
    check("newapi spend snapshot persisted", spent == {"agentrouter": 37.93},
          str(spent))

    # ---- hygiene -------------------------------------------------------------- #
    # (1) generic credential-shaped strings, (2) the ACTUAL fixture secrets the
    # corpus writes (auth.json/copilot/kimi/deepseek/env file) — the old regex
    # only matched an unexercised 'oc-env' substring and let the real
    # oc-fixtured-key-000000 token through.
    fixture_secrets = ["oc-fixtured-key-000000", "gho_fixturedcopilottoken0000000000",
                       "sk-fixtured-kimi-key", "sk-fixtured-deepseek-key",
                       "oc-env-key", "sk-or-env-key", "sk-kimi-env-key",
                       "sk-zai-env-key", "sk-ds-env-key", "gho_env_copilot_token",
                       "sk-fixtured-newapi-key", "sk-fixtured-trollllm-key",
                       "sk-literal-qwen-fixture"]
    leaked = [s for s in fixture_secrets if s in text]
    leak = re.search(r"(gho_[A-Za-z0-9]{10,}|sk-[A-Za-z0-9-]{10,})", text)
    check("no credential leakage in document", not leaked and leak is None,
          f"secrets={leaked} pattern={leak.group(0) if leak else ''}")

    # ---- determinism ----------------------------------------------------------- #
    proc2 = run_engine()
    same = proc2.returncode == 0 and strip_timings(proc2.stdout) == strip_timings(text)
    check("identical rerun (modulo timings)", same)

    print(f"\n{len(FAILURES)} failure(s)" if FAILURES else "\nALL CHECKS PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
