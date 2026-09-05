# AI Usage Global

Multi-provider AI subscription usage + local token consumption in the Omarchy
Quattro bar. Same concept as the OpenCode-Go-only widgets, but global: one bar
chip, one panel, every provider you actually pay for.

- **OpenCode Go**, **OpenRouter**, **Kimi / Moonshot**, **ZAI / GLM**,
  **DeepSeek**, **GitHub Copilot**, and **Alibaba Cloud Coding Plan (Qwen)** —
  meter bars, credit balances, live reset countdowns.
- **New-API gateways** ([QuantumNous/new-api](https://github.com/QuantumNous/new-api), e.g. Agent Router) —
  discovered automatically from OMP's provider registry; lifetime + daily
  spend in USD straight from the gateway's billing API.
- **Local consumption** scanned from the agent stores already on your disk:
  OpenCode SQLite, Claude Code / Codex / Qwen Code / OMP transcripts. Per-model
  tokens, 7-day history, today/week/all totals. Read-only; nothing leaves the
  machine.

```
OC 5h 43% · W 78% · M 15%        (Data-mode bar chip)
```

## Layout

```
manifest.json      Quattro plugin contract (bar-widget, entry = Panel.qml)
Panel.qml          bar chip + popup: subscriptions tab, consumption tab
engine/usage.py    stdlib-only fetch+scan engine → one JSON document
tools/make_corpus.py  deterministic benchmark corpus generator
bench/run_bench.py    timing harness (emits METRIC lines)
tests/validate.py     golden invariants — the correctness gate
autoresearch.sh       canonical benchmark entrypoint
```

## Install

```sh
omarchy plugin add https://github.com/h3nr1-d14z/ai-usage-global.git --enable
```

Providers appear as soon as their credential exists — no restart needed.

## Credentials

| Provider | Env var | Otherwise read from |
|---|---|---|
| OpenCode Go | `OPENCODE_GO_API_KEY` | `~/.local/share/opencode/auth.json` |
| OpenRouter | `OPENROUTER_API_KEY` | — (balance needs a *management* key; a regular key returns 403 and only the per-key limit window shows) |
| Kimi | `KIMI_API_KEY` / `MOONSHOT_API_KEY` | `~/.kimi-code/config.toml`; set `AIUSAGE_KIMI_INTL=1` for `api.moonshot.ai` ($) — CN/intl keys are not interchangeable |
| ZAI / GLM | `ZAI_API_KEY` / `ZHIPUAI_API_KEY` | — |
| DeepSeek | `DEEPSEEK_API_KEY` | `~/.deepseek/config.toml` |
| Copilot | `GITHUB_TOKEN` / `GH_TOKEN` | `~/.config/gh/hosts.yml` |
| Qwen Coding Plan | (none — see below) | local transcripts |
| New-API gateway | (auto — see below) | `~/.omp/agent/models.yml` + `~/.omp/agent/.env` |

The engine reads credentials from, lowest to highest precedence:

1. `~/.config/ai-usage/env` — a plain `KEY=value` dotenv (bar plugins inherit
   the shell's env, which on Wayland often lacks your exported keys)
2. `AIUSAGE_ENV_FILE` — alternative dotenv path
3. the real process environment

Easiest: open the panel and paste the key into the provider's own row (the
Save button stores it into `~/.config/ai-usage/env` with 0600 perms — the
value travels over stdin, never argv). CLI equivalent:
`mkdir -p ~/.config/ai-usage && printf 'OPENROUTER_API_KEY=sk-or-…\n' >> ~/.config/ai-usage/env`.
Note the engine's own store (`~/.config/ai-usage/env`) is the **lowest**
precedence — a real environment variable always wins over a paste made in
the panel.

### Qwen / Alibaba Coding Plan

The plan has **no public usage API** for `sk-sp-*` keys (every
`dashscope…/api/v1/usage|quota` path 404s — confirmed in
[`steipete/CodexBar#612`]), but the console gateway works with a browser
session cookie, and that is what the widget uses when available:

1. **Console mode (preferred)** — real 5h/7d credit percentages + reset
   times via the same `api.json` gateway the QwenCloud/Bailian web dashboard
   calls (protocol ported from [OMP](https://github.com/can1350/omp)'s
   `alibaba-token-plan` usage provider). The cookie is picked up **automatically**
   if you are logged into the plan with OMP (`~/.omp/agent/agent.db` →
   `auth_credentials`); otherwise paste it into the Qwen row's key field
   (stored as `QWEN_PLAN_COOKIE`). Get it from the dashboard's DevTools →
   any request → `cookie:` header. Session cookies expire — when the gateway
   stops answering, the widget silently falls back to (2).
2. **Local census (fallback)** — counts requests in
   `~/.qwen/projects/*/chats/*.jsonl` against the plan caps (default Pro:
   6 000 req / 5 h, 45 000 / week, 90 000 / month, editable in settings).

The cookie is a session credential: it is used for the two gateway requests
and never written to the emitted document or logs (asserted by tests).

### New-API gateways (Agent Router & friends)

Any OMP provider whose host speaks the [QuantumNous/new-api](https://github.com/QuantumNous/new-api) console API is
picked up automatically: `~/.omp/agent/models.yml` provides the `baseUrl`
and the key (an env-name entry resolves against `~/.omp/agent/.env`, the
file OMP itself loads — a literal `apiKey:` works too). The widget calls the
gateway's OpenAI-compatible billing endpoints and shows the tracked
**lifetime spend** (`total_usage`, US cents; token-scoped on Agent Router —
the annotation says "token only" there) plus a per-day figure
derived from the persisted history snapshots — the first day seeds
silently, the "$ today" line appears from day two. Quota-capped gateways
additionally get a spend meter (against `hard_limit_usd` = remaining +
used = total); new-api's "unlimited" sentinel (`1e8`) renders as uncapped.
Some forks ignore the billing date params (Agent Router does), which is why
the engine treats the figure as lifetime-cumulative. Hosts that fail the
`/api/status` signature check (or aren't new-api at all) drop out silently.

**Account view (optional):** on token-stat sites the sk- key only sees the
one token's spend. With `<ID>_ACCESS_TOKEN` and `<ID>_USER_ID` (e.g.
`AGENTROUTER_ACCESS_TOKEN` / `AGENTROUTER_USER_ID`, from the dashboard's
Personal Settings → access token + numeric user ID) the block upgrades to
the dashboard's account numbers: balance as the headline, used in the
detail, and a used-of-total meter. Store them the same way as any key:
`printf '…\n' | python3 engine/usage.py --add-key AGENTROUTER_ACCESS_TOKEN`.

## Settings (`omarchy bar set h3nr1.d14z.ai-usage <key> <value>`)

| Key | Default | Effect |
|---|---|---|
| `refreshIntervalSec` | 900 | engine refresh period (60–3600) |
| `barDisplay` | `Data` | `Data` compact chip or `Icon` glyph |
| `defaultProvider` | `opencode` | which provider the bar chip shows |
| `showLocalConsumption` | `On` | scan local agent stores |
| `qwenPlanCap5h` / `qwenPlanCapWeek` / `qwenPlanCapMonth` | 6000 / 45000 / 90000 | plan caps |

## Interactions

- Left click: panel. Right/middle click: refresh.
- `R` refresh · `1`/`2`/`3` switch tabs · `Tab` walks providers · `Esc` closes.
- Meter tone: foreground → accent past 70% → urgent red past 90%.

## Development

```sh
./dev.sh                       # symlink into ~/.config/omarchy/plugins + tail logs
bash autoresearch.sh           # correctness gate + benchmark (emits METRIC engine_ms=…)
python3 tools/make_corpus.py    # regenerate the benchmark corpus
python3 tests/validate.py       # golden invariants only
```

The engine honours `AIUSAGE_NOW`, `AIUSAGE_HOME`, `AIUSAGE_FIXTURES`,
`AIUSAGE_ENV_FILE` so the whole plugin can be exercised offline and
deterministically.

## Dependencies

`python3` (≥3.10, stdlib only). No `curl`, `jq`, or third-party packages.

## Privacy

Only your own authenticated vendor API calls, plus reads of your own local
agent stores. No telemetry, no aggregation service, no credentials in the
emitted document (asserted by the validator).

## License

MIT.
