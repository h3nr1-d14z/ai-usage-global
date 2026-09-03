#!/usr/bin/env python3
"""Deterministically generate a fake $HOME holding real-shaped agent stores.

The benchmark measures engine/usage.py against this corpus, so the corpus IS
the workload: same seed => byte-identical files and a manifest of expected
aggregates that tests/validate.py checks the engine against. No timestamps
are wall-clock relative: everything is anchored to FIXED_NOW.

Layout produced under --out:
  home/.claude/projects/<proj>/<session>.jsonl     Claude Code transcripts
  home/.codex/sessions/<y>/<m>/<d>/rollout-*.jsonl Codex CLI rollouts
  home/.qwen/projects/<proj>/chats/<chat>.jsonl    Qwen Code CLI (Coding Plan)
  home/.omp/agent/sessions/<p>/<s>.jsonl           Oh My Pi sessions
  home/.local/share/opencode/opencode.db           OpenCode SQLite (messages)
  home/.local/share/opencode/auth.json             opencode-go key entry
  home/.config/gh/hosts.yml                        Copilot OAuth token
  home/.kimi-code/config.toml                      Kimi api key
  home/.deepseek/config.toml                       DeepSeek api key
  fixtures/<provider>[.<part>].json                frozen quota responses
  env                                              dotenv with keys
  corpus.json                                      manifest (expected totals)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import shutil
import sqlite3
import sys
from pathlib import Path

CORPUS_VERSION = 4  # v4: expected.recentDays per-day golden arrays

# Fixed anchor: 2026-09-03T12:00:00Z. Pinned so window math is reproducible.
FIXED_NOW_MS = int(dt.datetime(2026, 9, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
                   .timestamp() * 1000)

DAY_MS = 24 * 3600_000
H5_MS = 5 * 3600_000

# Per-model token profile: (input, output, cacheRead, cacheWrite, reasoning)
PROFILES = {
    "claude-opus-5": (4200, 380, 51000, 4200, 0),
    "claude-sonnet-4-5": (3100, 260, 38000, 3100, 0),
    "gpt-5-codex": (5400, 520, 60000, 0, 900),
    "gpt-5.1": (4800, 410, 45000, 0, 640),
    "qwen3.7-plus": (6100, 700, 22000, 0, 1400),
    "qwen3-coder-plus": (7300, 850, 31000, 0, 0),
    "kimi-k2.5": (5200, 610, 18500, 0, 980),
    "glm-5": (4600, 520, 15000, 0, 700),
    "glm-4.7": (3900, 430, 12000, 0, 520),
    "MiniMax-M2.5": (3300, 390, 9800, 0, 0),
    "minilm-l12-v2": (120, 8, 0, 0, 0),
    "omp-default": (5000, 460, 26000, 300, 320),
}

# Dollars per million tokens: (in, out, cacheRead, cacheWrite)
PRICING = {
    "claude-opus-5": (15.0, 75.0, 1.5, 18.75),
    "claude-sonnet-4-5": (3.0, 15.0, 0.3, 3.75),
    "gpt-5-codex": (1.25, 10.0, 0.125, 0.0),
    "gpt-5.1": (1.25, 10.0, 0.125, 0.0),
    "qwen3.7-plus": (0.4, 1.2, 0.08, 0.0),
    "qwen3-coder-plus": (0.5, 2.0, 0.1, 0.0),
    "kimi-k2.5": (0.6, 2.5, 0.1, 0.0),
    "glm-5": (0.7, 2.8, 0.12, 0.0),
    "glm-4.7": (0.45, 1.8, 0.09, 0.0),
    "MiniMax-M2.5": (0.35, 1.4, 0.07, 0.0),
    "minilm-l12-v2": (0.0, 0.0, 0.0, 0.0),
    "omp-default": (1.0, 4.0, 0.1, 1.25),
}


def iso_utc(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def tokens_for(model: str, rng: random.Random) -> dict:
    inp, out, cr, cw, rs = PROFILES[model]

    def jitter(base: int) -> int:
        return max(0, int(base * rng.uniform(0.75, 1.35))) if base else 0

    return {"input": jitter(inp), "output": jitter(out), "cacheRead": jitter(cr),
            "cacheWrite": jitter(cw), "reasoning": jitter(rs)}


def cost_for(model: str, t: dict) -> float:
    pin, pout, pcr, pcw = PRICING[model]
    return round((t["input"] * pin + t["output"] * pout +
                  t["cacheRead"] * pcr + t["cacheWrite"] * pcw) / 1e6, 6)


DAY_MS = 24 * 3600_000


class Expect:
    """Accumulates the aggregate the engine is supposed to reproduce."""

    def __init__(self):
        self.models: dict[str, dict] = {}
        # 7 local midnights ending at FIXED_NOW's midnight — mirrors the
        # engine's DayIndex, so day buckets can be pinned exactly.
        midnight = dt.datetime.fromtimestamp(FIXED_NOW_MS / 1000.0).replace(
            hour=0, minute=0, second=0, microsecond=0)
        self.day_dates = [(midnight - dt.timedelta(days=6 - i)).strftime("%Y-%m-%d")
                          for i in range(7)]
        self.day_set = set(self.day_dates)
        self.day_tokens = {d: 0 for d in self.day_dates}
        self.day_requests = {d: 0 for d in self.day_dates}

    def add_day(self, ms: int, total_tokens: int) -> None:
        d = dt.datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d")
        if d in self.day_set:
            self.day_tokens[d] += total_tokens
            self.day_requests[d] += 1

    def add(self, model: str, t: dict, cost: float) -> None:
        b = self.models.setdefault(model, {
            "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
            "cacheWriteTokens": 0, "reasoningTokens": 0, "requests": 0, "cost": 0.0})
        b["inputTokens"] += t["input"]
        b["outputTokens"] += t["output"]
        b["cacheReadTokens"] += t["cacheRead"]
        b["cacheWriteTokens"] += t["cacheWrite"]
        b["reasoningTokens"] += t["reasoning"]
        b["requests"] += 1
        b["cost"] = round(b["cost"] + cost, 6)

    @property
    def total_tokens(self) -> int:
        return sum(b["inputTokens"] + b["outputTokens"] + b["cacheReadTokens"] +
                   b["cacheWriteTokens"] + b["reasoningTokens"]
                   for b in self.models.values())

    @property
    def total_requests(self) -> int:
        return sum(b["requests"] for b in self.models.values())


# --------------------------------------------------------------------------- #
# JSONL writers
# --------------------------------------------------------------------------- #

def write_claude(home: Path, rng: random.Random, expect: Expect) -> dict:
    models = ["claude-opus-5", "claude-sonnet-4-5"]
    n = 0
    for proj_index in range(6):
        proj = home / ".claude/projects" / f"-workspace-proj{proj_index}"
        proj.mkdir(parents=True, exist_ok=True)
        for session in range(8):
            sid = f"{proj_index:02d}{session:02d}"
            lines = []
            for i in range(60):
                model = models[(proj_index + session + i) % len(models)]
                t = tokens_for(model, rng)
                ms = FIXED_NOW_MS - rng.randrange(0, 30 * DAY_MS)
                cost = cost_for(model, t)
                expect.add(model, t, cost)
                expect.add_day(ms, t["input"] + t["output"] + t["cacheRead"]
                               + t["cacheWrite"] + t["reasoning"])
                n += 1
                lines.append(json.dumps({
                    "type": "assistant",
                    "timestamp": iso_utc(ms),
                    "sessionId": sid,
                    "cwd": "/workspace",
                    "message": {
                        "role": "assistant",
                        "model": model,
                        "stop_reason": "tool_use",
                        "content": [{"type": "text", "text": "x" * 32}],
                        "usage": {
                            "input_tokens": t["input"],
                            "cache_creation_input_tokens": t["cacheWrite"],
                            "cache_read_input_tokens": t["cacheRead"],
                            "output_tokens": t["output"],
                            "output_tokens_details": {"thinking_tokens": t["reasoning"]},
                            "cache_creation": {"ephemeral_5m_input_tokens": t["cacheWrite"],
                                               "ephemeral_1h_input_tokens": 0},
                        },
                    },
                }, separators=(",", ":")))
            (proj / f"session-{sid}.jsonl").write_text("\n".join(lines) + "\n",
                                                       encoding="utf-8")
    return {"source": "claude", "requests": n}


def write_codex(home: Path, rng: random.Random, expect: Expect) -> dict:
    models = ["gpt-5-codex", "gpt-5.1"]
    n = 0
    for session in range(10):
        day = FIXED_NOW_MS - rng.randrange(0, 30 * DAY_MS)
        d = dt.datetime.fromtimestamp(day / 1000.0, dt.timezone.utc)
        folder = home / ".codex/sessions" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}"
        folder.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(50):
            model = models[(session + i) % len(models)]
            t = tokens_for(model, rng)
            ms = FIXED_NOW_MS - rng.randrange(0, 30 * DAY_MS)
            cost = cost_for(model, t)
            expect.add(model, t, cost)
            expect.add_day(ms, t["input"] + t["output"] + t["cacheRead"]
                           + t["cacheWrite"] + t["reasoning"])
            n += 1
            lines.append(json.dumps({
                "type": "response",
                "timestamp": iso_utc(ms),
                "payload": {
                    "type": "event_msg",
                    "info": {
                        "model": model,
                        "token_usage": {
                            "input_tokens": t["input"],
                            "cached_input_tokens": t["cacheRead"],
                            "output_tokens": t["output"],
                            "reasoning_output_tokens": t["reasoning"],
                            "total_tokens": t["input"] + t["output"] + t["cacheRead"],
                        },
                    },
                    "rate_limits": {"limit_id": "codex"},
                },
            }, separators=(",", ":")))
        (folder / f"rollout-{session:04d}.jsonl").write_text("\n".join(lines) + "\n",
                                                             encoding="utf-8")
    return {"source": "codex", "requests": n}


def write_qwen(home: Path, rng: random.Random, expect: Expect) -> dict:
    """Qwen Code CLI: doubles as the Coding Plan local request census."""
    models = ["qwen3.7-plus", "qwen3-coder-plus", "kimi-k2.5", "glm-5", "glm-4.7",
              "MiniMax-M2.5"]
    n = 0
    windowed = {"5h": 0, "week": 0, "month": 0}
    tokens = 0
    for proj_index in range(5):
        chats = home / ".qwen/projects" / f"-workspace-app{proj_index}" / "chats"
        chats.mkdir(parents=True, exist_ok=True)
        for chat in range(10):
            lines = []
            for i in range(90):
                model = models[(proj_index * 7 + chat + i) % len(models)]
                t = tokens_for(model, rng)
                # Half the traffic sits inside the recent 5h window so the
                # Coding Plan meters have real numbers to render.
                if i % 2 == 0:
                    ms = FIXED_NOW_MS - rng.randrange(0, 4 * H5_MS)
                else:
                    ms = FIXED_NOW_MS - rng.randrange(0, 30 * DAY_MS)
                cost = cost_for(model, t)
                expect.add(model, t, cost)
                n += 1
                total = (t["input"] + t["output"] + t["cacheRead"] + t["reasoning"])
                expect.add_day(ms, total + t["cacheWrite"])
                tokens += total
                age = FIXED_NOW_MS - ms
                windowed["5h"] += 1 if age <= H5_MS else 0
                windowed["week"] += 1 if age <= 7 * DAY_MS else 0
                windowed["month"] += 1
                lines.append(json.dumps({
                    "type": "assistant",
                    "timestamp": iso_utc(ms),
                    "sessionId": f"{proj_index}{chat:02d}",
                    "model": model,
                    "usageMetadata": {
                        "promptTokenCount": t["input"],
                        "candidatesTokenCount": t["output"],
                        "totalTokenCount": total,
                        "promptTokensDetails": {"cachedContentTokenCount": t["cacheRead"]},
                        "candidatesTokensDetails": {"thinkingTokenCount": t["reasoning"]},
                    },
                }, separators=(",", ":")))
            (chats / f"chat-{chat:03d}.jsonl").write_text("\n".join(lines) + "\n",
                                                          encoding="utf-8")
    return {"source": "qwen", "requests": n,
            "planWindow": {**windowed, "tokens": tokens}}


def write_omp(home: Path, rng: random.Random, expect: Expect) -> dict:
    n = 0
    for proj_index in range(4):
        folder = home / ".omp/agent/sessions" / f"-workspace-x{proj_index}"
        folder.mkdir(parents=True, exist_ok=True)
        for session in range(6):
            lines = []
            for i in range(40):
                model = "omp-default"
                t = tokens_for(model, rng)
                ms = FIXED_NOW_MS - rng.randrange(0, 30 * DAY_MS)
                cost = cost_for(model, t)
                expect.add(model, t, cost)
                expect.add_day(ms, t["input"] + t["output"] + t["cacheRead"]
                               + t["cacheWrite"] + t["reasoning"])
                n += 1
                lines.append(json.dumps({
                    "type": "message",
                    "updatedAt": ms,
                    "message": {
                        "role": "assistant",
                        "model": model,
                        "provider": "alibaba-token-plan",
                        "timestamp": ms,
                        "stopReason": "stop",
                        "usage": {
                            "input": t["input"], "output": t["output"],
                            "cacheRead": t["cacheRead"], "cacheWrite": t["cacheWrite"],
                            "reasoningTokens": t["reasoning"],
                            "totalTokens": t["input"] + t["output"] + t["cacheRead"],
                            "cost": {"total": cost},
                        },
                    },
                }, separators=(",", ":")))
            (folder / f"{session:04d}.jsonl").write_text("\n".join(lines) + "\n",
                                                         encoding="utf-8")
    return {"source": "omp", "requests": n}


# --------------------------------------------------------------------------- #
# OpenCode SQLite
# --------------------------------------------------------------------------- #

def write_opencode(home: Path, rng: random.Random, expect: Expect,
                   rows: int = 30000) -> dict:
    share = home / ".local/share/opencode"
    share.mkdir(parents=True, exist_ok=True)
    db_path = share / "opencode.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,"
                 " time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.execute("CREATE INDEX idx_msg_time ON message(time_created)")
    models = ["qwen3.7-plus", "qwen3-coder-plus", "kimi-k2.5", "glm-5", "glm-4.7",
              "MiniMax-M2.5", "minilm-l12-v2", "claude-sonnet-4-5", "gpt-5-codex"]
    provider = "opencode-go"
    n = 0
    batch = []
    for i in range(rows):
        model = models[i % len(models)]
        # Assistant rows carry the token usage the scanner aggregates; every
        # 4th row is a user row (role != assistant) the SQL filter rejects.
        is_assistant = i % 4 != 3
        ms = FIXED_NOW_MS - rng.randrange(0, 30 * DAY_MS)
        if not is_assistant:
            data = json.dumps({"role": "user", "providerID": provider,
                               "time": {"created": ms}}, separators=(",", ":"))
        else:
            t = tokens_for(model, rng)
            cost = cost_for(model, t)
            expect.add(model, t, cost)
            expect.add_day(ms, t["input"] + t["output"] + t["cacheRead"]
                           + t["cacheWrite"] + t["reasoning"])
            n += 1
            data = json.dumps({
                "role": "assistant",
                "providerID": provider,
                "modelID": model,
                "cost": cost,
                "tokens": {"input": t["input"], "output": t["output"],
                           "reasoning": t["reasoning"],
                           "cache": {"read": t["cacheRead"], "write": t["cacheWrite"]}},
                "time": {"created": ms, "completed": ms + 900},
            }, separators=(",", ":"))
        batch.append((f"msg_{i:08d}", f"ses_{i % 400:04d}", ms, ms, data))
        if len(batch) >= 1000:
            conn.executemany("INSERT INTO message VALUES (?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO message VALUES (?,?,?,?,?)", batch)
    conn.commit()
    conn.close()
    (share / "auth.json").write_text(json.dumps(
        {"opencode-go": {"type": "api", "key": "oc-fixtured-key-000000"}}),
        encoding="utf-8")
    return {"source": "opencode", "requests": n, "dbRows": rows}


# --------------------------------------------------------------------------- #
# Credentials + quota fixtures
# --------------------------------------------------------------------------- #

def write_credentials(home: Path) -> None:
    gh = home / ".config/gh"
    gh.mkdir(parents=True, exist_ok=True)
    (gh / "hosts.yml").write_text(
        "github.com:\n"
        "    git_protocol: https\n"
        "    users:\n"
        "        h3nr1:\n"
        "    user: h3nr1\n"
        "    oauth_token: gho_fixturedcopilottoken0000000000\n", encoding="utf-8")
    (home / ".kimi-code").mkdir(parents=True, exist_ok=True)
    (home / ".kimi-code/config.toml").write_text(
        '[default]\napi_key = "sk-fixtured-kimi-key"\nmodel = "kimi-k2.5"\n',
        encoding="utf-8")
    (home / ".deepseek").mkdir(parents=True, exist_ok=True)
    (home / ".deepseek/config.toml").write_text(
        '[default]\napi_key = "sk-fixtured-deepseek-key"\n', encoding="utf-8")


def write_fixtures(out: Path) -> None:
    """Quota-plane fixtures: the exact response shapes the live endpoints
    return (source-verified), frozen so the benchmark never touches network."""
    fixtures = out / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)

    def dump(name: str, doc: object) -> None:
        (fixtures / name).write_text(json.dumps(doc, separators=(",", ":")),
                                     encoding="utf-8")

    dump("opencode.json", {"usage": {
        "rolling": {"status": "ok", "percent": 42.5, "limitDollars": 12,
                    "resetsAt": iso_utc(FIXED_NOW_MS + 2 * H5_MS)},
        "weekly": {"status": "ok", "percent": 78.25, "limitDollars": 30,
                   "resetsAt": iso_utc(FIXED_NOW_MS + 3 * DAY_MS)},
        "monthly": {"status": "ok", "percent": 15.0, "limitDollars": 60,
                    "resetsAt": iso_utc(FIXED_NOW_MS + 27 * DAY_MS)}}})
    dump("openrouter.credits.json", {"data": {"total_credits": 50.0,
                                              "total_usage": 21.37}})
    dump("openrouter.key.json", {"data": {"usage": 21.37, "limit": 25.0,
                                          "limit_reset": "weekly",
                                          "is_free_tier": False,
                                          "usage_daily": 3.1,
                                          "usage_weekly": 21.37,
                                          "usage_monthly": 21.37}})
    dump("kimi.json", {"data": {"available_balance": 128.44,
                                "total_balance": 500.0,
                                "voucher_balance": 0.0,
                                "cash_balance": 128.44}})
    dump("zai.json", {"code": 0, "data": {"limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 61.5,
         "nextResetTime": FIXED_NOW_MS + H5_MS},
        {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 34.25,
         "nextResetTime": FIXED_NOW_MS + 4 * DAY_MS}]}})
    dump("deepseek.json", {"is_available": True, "balance_infos": [
        {"currency": "CNY", "total_balance": "142.50", "granted_balance": "20.00",
         "topped_up_balance": "200.00"}]})
    dump("copilot.json", {"copilot_plan": "student", "access_type_sku": "student",
                          "quota_reset_date": "2026-10-01",
                          "quota_reset_date_utc": iso_utc(FIXED_NOW_MS + 28 * DAY_MS),
                          "quota_snapshots": {
                              "chat": {"entitlement": 300, "remaining": 91,
                                       "quota_remaining": 91, "unlimited": False},
                              "completions": {"entitlement": 300, "remaining": 250,
                                              "quota_remaining": 250,
                                              "unlimited": False},
                              "premium_interactions": {
                                  "entitlement": 1200, "remaining": 300.5,
                                  "quota_remaining": 300.5, "unlimited": False,
                                  "overage_permitted": False}}})

    (out / "env").write_text(
        "OPENCODE_GO_API_KEY=oc-env-key\n"
        "OPENROUTER_API_KEY=sk-or-env-key\n"
        "KIMI_API_KEY=sk-kimi-env-key\n"
        "ZAI_API_KEY=sk-zai-env-key\n"
        "DEEPSEEK_API_KEY=sk-ds-env-key\n"
        "GITHUB_TOKEN=gho_env_copilot_token\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the benchmark corpus")
    parser.add_argument("--out", default="bench/corpus")
    parser.add_argument("--seed", type=int, default=20260903)
    parser.add_argument("--opencode-rows", type=int, default=30000)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    manifest_path = out / "corpus.json"
    if manifest_path.is_file() and not args.force:
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            existing = {}
        if (existing.get("version") == CORPUS_VERSION
                and existing.get("seed") == args.seed
                and existing.get("opencodeRows") == args.opencode_rows
                and (out / "home").is_dir()):
            print(f"corpus up to date: {out}", file=sys.stderr)
            return 0

    rng = random.Random(args.seed)
    home = out / "home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)

    expect = Expect()
    sources = [
        write_opencode(home, rng, expect, rows=args.opencode_rows),
        write_claude(home, rng, expect),
        write_codex(home, rng, expect),
        write_qwen(home, rng, expect),
        write_omp(home, rng, expect),
    ]
    write_credentials(home)
    write_fixtures(out)

    manifest = {
        "version": CORPUS_VERSION,
        "seed": args.seed,
        "nowMs": FIXED_NOW_MS,
        "opencodeRows": args.opencode_rows,
        "expected": {
            "models": expect.models,
            "totalTokens": expect.total_tokens,
            "totalRequests": expect.total_requests,
            "recentDays": [{"date": d, "tokens": expect.day_tokens[d],
                            "requests": expect.day_requests[d]}
                           for d in expect.day_dates],
            "sources": sources,
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True),
                             encoding="utf-8")
    print(f"corpus: {out} · {expect.total_requests} requests · "
          f"{expect.total_tokens:,} tokens · {len(expect.models)} models",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
