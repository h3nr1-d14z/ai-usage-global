# AI Usage Global

Multi-provider AI subscription usage + local token consumption in the Omarchy
Quattro bar. Same concept as the OpenCode-Go-only widgets, but global: one bar
chip, one panel, every provider you actually pay for.

- **OpenCode Go**, **OpenRouter**, **Kimi / Moonshot**, **ZAI / GLM**,
  **DeepSeek**, **GitHub Copilot**, and **Alibaba Cloud Coding Plan (Qwen)** —
  meter bars, credit balances, live reset countdowns.
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
tests/validate.py     37 golden invariants (correctness gate)
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

The engine reads credentials from, lowest to highest precedence:

1. `~/.config/ai-usage/env` — a plain `KEY=value` dotenv (bar plugins inherit
   the shell's env, which on Wayland often lacks your exported keys)
2. `AIUSAGE_ENV_FILE` — alternative dotenv path
3. the real process environment

So: `mkdir -p ~/.config/ai-usage && printf 'OPENROUTER_API_KEY=sk-or-…\n' >> ~/.config/ai-usage/env`.

### Qwen / Alibaba Coding Plan — honest note

There is **no public usage API** for Coding Plan (`sk-sp-*`) keys: every
`dashscope…/api/v1/usage|quota` path 404s and the console gateway needs a
browser session (independently confirmed in [`steipete/CodexBar#612`]). So this
plugin computes the plan's three windows **locally**, by counting requests in
`~/.qwen/projects/*/chats/*.jsonl` against the plan caps — the same unit the
official console dashboard shows. Caps default to Pro (6 000 req / 5 h,
45 000 / week, 90 000 / month) and are editable in the widget settings.

If you want a real live meter for it, the fix has to come from Alibaba.

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
- `R` refresh · `1`/`2` switch tabs · `Tab` walks providers · `Esc` closes.
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
