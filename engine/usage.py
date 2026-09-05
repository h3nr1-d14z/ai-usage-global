#!/usr/bin/env python3
"""AI Usage Global — multi-provider usage + local consumption engine.

One JSON document on stdout, read verbatim by Panel.qml. Stdlib-only.

Two data planes:

  QUOTA (kind: percent/balance)  — per-provider limit windows and balances.
    Live mode: HTTPS with the user's own credentials (Tier-1 scoped keys).
    Fixture mode: JSON files under $AIUSAGE_FIXTURES, byte-identical runs.

  CONSUMPTION (kind: local)      — token accounting scanned from the agent
    stores that already exist on disk (OpenCode SQLite, Claude/Codex/Qwen/
    OMP JSONL). Offline, no credentials, per-model + per-day breakdowns.

Determinism hooks (used by the benchmark harness; all optional):
  AIUSAGE_NOW=ms          frozen wall clock
  AIUSAGE_HOME=path       fake $HOME with the agent stores laid out inside
  AIUSAGE_ENV_FILE=path   dotenv with provider keys
  AIUSAGE_FIXTURES=dir    quota transport = fixture files, never network

Canonical document:
  { schema: 1, nowMs, generatedAtMs,
    providers: [ ProviderRecord ],      # quota plane, ordered
    local: { models: {model: {inputTokens, outputTokens, cacheReadTokens,
           cacheWriteTokens, reasoningTokens, requests, cost}},
           recentDays: [{date, tokens, requests}],
           todayTokens, todayRequests, weekTokens, totalTokens,
           totalRequests, sources: [{source, requests, models}] },
    timings: {quotaMs, localMs},
    errors: [str] }

  ProviderRecord:
    { id, name, display, configured, kind: "percent"|"balance"|"note",
      label, value, currency, detail,
      windows: [{ id, label, spanMs, percent(0..999|null), used, total,
                  unit, resetsAt(iso|null) }],
      error: str|null }
"""

from __future__ import annotations

import bisect
import datetime as dt
import glob
import json
import math
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 65536

# --------------------------------------------------------------------------- #
# Environment / determinism
# --------------------------------------------------------------------------- #

def env_home() -> Path:
    return Path(os.environ.get("AIUSAGE_HOME") or os.path.expanduser("~"))


def now_ms() -> int:
    fixed = os.environ.get("AIUSAGE_NOW", "").strip()
    if fixed.isdigit():
        return int(fixed)
    return int(time.time() * 1000)


def iso(ms: int | float) -> str:
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def local_date(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d")


def finite(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def round_up(value: float, digits: int = 1) -> float:
    """Round half away from zero (display percentages must not drift down
    via banker's rounding: 78.25% renders as 78.3, not 78.2)."""
    factor = 10 ** digits
    return math.floor(abs(value) * factor + 0.5) / factor * (1 if value >= 0 else -1)


def safe_error(exc: Exception) -> str:
    """Transport errors surface in the panel; never let a bearer token or
    key-shaped substring ride along in an exception message. Redact FIRST,
    then truncate — truncating first could cut mid-token and defeat the
    pattern, leaking the token's tail."""
    import re
    red = re.sub(r"(gho_|sk-|Bearer\s+|token[=:\"']+)[A-Za-z0-9_\-]{8,}",
                 r"\1[redacted]", str(exc))
    return red[:120]


# --------------------------------------------------------------------------- #
# Transport: live HTTPS or deterministic fixtures
# --------------------------------------------------------------------------- #

_FIXTURES_DIR: Path | None = None


def fixtures_dir() -> Path | None:
    raw = os.environ.get("AIUSAGE_FIXTURES") or ""
    if not raw:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    return path if path.is_dir() else None


def fetch_json(url: str, headers: dict[str, str] | None = None) -> object:
    """Live transport. Only reached when AIUSAGE_FIXTURES is unset."""
    import urllib.request  # lazy: ~70ms import, never needed offline
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "h3nr1.d14z-ai-usage/1.0")
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    return json.loads(raw[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace"))


def post_form(url: str, fields: dict[str, str],
              headers: dict[str, str] | None = None) -> object:
    """Live transport for form-encoded POSTs. Only reached when
    AIUSAGE_FIXTURES is unset."""
    import urllib.request
    import urllib.parse  # lazy: cold-path only
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", "h3nr1.d14z-ai-usage/1.0")
    req.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)  # add_header replaces defaults (UA/Accept)
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    return json.loads(raw[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace"))


def fetch_fixture(provider_id: str, key: str = "main") -> object:
    """Deterministic offline transport: <provider>.<key>.json falling back
    to <provider>.json inside $AIUSAGE_FIXTURES."""
    assert _FIXTURES_DIR is not None
    for name in (f"{provider_id}.{key}.json", f"{provider_id}.json"):
        path = _FIXTURES_DIR / name
        if path.is_file():
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(name)


# --------------------------------------------------------------------------- #
# Credential resolution: env → dotenv → native config files
# --------------------------------------------------------------------------- #

def _load_dotenv(path: Path, into: dict[str, str]) -> None:
    try:
        if not path.is_file() or path.stat().st_size > 65536:
            return
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip("'\"")
            if k.strip() and v:
                into[k.strip()] = v
    except OSError:
        pass


def env_keys() -> dict[str, str]:
    """Credential env, lowest to highest precedence: the plugin's own dotenv
    (~/.config/ai-usage/env — bar plugins inherit the shell's env, which often
    lacks your exported keys on Wayland), then $AIUSAGE_ENV_FILE, then real env."""
    out: dict[str, str] = {}
    _load_dotenv(env_home() / ".config/ai-usage/env", out)
    env_file = os.environ.get("AIUSAGE_ENV_FILE", "")
    if env_file:
        _load_dotenv(Path(os.path.expandvars(os.path.expanduser(env_file))), out)
    out.update({k: v for k, v in os.environ.items() if v})
    return out


_ENV: dict[str, str] = {}


def get_key(*names: str) -> str | None:
    for name in names:
        value = _ENV.get(name, "").strip()
        if value:
            return value
    return None


def read_config_key(file_path: Path, pattern: str) -> str | None:
    """Regex-extract a credential from a TOML/YAML-ish native config file."""
    import re  # lazy: cold-path only (native key fallbacks)
    try:
        if not file_path.is_file() or file_path.stat().st_size > 65536:
            return None
        m = re.search(pattern, file_path.read_text(encoding="utf-8", errors="replace"))
        return m.group(1) if m else None
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# Provider record helpers
# --------------------------------------------------------------------------- #

H5 = 5 * 3600_000
WEEK = 7 * 24 * 3600_000
MONTH = 30 * 24 * 3600_000


# Cost policy: report ONLY what each store actually embeds — never fabricate
# from a pricing table (a local widget has no reliable per-model price, and a
# generator-side table mirrored into the engine would be a shared-
# misunderstanding oracle, not a cross-check). OpenCode (DB $.cost) and OMP
# (usage.cost.total) embed per-request cost; Claude Code embeds a top-level
# costUSD in legacy formats (absent in current versions -> 0.0); Codex/Qwen
# transcripts carry no cost at all. validate.py pins this per model.


def parse_ts_ms(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        return int(value * 1000) if value < 1e11 else int(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    try:
        return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return int(dt.datetime.strptime(text, fmt).replace(
                tzinfo=dt.timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


def window(wid: str, label: str, span_ms: int, percent=None, used=None,
           total=None, unit: str = "", resets_at_ms: int | float | None = None) -> dict:
    pct = None
    if percent is not None:
        pct = round_up(clamp(finite(percent), 0.0, 999.0), 1)
    elif used is not None and total:
        pct = round_up(clamp(finite(used) / finite(total) * 100.0, 0.0, 999.0), 1)
    return {
        "id": wid, "label": label, "spanMs": span_ms, "percent": pct,
        "used": None if used is None else round(finite(used), 2),
        "total": None if total is None else round(finite(total), 2),
        "unit": unit,
        "resetsAt": iso(resets_at_ms) if resets_at_ms else None,
    }


def record(provider: dict, **fields) -> dict:
    base = {
        "id": provider["id"], "name": provider["name"], "display": provider["display"],
        "configured": False, "kind": "note", "label": "", "value": None,
        "currency": "", "detail": "", "windows": [], "error": None,
        "keyEnv": provider.get("keyEnv", ""),
    }
    base.update(fields)
    return base


def percent_headline(windows: list[dict]) -> float | None:
    live = [w["percent"] for w in windows if w["percent"] is not None]
    return max(live, default=None)


# --------------------------------------------------------------------------- #
# QUOTA adapters (endpoint shapes source-verified 2026-09)
# --------------------------------------------------------------------------- #

def fetch_opencode(ctx: dict) -> dict:
    p = ctx["provider"]
    key = get_key("OPENCODE_GO_API_KEY")
    if not key:
        try:
            auth = ctx["home"] / ".local/share/opencode/auth.json"
            if auth.is_file() and auth.stat().st_size < 65536:
                doc = json.loads(auth.read_text(encoding="utf-8"))
                entry = doc.get("opencode-go") if isinstance(doc, dict) else None
                if isinstance(entry, dict):
                    key = entry.get("key") or None
        except (OSError, ValueError):
            pass
    if not key:
        return record(p, error="no-key")
    try:
        body = (fetch_fixture(p["id"]) if _FIXTURES_DIR else
                fetch_json("https://opencode.ai/zen/go/v1/usage",
                           {"Authorization": f"Bearer {key}"}))
    except Exception as exc:  # noqa: BLE001
        return record(p, error=safe_error(exc))
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return record(p, error="unexpected-response")
    windows = []
    for wid, label, span in (("rolling", "5h", H5), ("weekly", "W", WEEK),
                             ("monthly", "M", MONTH)):
        raw = usage.get(wid) if isinstance(usage.get(wid), dict) else {}
        windows.append(window(wid, label, span, percent=raw.get("percent"),
                              resets_at_ms=parse_ts_ms(raw.get("resetsAt"))))
    headline = percent_headline(windows)
    return record(p, configured=True, kind="percent",
                  label=f"{round(headline)}%" if headline is not None else "—",
                  value=headline, windows=windows)


def fetch_openrouter(ctx: dict) -> dict:
    p = ctx["provider"]
    key = get_key("OPENROUTER_API_KEY")
    if not key:
        return record(p, error="no-key")
    try:
        if _FIXTURES_DIR:
            credits = fetch_fixture(p["id"], "credits")
            keyinfo = fetch_fixture(p["id"], "key")
        else:
            import urllib.error
            hdr = {"Authorization": f"Bearer {key}"}
            try:
                credits = fetch_json("https://openrouter.ai/api/v1/credits", hdr)
            except urllib.error.HTTPError as err:
                if err.code == 403:
                    return record(p, error="HTTP 403 — /credits needs a management key")
                raise
            keyinfo = fetch_json("https://openrouter.ai/api/v1/key", hdr)
    except Exception as exc:  # noqa: BLE001
        return record(p, error=safe_error(exc))
    cd = credits.get("data") if isinstance(credits, dict) else None
    kd = keyinfo.get("data") if isinstance(keyinfo, dict) else None
    if not isinstance(cd, dict) or cd.get("total_credits") is None:
        return record(p, error="unexpected-response")
    total = finite(cd["total_credits"])
    used = finite(cd.get("total_usage"))
    remaining = max(0.0, total - used)
    windows = []
    if isinstance(kd, dict) and finite(kd.get("limit")) > 0:
        span = {"daily": 24 * 3600_000, "weekly": WEEK, "monthly": MONTH}.get(
            str(kd.get("limit_reset")), 0)
        windows.append(window("keylimit", "KEY", span, used=finite(kd.get("usage")),
                              total=finite(kd["limit"]), unit="USD"))
    return record(p, configured=True, kind="balance", label=f"${remaining:,.2f}",
                  value=round(remaining, 2), currency="USD",
                  detail=f"${remaining:,.2f} left of ${total:,.2f}", windows=windows)


def fetch_kimi(ctx: dict) -> dict:
    p = ctx["provider"]
    key = get_key("KIMI_API_KEY", "MOONSHOT_API_KEY")
    if not key:
        key = read_config_key(ctx["home"] / ".kimi-code/config.toml",
                              r'api_key\s*=\s*"([^"]+)"')
    if not key:
        return record(p, error="no-key")
    # CN (¥) by default; AIUSAGE_KIMI_INTL=1 switches to api.moonshot.ai ($).
    # Keys are NOT cross-compatible between regions.
    intl = os.environ.get("AIUSAGE_KIMI_INTL", "").strip() in ("1", "true", "yes")
    host = "api.moonshot.ai" if intl else "api.moonshot.cn"
    symbol, currency = ("$", "USD") if intl else ("¥", "CNY")
    try:
        body = (fetch_fixture(p["id"]) if _FIXTURES_DIR else
                fetch_json(f"https://{host}/v1/users/me/balance",
                           {"Authorization": f"Bearer {key}"}))
    except Exception as exc:  # noqa: BLE001
        return record(p, error=safe_error(exc))
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or data.get("available_balance") is None:
        return record(p, error="unexpected-response")
    balance = finite(data["available_balance"])
    # The docs guarantee available/voucher/cash; total_balance appears on some
    # accounts only. Never synthesize a 0% window from a missing total.
    total_raw = data.get("total_balance")
    if total_raw is not None:
        total = finite(total_raw)
        windows = ([window("billing", symbol, 0, used=max(0.0, total - balance),
                           total=total, unit=currency)] if total > 0 else [])
        detail = f"{symbol}{balance:,.2f} available of {symbol}{total:,.2f}"
    else:
        windows = []
        cash = finite(data.get("cash_balance"))
        voucher = finite(data.get("voucher_balance"))
        parts = [f"{symbol}{balance:,.2f} available"]
        if cash:
            parts.append(f"cash {symbol}{cash:,.2f}")
        if voucher:
            parts.append(f"voucher {symbol}{voucher:,.2f}")
        detail = " · ".join(parts)
    return record(p, configured=True, kind="balance", label=f"{symbol}{balance:,.2f}",
                  value=round(balance, 2), currency=currency,
                  detail=detail, windows=windows)


def fetch_zai(ctx: dict) -> dict:
    p = ctx["provider"]
    key = get_key("ZAI_API_KEY", "ZHIPUAI_API_KEY")
    if not key:
        return record(p, error="no-key")
    try:
        if _FIXTURES_DIR:
            body = fetch_fixture(p["id"])
        else:
            import urllib.error
            url = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
            try:
                body = fetch_json(url, {"Authorization": f"Bearer {key}"})
            except urllib.error.HTTPError as err:
                if err.code != 401:
                    raise
                # Region implementations disagree: some want the bare key.
                body = fetch_json(url, {"Authorization": key})
    except Exception as exc:  # noqa: BLE001
        return record(p, error=safe_error(exc))
    data = body.get("data") if isinstance(body, dict) else None
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list):
        return record(p, error="unexpected-response")
    windows = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") not in (None, "", "TOKENS_LIMIT"):
            continue  # request-per-minute entries are not subscription meters
        # unit: 3=hours, 6=weeks; `number` pairs with the unit. percentage = USED %.
        unit = int(finite(entry.get("unit")))
        number = int(finite(entry.get("number")))
        span = {3: number * 3600_000, 6: number * WEEK}.get(unit, 0)
        label = ("5h" if span == H5 else
                 "W" if span in (WEEK, 2 * WEEK) else
                 f"{max(1, round(span / 3600_000))}h" if span else "?")
        windows.append(window(label.lower(), label, span,
                              percent=entry.get("percentage"),
                              resets_at_ms=finite(entry.get("nextResetTime")) or None))
    headline = percent_headline(windows)
    return record(p, configured=True, kind="percent",
                  label=f"{round(headline)}%" if headline is not None else "—",
                  value=headline, windows=windows)


def fetch_deepseek(ctx: dict) -> dict:
    p = ctx["provider"]
    key = get_key("DEEPSEEK_API_KEY")
    if not key:
        key = read_config_key(ctx["home"] / ".deepseek/config.toml",
                              r'api_key\s*=\s*"([^"]+)"')
    if not key:
        return record(p, error="no-key")
    try:
        body = (fetch_fixture(p["id"]) if _FIXTURES_DIR else
                fetch_json("https://api.deepseek.com/user/balance",
                           {"Authorization": f"Bearer {key}"}))
    except Exception as exc:  # noqa: BLE001
        return record(p, error=safe_error(exc))
    infos = body.get("balance_infos") if isinstance(body, dict) else None
    if not isinstance(infos, list) or not infos:
        return record(p, error="unexpected-response")
    total_left = sum(finite(e.get("total_balance")) for e in infos if isinstance(e, dict))
    granted = sum(finite(e.get("granted_balance")) + finite(e.get("topped_up_balance"))
                  for e in infos if isinstance(e, dict))
    used = max(0.0, granted - total_left)
    windows = ([window("billing", "$", 0, used=used, total=granted, unit="USD")]
               if granted > 0 else [])
    return record(p, configured=True, kind="balance", label=f"${total_left:,.2f}",
                  value=round(total_left, 2), currency="USD",
                  detail=f"${total_left:,.2f} balance", windows=windows)


def fetch_copilot(ctx: dict) -> dict:
    p = ctx["provider"]
    token = get_key("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN")
    if not token:
        # gh stores OAuth in YAML; regex the token out to stay stdlib-only.
        token = read_config_key(ctx["home"] / ".config/gh/hosts.yml",
                                r"oauth_token:\s*(\S+)")
    if not token:
        return record(p, error="no-token")
    try:
        body = (fetch_fixture(p["id"]) if _FIXTURES_DIR else
                fetch_json("https://api.github.com/copilot_internal/user",
                           {"Authorization": f"Bearer {token}",
                            "Editor-Version": "ai-usage/1.0"}))
    except Exception as exc:  # noqa: BLE001
        return record(p, error=safe_error(exc))
    if not isinstance(body, dict) or not isinstance(body.get("quota_snapshots"), dict):
        return record(p, error="unexpected-response")
    snap = body["quota_snapshots"]
    reset_ms = parse_ts_ms(body.get("quota_reset_date_utc") or body.get("quota_reset_date"))
    windows = []
    for entry_key, wid, label in (("premium_interactions", "premium", "PREM"),
                                  ("chat", "month", "M"),
                                  ("completions", "month", "M")):
        entry = snap.get(entry_key)
        if not isinstance(entry, dict) or entry.get("unlimited"):
            continue
        entitlement = finite(entry.get("entitlement") or entry.get("quota_total"), -1)
        remaining = finite(entry.get("quota_remaining", entry.get("remaining")), -1)
        if entitlement <= 0 or remaining < 0:
            continue
        span = 0 if entry_key == "premium_interactions" else MONTH
        windows.append(window(wid, label, span, used=entitlement - remaining,
                              total=entitlement,
                              unit="credits" if entry_key == "premium_interactions" else "",
                              resets_at_ms=reset_ms))
    headline = percent_headline(windows)
    plan = str(body.get("copilot_plan") or body.get("access_type_sku") or "")
    return record(p, configured=True, kind="percent",
                  label=f"{round(headline)}%" if headline is not None else (plan[:6] or "—"),
                  value=headline, windows=windows,
                  detail=plan)


QWEN_USAGE_API = "zeldaHttp.apikeyMgr./tokenplan/personal/api/v2/usage"
QWEN_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/143.0.0.0 Safari/537.36")


def _cookie_value(cookie: str, name: str) -> str | None:
    for seg in cookie.split(";"):
        k, sep, v = seg.partition("=")
        if sep and k.strip() == name and v.strip():
            return v.strip()
    return None


def _used_fraction(value) -> float | None:
    n = finite(value, -1.0)
    if n < 0:
        return None
    if n > 1:
        n /= 100.0
    return min(1.0, n)


def qwen_credential(ctx: dict) -> tuple[str, bool] | None:
    """(cookie, is_china) for the console usage API. Precedence: env
    QWEN_PLAN_COOKIE (international) → OMP agent store auto-detect (its
    alibaba-token-plan credential embeds the browser cookie; baseUrl marks
    the China region). None → local census fallback."""
    cookie = _ENV.get("QWEN_PLAN_COOKIE", "").strip()
    if cookie:
        return cookie, False
    db = ctx["home"] / ".omp/agent/agent.db"
    if not db.is_file():
        return None
    import sqlite3  # lazy: only when an OMP store exists
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT data FROM auth_credentials"
                " WHERE provider='alibaba-token-plan'"
                " ORDER BY updated_at DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or not isinstance(row[0], str):
        return None
    try:
        outer = json.loads(row[0])
        inner = outer.get("key") if isinstance(outer, dict) else None
        if not isinstance(inner, str) or not inner.startswith("{"):
            return None  # bare sk- key: OMP holds no cookie for it
        cred = json.loads(inner)
        cookie = cred.get("cookie")
        if not isinstance(cookie, str) or not cookie.strip():
            return None
        base = cred.get("baseUrl") or ""
        return cookie.strip(), isinstance(base, str) and "cn-beijing" in base
    except ValueError:
        return None


def fetch_qwen_console(cookie: str, is_china: bool) -> list[dict] | None:
    """Real 5h/7d credit percentages via the console gateway — the flow OMP
    ships (port of pi-ai/src/usage/alibaba-token-plan.ts). The cookie is a
    browser session credential: used for the two requests, NEVER stored in
    the emitted document. Any failure → None → census fallback."""
    import re as _re
    import uuid
    from urllib.parse import quote
    if is_china:
        origin = "https://bailian.console.aliyun.com"
        session_url = origin + "/cn-beijing?tab=plan"
        action, region = "BroadScopeAspnGateway", "cn-beijing"
        usage_url = ("https://bailian-cs.console.aliyun.com/data/api.json?action="
                     + action + "&product=sfm_bailian&api=" + quote(QWEN_USAGE_API))
        cornerstone = {"feTraceId": str(uuid.uuid4()),
                       "feURL": session_url + "#/efm/subscription/token-plan/personal",
                       "protocol": "V2", "console": "ONE_CONSOLE",
                       "productCode": "p_efm", "switchAgent": 12608464,
                       "switchUserType": 3, "domain": "bailian.console.aliyun.com",
                       "consoleSite": "BAILIAN_ALIYUN", "userNickName": "",
                       "userPrincipalName": "", "xsp_lang": "zh-CN"}
    else:
        origin = "https://home.qwencloud.com"
        session_url = origin + "/tool/user/info.json"
        action, region = "IntlBroadScopeAspnGateway", "ap-southeast-1"
        usage_url = ("https://cs-data.qwencloud.com/data/api.json?product=sfm_bailian&action="
                     + action + "&api=" + quote(QWEN_USAGE_API))
        cornerstone = {"domain": "home.qwencloud.com", "consoleSite": "QWENCLOUD",
                       "console": "ONE_CONSOLE", "xsp_lang": "en-US",
                       "protocol": "V2", "productCode": "p_efm"}

    hdr = {"Cookie": cookie, "User-Agent": QWEN_BROWSER_UA, "Referer": origin + "/"}
    if _FIXTURES_DIR:
        session = fetch_fixture("qwen", "session")
    elif is_china:
        import urllib.request
        req = urllib.request.Request(session_url)
        for k, v in hdr.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            html = resp.read(4 * MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
        m = _re.search(r'\bSEC_TOKEN\s*:\s*"([^"]+)"', html)
        if not m:
            return None
        session = {"data": {"secToken": m.group(1)}}
    else:
        session = fetch_json(session_url, hdr)
    if not isinstance(session, dict) or not isinstance(session.get("data"), dict):
        return None
    sec_token = session["data"].get("secToken")
    if not isinstance(sec_token, str) or not sec_token:
        return None

    csrf = _cookie_value(cookie, "login_aliyunid_csrf") or _cookie_value(cookie, "csrf")
    post_hdr = {"Cookie": cookie, "User-Agent": QWEN_BROWSER_UA,
                "Origin": origin, "Referer": session_url,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*"}
    if csrf:
        post_hdr["x-xsrf-token"] = csrf
        post_hdr["x-csrf-token"] = csrf
    fields = {"product": "sfm_bailian", "action": action, "region": region,
              "sec_token": sec_token,
              "params": json.dumps({"Api": QWEN_USAGE_API,
                                    "Data": {"cornerstoneParam": cornerstone},
                                    "V": "1.0"})}
    payload = fetch_fixture("qwen", "usage") if _FIXTURES_DIR else \
        post_form(usage_url, fields, post_hdr)
    if not isinstance(payload, dict) or payload.get("successResponse") is False:
        return None
    node = payload.get("data")
    if not isinstance(node, dict):
        return None
    # Envelope unwrap mirrors OMP's X8r: a JSON-string "Data" may wrap the
    # payload, "DataV2".data may wrap that, and one further "data" member
    # may hold the usage fields (live intl: data.DataV2.data.data.{per…}).
    if isinstance(node.get("Data"), str):
        try:
            parsed = json.loads(node["Data"])
            if isinstance(parsed, dict):
                node = parsed
        except ValueError:
            pass
    dv2 = node.get("DataV2")
    if isinstance(dv2, dict) and isinstance(dv2.get("data"), dict):
        node = dv2["data"]
    if isinstance(node.get("data"), dict):
        node = node["data"]
    f5 = _used_fraction(node.get("per5HourPercentage"))
    fw = _used_fraction(node.get("per1WeekPercentage"))
    windows = []
    if f5 is not None:
        windows.append(window("rolling", "5h", H5, percent=f5 * 100.0,
                              resets_at_ms=_reset_ms(node.get("per5HourResetTime"))))
    if fw is not None:
        windows.append(window("weekly", "W", WEEK, percent=fw * 100.0,
                              resets_at_ms=_reset_ms(node.get("per1WeekResetTime"))))
    return windows or None


def _reset_ms(value) -> int | None:
    """Gateway reset times: epoch seconds/ms, numeric strings, or ISO."""
    n = finite(value, -1.0)
    return parse_ts_ms(n) if n > 0 else parse_ts_ms(value)


def fetch_qwen(ctx: dict) -> dict:
    """Alibaba Cloud Model Studio Coding Plan (Qwen token plan).

    Console gateway first: with a session cookie (QWEN_PLAN_COOKIE env or
    auto-detected from the OMP agent.db) the real 5h/7d credit percentages
    and reset times come from the dashboard's api.json endpoint. There is
    no public usage endpoint for sk-sp-* keys (dashscope quota paths 404;
    verified in steipete/CodexBar#612), so without a cookie — or when the
    gateway fails — the quota plane falls back to LOCAL Qwen Code CLI
    transcripts (~/.qwen/projects/*/chats/*.jsonl) counted against the plan
    caps (Pro: 6k req/5h, 45k/wk, 90k/mo; configurable). Same unit the
    official console dashboard shows: request counts."""
    p = ctx["provider"]
    caps = ctx.get("caps") or {}
    cap5 = max(1, int(finite(caps.get("qwenPlanCap5h"), 6000)))
    capw = max(1, int(finite(caps.get("qwenPlanCapWeek"), 45000)))
    capm = max(1, int(finite(caps.get("qwenPlanCapMonth"), 90000)))
    # Console gateway first (real percentages, resets) when a session cookie
    # is available; the local census below is the no-cookie fallback.
    cred = qwen_credential(ctx)
    if cred is not None:
        try:
            wins = fetch_qwen_console(cred[0], cred[1])
        except Exception:  # noqa: BLE001 — console is best-effort
            wins = None
        if wins:
            headline = percent_headline(wins)
            return record(p, configured=True, kind="percent",
                          label=f"{round(headline)}%" if headline is not None else "—",
                          value=headline, windows=wins, detail="console")
    root = ctx["home"] / ".qwen/projects"
    if not root.is_dir():
        return record(p, error="no-local-store")
    now = ctx["nowMs"]
    # The consumption scan walks the same transcripts; when both planes run,
    # wait for its census instead of re-reading the files ourselves.
    census = ctx.get("qwenCensus")
    event = ctx.get("qwenCensusEvent")
    if census is None and event is not None:
        if event.wait(timeout=TIMEOUT_SECONDS):
            census = ctx.get("qwenCensus")
        event = None  # one wait per build is plenty; then fall through
    if census is None:
        req5 = reqw = reqm = 0
        for path in glob.glob(str(root / "*" / "chats" / "*.jsonl")):
            try:
                if os.path.getsize(path) > 8 * 1024 * 1024:
                    continue
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"timestamp"' not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except (ValueError, RecursionError):
                            continue
                        ms = parse_ts_ms(d.get("timestamp"))
                        if ms is None or ms > now:
                            continue
                        age = now - ms
                        req5 += 1 if age <= H5 else 0
                        reqw += 1 if age <= WEEK else 0
                        reqm += 1 if age <= MONTH else 0
            except OSError:
                continue
    else:
        req5, reqw, reqm = census
    if not (req5 or reqw or reqm):
        return record(p, error="no-local-usage")

    def week_reset() -> int:  # next Monday 00:00 in UTC+8 (plan timezone)
        t = dt.datetime.fromtimestamp(now / 1000.0, dt.timezone(dt.timedelta(hours=8)))
        days = (7 - t.weekday()) % 7 or 7
        nxt = (t + dt.timedelta(days=days)).replace(hour=0, minute=0, second=0,
                                                    microsecond=0)
        return int(nxt.timestamp() * 1000)

    windows = [
        window("rolling", "5h", H5, used=req5, total=cap5, unit="req"),
        window("weekly", "W", WEEK, used=reqw, total=capw, unit="req",
               resets_at_ms=week_reset()),
        window("monthly", "M", MONTH, used=reqm, total=capm, unit="req"),
    ]
    headline = percent_headline(windows)
    return record(p, configured=True, kind="percent",
                  label=f"{round(headline)}%" if headline is not None else "0%",
                  value=headline, windows=windows,
                  detail=f"local · {req5}/{cap5} 5h · {reqw}/{capw} wk · {reqm}/{capm} mo req")


# --------------------------------------------------------------------------- #
# New-API gateways (QuantumNous/new-api): OMP models.yml providers whose host
# speaks the new-api console API. The OpenAI-compatible billing endpoints
# report lifetime spend (total_usage, US cents) and quota (hard_limit_usd =
# remaining + used = TOTAL per controller/billing.go; new-api's
# "unlimited" sentinel is 1e8). With DisplayTokenStatEnabled off — the
# agentrouter case, verified live — both figures are USER-level (account
# spend), not per-token; token-stat sites scope them to the calling token.
# Sites can also render CNY or raw-token display types (QuotaDisplayType):
# the "$" here assumes the USD default agentrouter uses. Some forks
# ignore the billing date params (agentrouter: disjoint windows return
# identical sums), so the figure is treated as lifetime-cumulative and
# today's spend is derived from the persisted history snapshots instead.
# The gateway key resolves the way OMP resolves it: an env NAME in
# models.yml → ~/.omp/agent/.env (the file OMP loads) → the panel's own
# env store; a literal value is used as-is.
# --------------------------------------------------------------------------- #

def _omp_dotenv(ctx: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    _load_dotenv(ctx["home"] / ".omp/agent/.env", out)
    return out


def _omp_models_providers(ctx: dict) -> dict[str, dict]:
    """providers: section of OMP's models.yml → {name: {baseUrl, apiKey}}.
    A subset parser for the shape OMP writes (2-space providers, 4-space
    flat mappings); nested model lists are skipped. Fail-soft."""
    path = ctx["home"] / ".omp/agent/models.yml"
    try:
        if not path.is_file() or path.stat().st_size > 262144:
            return {}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    import re
    section = current = ""
    out: dict[str, dict] = {}
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            section, current = line.split(":", 1)[0].strip(), ""
            continue
        if section != "providers":
            continue
        m = re.match(r"^  (\S[^:]*):\s*$", line)
        if m:
            current = m.group(1).strip()
            out.setdefault(current, {})
            continue
        m = re.match(r"^    ([A-Za-z][A-Za-z0-9]*): (.+)$", line)
        if m and current and m.group(1) in ("baseUrl", "apiKey"):
            out[current][m.group(1)] = m.group(2).strip().strip("'\"")
    return out


def newapi_gateways(ctx: dict) -> list[dict]:
    """Candidate gateway providers (disk-only; new-api-ness is confirmed by
    fetch_newapi via /api/status, so a non-gateway host costs one request
    and then drops out). Host-deduped, name-sorted, never colliding with
    the static quota registry ids."""
    import re
    env = _omp_dotenv(ctx)
    static = {p["id"] for p in PROVIDERS}
    hosts: set[str] = set()
    out: list[dict] = []
    for name, cfg in sorted(_omp_models_providers(ctx).items()):
        if name in static:
            continue
        base = (cfg.get("baseUrl") or "").strip()
        if not base.startswith(("http://", "https://")):
            continue
        root = base.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3].rstrip("/")
        host = root.split("://", 1)[1].split("/", 1)[0]
        if not host or host in hosts:
            continue
        hosts.add(host)
        field = (cfg.get("apiKey") or "").strip()
        key_env, key = "", None
        if field and re.fullmatch(r"[A-Z][A-Z0-9_]*", field):
            key_env = field
            key = env.get(field) or _ENV.get(field) or None
        elif field:
            key_env = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_API_KEY"
            key = field
        out.append({"id": name, "name": name, "display": name[:2].upper(),
                    "keyEnv": key_env, "baseUrl": root, "apiKey": key})
    return out


def _gateway_spent_today(ctx: dict, gateway_id: str, current_usd: float,
                         scope: str) -> float | None:
    """Today's spend = the tracked figure now minus the last snapshot from a
    previous day (normally yesterday's final refresh). Snapshots are scope-
    tagged ({usd, scope}); a bare number is the legacy token-scope format.
    No baseline, scope change, or counter going backwards (reset/rotation)
    → None: unknown is honest, negative is not."""
    try:
        data = json.loads((ctx["home"] / ".local/state/h3nr1.d14z.ai-usage"
                           "/history.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    days = data.get("days") if isinstance(data, dict) else None
    prior = days.get(local_date(ctx["nowMs"] - 86400_000)) \
        if isinstance(days, dict) else None
    snap = (prior.get("spend") or {}).get(gateway_id) \
        if isinstance(prior, dict) else None
    if isinstance(snap, dict):
        prev, prev_scope = finite(snap.get("usd"), -1.0), snap.get("scope")
    else:
        prev, prev_scope = finite(snap, -1.0), "token"
    if prev < 0 or prev_scope != scope or current_usd < prev:
        return None
    return round(current_usd - prev, 2)


def _newapi_user_credential(ctx: dict, p: dict) -> tuple[str, str] | None:
    """(access_token, user_id) for user-level billing, or None. Some forks
    (agentrouter) require the New-Api-User header on /api/user/* — the PAT
    alone 401s. Names derive from the gateway id: AGENTROUTER_ACCESS_TOKEN /
    AGENTROUTER_USER_ID, resolved against OMP's .env then the panel env."""
    import re
    stem = re.sub(r"[^A-Z0-9]+", "_", p["id"].upper()).strip("_")
    if not stem:
        return None
    env = _omp_dotenv(ctx)
    pat = env.get(stem + "_ACCESS_TOKEN") or _ENV.get(stem + "_ACCESS_TOKEN") or ""
    uid = env.get(stem + "_USER_ID") or _ENV.get(stem + "_USER_ID") or ""
    pat, uid = pat.strip(), uid.strip()
    return (pat, uid) if pat and uid else None


def fetch_newapi(ctx: dict) -> dict | None:
    """One new-api gateway. /api/status identifies the host without auth;
    a non-new-api answer (or an unreachable host) → None so the provider
    drops out of the document entirely. Once identified, failures are real
    and surface as error records. No credential ever enters the document.

    Two data levels: the sk- billing endpoints are token-scoped on sites
    that enable token stats (subscription hard_limit_usd = 1e8 is the
    unlimited-TOKEN sentinel — the account's real quota is invisible
    there). With {ID}_ACCESS_TOKEN + {ID}_USER_ID the user-level record
    (what the web dashboard shows: balance + account consumption) comes
    from /api/user/self; quota figures are raw units, QuotaPerUnit 500000
    (compiled constant, source-verified). Any user-level failure falls
    back to the token-level spend."""
    p = ctx["provider"]
    fx = f"newapi-{p['id']}"
    try:
        status = fetch_fixture(fx, "status") if _FIXTURES_DIR else \
            fetch_json(p["baseUrl"] + "/api/status")
    except Exception:  # noqa: BLE001 — unreachable/404/HTML → not new-api
        return None
    data = status.get("data") if isinstance(status, dict) else None
    if not (isinstance(data, dict) and isinstance(data.get("system_name"), str)
            and data.get("version")):
        return None
    if not p.get("apiKey"):
        return record(p, error="no-key")

    prov = dict(p)
    prov["name"] = data["system_name"]
    words = data["system_name"].split()
    prov["display"] = ("".join(w[0] for w in words[:2]).upper() or p["display"])[:2]

    def today_part(current: float, scope: str) -> str:
        spent = _gateway_spent_today(ctx, p["id"], current, scope)
        return f" · ${spent:.2f} today" if spent is not None else ""

    # ---- user level (dashboard numbers) ---------------------------------- #
    cred = _newapi_user_credential(ctx, p)
    if cred is not None:
        try:
            hdr = {"Authorization": "Bearer " + cred[0],
                   "New-Api-User": cred[1],
                   "Referer": p["baseUrl"] + "/console"}
            me = fetch_fixture(fx, "user") if _FIXTURES_DIR else \
                fetch_json(p["baseUrl"] + "/api/user/self", hdr)
            ud = me.get("data") if isinstance(me, dict) else None
            remaining = finite(ud.get("quota"), -1.0) if isinstance(ud, dict) else -1.0
            used = finite(ud.get("used_quota"), -1.0) if isinstance(ud, dict) else -1.0
            if remaining >= 0 and used >= 0:
                remaining /= 500000.0  # QuotaPerUnit
                used /= 500000.0
                return record(prov, configured=True, kind="balance",
                              label=f"${remaining:.2f}", value=round(used, 2),
                              currency="$",
                              windows=[window("spend", "used", 0, used=used,
                                              total=remaining + used, unit="$")],
                              detail=(f"new-api · {data['system_name']}"
                                      f" · used ${used:.2f}"
                                      + today_part(used, "user")),
                              spendScope="user", newapi=True)
        except Exception:  # noqa: BLE001 — fall back to token level below
            pass

    # ---- token level (sk- billing endpoints) ------------------------------ #
    try:
        hdr = {"Authorization": "Bearer " + p["apiKey"]}
        sub = fetch_fixture(fx, "subscription") if _FIXTURES_DIR else \
            fetch_json(p["baseUrl"] + "/v1/dashboard/billing/subscription", hdr)
        usg = fetch_fixture(fx, "usage") if _FIXTURES_DIR else \
            fetch_json(p["baseUrl"] + "/v1/dashboard/billing/usage", hdr)
    except Exception:  # noqa: BLE001 — identified gateway, real failure
        return record(p, error="fetch-failed")
    lifetime = finite(usg.get("total_usage") if isinstance(usg, dict) else None,
                      0.0) / 100.0
    cap = finite(sub.get("hard_limit_usd") if isinstance(sub, dict) else None, 0.0)
    uncapped = not (0 < cap < 10_000_000)  # 1e8 = new-api "unlimited"
    parts = ["new-api · " + data["system_name"]]
    if uncapped:
        # 1e8 only happens on the token branch (source: billing.go), so the
        # figure is the token's spend, not the account's.
        parts.append("token only" + (" · user fetch failed" if cred else ""))
    if not uncapped:
        parts.append(f"${lifetime:.2f} of ${cap:.0f}")
    detail = " · ".join(parts) + today_part(lifetime, "token")
    windows = ([] if uncapped else
               [window("spend", "life", 0, used=lifetime, total=cap, unit="$")])
    return record(prov, configured=True, kind="balance",
                  label=f"${lifetime:.2f}", value=round(lifetime, 2),
                  currency="$", windows=windows, detail=detail,
                  spendScope="token", newapi=True)


QUOTA_ADAPTERS = {
    "opencode": fetch_opencode,
    "openrouter": fetch_openrouter,
    "kimi": fetch_kimi,
    "zai": fetch_zai,
    "deepseek": fetch_deepseek,
    "copilot": fetch_copilot,
    "qwen": fetch_qwen,
}

# keyEnv: the credential the panel's "Add key" UI stores for this provider
# ("" = no remote credential — qwen is a local-plan meter, opencode also
# auto-detects auth.json). Must match the first name in each adapter's
# get_key() chain.
PROVIDERS = [
    {"id": "opencode", "name": "OpenCode Go", "display": "OC",
     "keyEnv": "OPENCODE_GO_API_KEY"},
    {"id": "openrouter", "name": "OpenRouter", "display": "OR",
     "keyEnv": "OPENROUTER_API_KEY"},
    {"id": "kimi", "name": "Kimi / Moonshot", "display": "KI",
     "keyEnv": "KIMI_API_KEY"},
    {"id": "zai", "name": "ZAI / GLM", "display": "Z", "keyEnv": "ZAI_API_KEY"},
    {"id": "deepseek", "name": "DeepSeek", "display": "DS",
     "keyEnv": "DEEPSEEK_API_KEY"},
    {"id": "copilot", "name": "GitHub Copilot", "display": "CP",
     "keyEnv": "GITHUB_TOKEN"},
    {"id": "qwen", "name": "Qwen Coding Plan", "display": "AB",
     "keyEnv": "QWEN_PLAN_COOKIE"},
]


# --------------------------------------------------------------------------- #
# LOCAL CONSUMPTION scanners (offline; strictly read-only over agent stores)
# --------------------------------------------------------------------------- #

class DayIndex:
    """Local midnights of the last 7 days, oldest first. Replaces per-row
    strftime+dict-lookup with one bisect (the dominant scan cost)."""

    __slots__ = ("starts", "dates")

    def __init__(self, now_ms: int):
        midnight = dt.datetime.fromtimestamp(now_ms / 1000.0).replace(
            hour=0, minute=0, second=0, microsecond=0)
        self.dates: list[str] = []
        self.starts: list[int] = []
        day = midnight - dt.timedelta(days=6)
        for _ in range(7):
            self.starts.append(int(day.timestamp() * 1000))
            self.dates.append(day.strftime("%Y-%m-%d"))
            day += dt.timedelta(days=1)
        # Sentinel boundary so rows inside today still land in bucket 6.
        self.starts.append(int(day.timestamp() * 1000))


def _bucket() -> dict:
    return {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
            "cacheWriteTokens": 0, "reasoningTokens": 0, "requests": 0, "cost": 0.0}


def _row(ms: int, model: str, inp: int, out: int, cr: int, cw: int, rs: int,
         cost: float, models: dict, days: DayIndex, dtok: list, dreq: list) -> None:
    b = models.get(model)
    if b is None:
        b = models[model] = _bucket()
    b["inputTokens"] += inp
    b["outputTokens"] += out
    b["cacheReadTokens"] += cr
    b["cacheWriteTokens"] += cw
    b["reasoningTokens"] += rs
    b["requests"] += 1
    b["cost"] += cost
    i = bisect.bisect_right(days.starts, ms) - 1
    if 0 <= i < 7:
        dtok[i] += inp + out + cr + cw + rs
        dreq[i] += 1


def walk_jsonl(label: str, files: list[str], extract, models: dict,
               days: DayIndex, dtok: list, dreq: list, observe=None,
               sink: dict | None = None, lane_of=None) -> dict:
    """Shared skeleton: json.loads per line, extractor returns
    (ms, model, inp, out, cr, cw, rs, cost) or None to skip. observe(d, ms)
    is called for every parsed line before extraction (used by the qwen
    scan to feed the plan census without a second pass). sink, when given,
    accumulates tokens/requests — scan_omp uses it to attribute transcript
    lanes (main / advisor / subagent / roles) without a second read:
    lane_of(d) maps the RAW record to a bucket key ("" when no router),
    so interleaved record kinds in one file still land in their lane."""
    n_req = 0
    seen_models: set[str] = set()
    for path in files:
        try:
            if os.path.getsize(path) > 32 * 1024 * 1024:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if observe is not None:
                        observe(d, parse_ts_ms(d.get("timestamp")
                                               if isinstance(d, dict) else None))
                    try:
                        rec = extract(d)
                    except (ValueError, TypeError):
                        continue
                    if not rec or rec[0] is None:
                        continue
                    _row(*rec, models, days, dtok, dreq)
                    n_req += 1
                    if sink is not None:
                        key = lane_of(d) if lane_of is not None else ""
                        bucket = sink.get(key)
                        if bucket is None:
                            bucket = sink[key] = {"tokens": 0, "requests": 0}
                        bucket["tokens"] += (rec[2] + rec[3] + rec[4]
                                             + rec[5] + rec[6])
                        bucket["requests"] += 1
        except OSError:
            continue
    return {"source": label, "requests": n_req, "models": sorted(seen_models)}


def extract_claude(d):
    if not isinstance(d, dict) or d.get("type") != "assistant":
        return None
    m = d.get("message") or {}
    u = m.get("usage") or {}
    if not isinstance(u, dict) or not u:
        return None
    cc = u.get("cache_creation") or {}
    # cache_creation_input_tokens is the total; ephemeral_* is its breakdown.
    cw = finite(u.get("cache_creation_input_tokens"),
                finite(cc.get("ephemeral_5m_input_tokens"))
                + finite(cc.get("ephemeral_1h_input_tokens")))
    od = u.get("output_tokens_details") or {}
    inp, out = int(finite(u.get("input_tokens"))), int(finite(u.get("output_tokens")))
    cr = int(finite(u.get("cache_read_input_tokens")))
    # costUSD: legacy Claude Code format embedded it per assistant line;
    # current versions don't (only cumulative cost-state entries, which are
    # a different feature). Read-if-present, else 0.0 — never fabricate.
    return (parse_ts_ms(d.get("timestamp")), str(m.get("model") or "claude"),
            inp, out, cr, int(cw),
            int(finite(od.get("thinking_tokens"))), finite(d.get("costUSD")))


def extract_codex(d):
    pl = d.get("payload") if isinstance(d, dict) else None
    if not isinstance(pl, dict) or pl.get("type") != "event_msg":
        return None
    info = pl.get("info")
    if not isinstance(info, dict):
        return None
    u = info.get("token_usage") or info.get("last_token_usage")
    if not isinstance(u, dict):
        return None
    inp, out = int(finite(u.get("input_tokens"))), int(finite(u.get("output_tokens")))
    cr = int(finite(u.get("cached_input_tokens")))
    model = str(info.get("model") or "codex")
    # Codex transcripts carry no cost — 0.0 is the truth, not a gap.
    return (parse_ts_ms(d.get("timestamp")), model,
            inp, out, cr, 0,
            int(finite(u.get("reasoning_output_tokens"))), 0.0)


def extract_omp(d):
    if not isinstance(d, dict):
        return None
    if d.get("type") == "model_usage":
        # Internal role calls (auto-thinking probes on a tiny model): flat
        # record, usage at the top level, no nested message. Verified
        # non-duplicative — the probed calls never also appear as
        # assistant messages. Explicit completion() oneshots write no
        # record at all; that gap stays open.
        u = d.get("usage") or {}
        if not isinstance(u, dict) or not u:
            return None
        cost = (u.get("cost") or {}).get("total") if isinstance(u.get("cost"), dict) else None
        return (parse_ts_ms(d.get("timestamp")),
                str(d.get("model") or "omp"),
                int(finite(u.get("input"))), int(finite(u.get("output"))),
                int(finite(u.get("cacheRead"))), int(finite(u.get("cacheWrite"))),
                int(finite(u.get("reasoningTokens"))), finite(cost))
    if d.get("type") != "message":
        return None
    m = d.get("message") or {}
    if m.get("role") != "assistant":
        return None
    u = m.get("usage") or {}
    if not isinstance(u, dict) or not u:
        return None
    cost = (u.get("cost") or {}).get("total") if isinstance(u.get("cost"), dict) else None
    return (parse_ts_ms(m.get("timestamp") or d.get("timestamp")),
            str(m.get("model") or "omp"),
            int(finite(u.get("input"))), int(finite(u.get("output"))),
            int(finite(u.get("cacheRead"))), int(finite(u.get("cacheWrite"))),
            int(finite(u.get("reasoningTokens"))), finite(cost))


def extract_qwen(d):
    if not isinstance(d, dict):
        return None
    um = d.get("usageMetadata") or (d.get("message") or {}).get("usageMetadata")
    if not isinstance(um, dict):
        return None
    pd = um.get("promptTokensDetails") or {}
    cd = um.get("candidatesTokensDetails") or {}
    model = str((d.get("message") or {}).get("model") or d.get("model") or "qwen")
    inp = int(finite(um.get("promptTokenCount")))
    out = int(finite(um.get("candidatesTokenCount")))
    cr = int(finite(pd.get("cachedContentTokenCount")))
    # Qwen transcripts carry no cost — 0.0 is the truth, not a gap.
    return (parse_ts_ms(d.get("timestamp")), model,
            inp, out, cr,
            0, int(finite(cd.get("thinkingTokenCount"))), 0.0)


def scan_claude(ctx: dict, models: dict, days: DayIndex, dtok: list, dreq: list) -> dict:
    files = sorted(glob.glob(str(ctx["home"] / ".claude/projects/*/*.jsonl")))
    return walk_jsonl("claude", files, extract_claude, models, days, dtok, dreq)


def scan_codex(ctx: dict, models: dict, days: DayIndex, dtok: list, dreq: list) -> dict:
    files = sorted(glob.glob(str(ctx["home"] / ".codex/sessions/**/rollout-*.jsonl"),
                             recursive=True))
    return walk_jsonl("codex", files, extract_codex, models, days, dtok, dreq)


def scan_omp(ctx: dict, models: dict, days: DayIndex, dtok: list, dreq: list) -> dict:
    """OMP transcripts carry a lane structure: top-level session files are
    the main agents, __advisor.jsonl inside a session dir is the advisor,
    and any other nested file (named after the subagent) is a spawned
    subagent. Lanes are attributed per file; model_usage records (internal
    role calls) are routed to a shared "roles" bucket regardless of which
    file they sit in. Explicit completion() oneshots write no transcript
    anywhere on disk — a runtime gap this engine cannot close; the panel
    marks those unlogged."""
    root = ctx["home"] / ".omp/agent/sessions"
    files = sorted(glob.glob(str(root / "**/*.jsonl"), recursive=True))
    groups: dict[str, list[str]] = {"main": [], "advisor": [], "subagent": []}
    for f in files:
        name = os.path.basename(f)
        if name == "__advisor.jsonl":
            groups["advisor"].append(f)
        elif os.path.dirname(os.path.dirname(f)) == str(root):
            groups["main"].append(f)
        else:
            groups["subagent"].append(f)

    def lane_router(native: str):
        def lane_of(d: dict) -> str:
            return "roles" if d.get("type") == "model_usage" else native
        return lane_of

    src: dict = {"source": "omp", "requests": 0, "models": []}
    lanes: dict[str, dict] = {}
    for lane in ("main", "advisor", "subagent"):
        sink: dict[str, dict] = {}
        s = walk_jsonl("omp", groups[lane], extract_omp, models, days, dtok, dreq,
                       sink=sink, lane_of=lane_router(lane))
        src["requests"] += s["requests"]
        src["models"] = sorted(set(src["models"]) | set(s["models"]))
        for sub, b in sink.items():
            tgt = lanes.get(sub)
            if tgt is None:
                tgt = lanes[sub] = {"tokens": 0, "requests": 0}
            tgt["tokens"] += b["tokens"]
            tgt["requests"] += b["requests"]
    src["lanes"] = lanes
    return src


def scan_qwen(ctx: dict, models: dict, days: DayIndex, dtok: list, dreq: list) -> dict:
    """Walks the transcripts once. When the quota plane runs concurrently it
    publishes the 5h/week/month request census through ctx so fetch_qwen does
    not re-read the same files."""
    files = sorted(glob.glob(str(ctx["home"] / ".qwen/projects/*/chats/*.jsonl")))
    now = ctx["nowMs"]
    counters = [0, 0, 0]

    def observe(d, ms):
        if ms is None or ms > now:
            return
        age = now - ms
        counters[0] += 1 if age <= H5 else 0
        counters[1] += 1 if age <= WEEK else 0
        counters[2] += 1

    try:
        return walk_jsonl("qwen", files, extract_qwen, models, days, dtok,
                          dreq, observe=observe)
    finally:
        # Publish even on a failed walk (None → adapter re-reads as fallback).
        ctx["qwenCensus"] = tuple(counters) if files else None
        event = ctx.get("qwenCensusEvent")
        if event is not None:
            event.set()


def scan_opencode(ctx: dict, models: dict, days: DayIndex, dtok: list, dreq: list) -> dict | None:
    """OpenCode SQLite via SQL-side json_extract, so Python never holds raw
    message JSON (markbus pattern). Read-only connection."""
    candidates = [os.environ.get("OPENCODE_DB", ""),
                  str(ctx["home"] / ".local/share/opencode/opencode.db")]
    db_path = next((c for c in candidates if c and os.path.isfile(c)), None)
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA mmap_size=67108864")
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return None
    n = 0
    seen: set[str] = set()
    now = ctx["nowMs"]
    try:
        # Aggregation stays in SQL: GROUP BY (model, day-bucket) hands Python
        # a few hundred pre-summed rows instead of tens of thousands of JSON
        # parses. Day buckets are integer CASE branches on the engine's own
        # midnight boundaries — DST-correct by construction and ~2x cheaper
        # than per-row strftime. json_valid guards malformed rows; the LIKE
        # gate is a pure accelerator in front of it (reference-scanner trick).
        starts = days.starts
        # Bucket i holds [midnight_i, midnight_i+1); anything outside the
        # 7-day window (older than starts[0], or >= tomorrow) is -1.
        case = ("CASE WHEN time_created < " + str(int(starts[0])) + " THEN -1 "
                + " ".join(f"WHEN time_created < {int(starts[i])} THEN {i - 1}"
                           for i in range(1, 8)) + " ELSE -1 END")
        head = (
            "SELECT COALESCE(json_extract(data,'$.modelID'),"
            " json_extract(data,'$.model.modelID'),'opencode') AS m,"
            f" {case} AS d,"
            " SUM(CAST(COALESCE(json_extract(data,'$.tokens.input'),0) AS INTEGER)),"
            " SUM(CAST(COALESCE(json_extract(data,'$.tokens.output'),0) AS INTEGER)),"
            " SUM(CAST(COALESCE(json_extract(data,'$.tokens.reasoning'),0) AS INTEGER)),"
            " SUM(CAST(COALESCE(json_extract(data,'$.tokens.cache.read'),0) AS INTEGER)),"
            " SUM(CAST(COALESCE(json_extract(data,'$.tokens.cache.write'),0) AS INTEGER)),"
            " SUM(COALESCE(json_extract(data,'$.cost'),0)), COUNT(*)"
            " FROM message WHERE data LIKE '%\"role\":\"assistant\"%'")
        tail = ("   AND json_extract(data,'$.role')='assistant'"
                "   AND time_created > ? AND time_created <= ?"
                " GROUP BY m, d")
        params = (now - 31 * 24 * 3600_000, now)
        # Fast path skips the per-row json_valid gate: a malformed row aborts
        # this query (fetchall raises before any row is consumed) and the
        # guarded fallback below rescans WITH json_valid first (AND short-
        # circuits), skipping corrupt rows exactly as before. Well-formed
        # databases — the common case — skip a full parse pass (~25-35ms).
        try:
            rows = conn.execute(head + tail, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "malformed" not in str(exc).lower():
                raise
            rows = conn.execute(
                head + "   AND json_valid(data)" + tail, params).fetchall()
        for model, d, inp, out, rs, cr, cw, cost, count in rows:
            model = str(model)
            seen.add(model)
            b = models.get(model)
            if b is None:
                b = models[model] = _bucket()
            b["inputTokens"] += inp
            b["outputTokens"] += out
            b["cacheReadTokens"] += cr
            b["cacheWriteTokens"] += cw
            b["reasoningTokens"] += rs
            b["requests"] += count
            b["cost"] += cost
            if 0 <= d < 7:
                dtok[d] += inp + out + cr + cw + rs
                dreq[d] += count
            n += count
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {"source": "opencode", "requests": n, "models": sorted(seen)}



LOCAL_SCANNERS = (scan_opencode, scan_claude, scan_codex, scan_qwen, scan_omp)

def scan_local_slot(scanner, ctx: dict, days: DayIndex):
    """One scanner against private accumulators; safe to run in a process
    pool. Returns (source, models, dayTokens, dayReqs, qwenCensus) — the
    census survives pickling back from a fork child."""
    models: dict[str, dict] = {}
    dtok = [0] * 7
    dreq = [0] * 7
    try:
        src = scanner(ctx, models, days, dtok, dreq)
    except Exception:  # noqa: BLE001 — a dead store must not kill the plane
        return None, models, dtok, dreq, ctx.get("qwenCensus")
    return src, models, dtok, dreq, ctx.get("qwenCensus")


def merge_local(results, days: DayIndex, now: int) -> dict:
    """Fold parallel scanner slots back together in fixed scanner order so the
    document stays deterministic regardless of completion order."""
    models: dict[str, dict] = {}
    dtot = [0] * 7
    dreqt = [0] * 7
    sources = []
    for src, mm, dt, dr, _census in results:
        if not src or not src["requests"]:
            continue
        sources.append(src)
        for model, b in mm.items():
            tgt = models.get(model)
            if tgt is None:
                models[model] = b
            else:
                for k in ("inputTokens", "outputTokens", "cacheReadTokens",
                          "cacheWriteTokens", "reasoningTokens", "requests"):
                    tgt[k] += b[k]
                tgt["cost"] += b["cost"]
        for i in range(7):
            dtot[i] += dt[i]
            dreqt[i] += dr[i]
    total_tokens = 0
    for b in models.values():
        b["cost"] = round(b["cost"], 6)
        total_tokens += (b["inputTokens"] + b["outputTokens"] + b["cacheReadTokens"]
                         + b["cacheWriteTokens"] + b["reasoningTokens"])
    today_i = bisect.bisect_right(days.starts, now) - 1
    return {
        "models": models,
        "recentDays": [{"date": days.dates[i], "tokens": dtot[i], "requests": dreqt[i]}
                       for i in range(7)],
        "todayTokens": dtot[today_i] if 0 <= today_i < 7 else 0,
        "todayRequests": dreqt[today_i] if 0 <= today_i < 7 else 0,
        "weekTokens": sum(dtot),
        "totalTokens": total_tokens,
        "totalRequests": sum(b["requests"] for b in models.values()),
        "sources": sources,
    }

def update_history(ctx: dict, now: int, local: dict, providers: list) -> list:
    """Upsert today's snapshot into the persisted daily history and return
    the trailing 30 days for the document. Provider-cap percents and
    new-api gateway lifetime spend ride along in the file (spend feeds
    tomorrow's today-figure): history not recorded now can never be
    reconstructed later. Path derives from env_home() so test corpora stay
    isolated. Never raises — history is a bonus, not a plane."""
    try:
        path = (ctx["home"] / ".local/state/h3nr1.d14z.ai-usage/history.json")
        days: dict = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("days"), dict):
                days = data["days"]
        except (OSError, ValueError):
            days = {}
        today = local_date(now)
        caps: dict = {}
        for p in providers:
            if not p.get("configured"):
                continue
            wins = {}
            for w in p.get("windows") or []:
                if w.get("percent") is not None:
                    wins[str(w.get("id") or w.get("label"))] = w["percent"]
            if wins:
                caps[p["id"]] = wins
        spend: dict = {}
        for p in providers:
            if p.get("newapi") and p.get("value") is not None:
                spend[p["id"]] = {"usd": round(finite(p["value"], 0.0), 2),
                                  "scope": p.get("spendScope") or "token"}
        day = {"tokens": local.get("todayTokens", 0),
               "requests": local.get("todayRequests", 0),
               "caps": caps}
        if spend:
            day["spend"] = spend
        days[today] = day
        days = {d: days[d] for d in sorted(days)[-60:]}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps({"days": days}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        out = []
        for i in range(29, -1, -1):
            d = local_date(now - i * 86400_000)
            if d == today:
                out.append({"date": d, "tokens": local.get("todayTokens", 0),
                            "requests": local.get("todayRequests", 0)})
            elif d in days:
                out.append({"date": d, "tokens": days[d].get("tokens", 0),
                            "requests": days[d].get("requests", 0)})
            else:
                out.append({"date": d, "tokens": 0, "requests": 0})
        return out
    except Exception:  # noqa: BLE001 — a history failure never breaks refresh
        return []


# --------------------------------------------------------------------------- #
# Document assembly
# --------------------------------------------------------------------------- #

def build_document(settings: dict) -> dict:
    global _ENV, _FIXTURES_DIR
    _ENV = env_keys()
    if fixtures_dir():
        _FIXTURES_DIR = fixtures_dir()
    now = now_ms()
    ctx = {"nowMs": now, "caps": settings, "home": env_home()}

    quota_enabled = settings.get("quotaMode") != "off"
    local_enabled = settings.get("showLocalConsumption", True)
    if quota_enabled and local_enabled:
        ctx["qwenCensusEvent"] = threading.Event()
    days = DayIndex(now)
    started = time.monotonic_ns()

    def run_adapter(provider):
        try:
            job = dict(ctx)
            job["provider"] = provider
            if provider.get("baseUrl"):
                return fetch_newapi(job)
            adapter = QUOTA_ADAPTERS.get(provider["id"])
            return adapter(job) if adapter else record(provider, error="no-adapter")
        except Exception as exc:  # noqa: BLE001 — one failure never kills the doc
            return record(provider, error=f"internal:{type(exc).__name__}")

    providers: list[dict] = []
    local: dict = {}
    # One wave, bare threads (concurrent.futures was a 15ms import for the
    # same effect). SQLite's conn.execute releases the GIL in wide stretches,
    # so the GIL-bound transcript walks hide inside it instead of serialising
    # behind it (a fork pool measured +12% — pure overhead, run #6).
    slots: list[list] = []          # [out, name] per thread, fixed order
    threads: list[threading.Thread] = []

    def spawn(fn, args, out_box):
        box = [out_box, None]
        def target():
            try:
                box[1] = fn(*args)
            except Exception as exc:  # noqa: BLE001 — surfaced via box[0]
                box[1] = exc
        t = threading.Thread(target=target)
        slots.append(box)
        threads.append(t)
        t.start()

    gateways: list[dict] = newapi_gateways(ctx) if quota_enabled else []
    if quota_enabled:
        for provider in PROVIDERS:
            spawn(run_adapter, (provider,), None)
        for gateway in gateways:
            spawn(run_adapter, (gateway,), None)
    if local_enabled:
        for scanner in LOCAL_SCANNERS:
            spawn(scan_local_slot, (scanner, ctx, days), scanner.__name__)
    for t in threads:
        t.join()
    results = iter([box[1] for box in slots])
    if quota_enabled:
        providers = [next(results) for _ in PROVIDERS]
        # Non-new-api hosts returned None and drop out; the rest append in
        # the deterministic (name-sorted) spawn order.
        providers += [r for r in (next(results) for _ in gateways)
                      if r is not None]
    if local_enabled:
        local = merge_local([next(results) for _ in LOCAL_SCANNERS], days, now)
        local["history"] = update_history(ctx, now, local, providers)
    plane_ns = time.monotonic_ns() - started

    return {
        "schema": SCHEMA_VERSION,
        "nowMs": now,
        "generatedAtMs": now,
        "providers": providers,
        "local": local,
        "timings": {"totalMs": round(plane_ns / 1e6, 1)},
        "errors": [p["error"] for p in providers if p.get("error")],
    }


def add_key(name: str, value: str) -> dict:
    """Merge one KEY=value into ~/.config/ai-usage/env (0600): removes the
    old assignment line and appends the new one at the end, preserving
    comments and other keys. The value arrives via stdin — argv would be
    visible in `ps`. Never echoes the value back in the result."""
    import re
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,64}", name):
        return {"ok": False, "error": "invalid key name"}
    value = value.strip()
    if not value or len(value) > 4096 or "\n" in value or "\r" in value:
        return {"ok": False, "error": "invalid key value"}
    path = env_home() / ".config/ai-usage/env"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if path.is_file():
            # Line-based merge: drop only the replaced assignment; comments,
            # blanks and ordering are the user's and stay verbatim.
            lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                     if l.split("=", 1)[0].strip() != name]
        lines.append(f"{name}={value}")
        tmp_path = path.parent / (path.name + ".tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)  # atomic: bad input can never truncate
    except OSError as exc:
        return {"ok": False, "error": safe_error(exc)}
    return {"ok": True, "file": str(path)}


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="AI Usage Global engine")
    parser.add_argument("--settings", default="", help="JSON settings file or inline JSON")
    parser.add_argument("--add-key", default="", metavar="NAME",
                        help="read one credential from stdin and store it")
    args = parser.parse_args()
    if args.add_key:
        result = add_key(args.add_key, sys.stdin.readline())
        json.dump(result, sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
        raise SystemExit(0 if result.get("ok") else 1)
    settings: dict = {}
    raw = args.settings
    if raw:
        try:
            text = Path(raw).read_text(encoding="utf-8") if os.path.isfile(raw) else raw
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                settings = loaded
        except (OSError, ValueError):
            pass
    doc = build_document(settings)
    json.dump(doc, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
