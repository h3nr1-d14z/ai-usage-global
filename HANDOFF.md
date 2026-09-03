# Handoff — AI Usage Global plugin

Status snapshot for continuing this work on the Omarchy machine.
Repo: `h3nr1.d14z/omarchy-ai-subcription-usage-tracker` → plugin id
`h3nr1.d14z.ai-usage`. Everything below is committed and pushed; the
working tree is clean.

## What we built

An Omarchy Quattro **bar-widget plugin** (one `Panel.qml` entry point,
`on-demand` activation) that shows subscription/quota usage for AI coding
plans, plus a local-cost plane. Two layers, deliberately separated:

1. **`engine/usage.py`** — one stdlib-only Python 3 script. Every provider
   is fetched **in parallel threads**; the whole document is one JSON blob
   on stdout. No dependencies, no network in tests (fixture transport),
   frozen clock via `AIUSAGE_NOW`. The QML never talks to any API itself.
2. **`Panel.qml`** — renders the document: meter bars per quota window
   (5h / week / month), reset countdowns, a local-consumption plane
   (tokens + estimated cost from OpenCode's SQLite), key-paste rows for
   unconfigured providers, per-provider `detail` strings.

### Providers

| Provider | Quota source | Credential |
|---|---|---|
| OpenCode Go | `openai/zen/go/v1/usage` (official) | auth.json `opencode-go` key |
| Claude / Codex / Copilot / Hermes / OMP | local stores (auth.json, OAuth, SQLite) | auto-detected |
| OpenRouter / Kimi / ZAI / DeepSeek / Alibaba | vendor quota APIs | `*_API_KEY` env |
| Qwen Coding Plan | **console gateway** (real %) → **transcript census** fallback | cookie (see below) |
| Cursor | local state.vscdb | auto-detected |

### Qwen — the long saga, resolved

- No public usage API exists for `sk-sp-*` Coding Plan keys (dashscope
  quota paths 404; `steipete/CodexBar#612`). Verified twice against
  QwenLM/qwen-code source: its `/usage` is a **local transcript replay**
  (no remote quota endpoint anywhere; OAuth `oauth_creds.json` authorizes
  inference only, not quota — same conclusion OMP reached by shipping the
  cookie flow).
- **Shipped**: the console gateway flow ported from OMP
  (`pi-ai/src/usage/alibaba-token-plan.ts`) — session `secToken` +
  form-POST `api.json` gateway → real 5h/7d percentages + reset times.
  Cookie resolution: `QWEN_PLAN_COOKIE` env → **auto-detect from
  `~/.omp/agent/agent.db`** (`auth_credentials`, provider
  `alibaba-token-plan`, most-recent row wins, `baseUrl` marks cn-beijing
  → China variant with `DataV2` envelope + HTML `SEC_TOKEN` session).
  Any failure → silent census fallback (`~/.qwen/projects/*/chats/*.jsonl`
  request counts vs plan caps 6k/5h, 45k/wk, 90k/mo — editable in
  settings). Console gives 5h+7d only; census gives all three. The cookie
  never enters the emitted document (test-asserted).
- Gateway reset-time types are sloppy (epoch s, ms, numeric strings, ISO)
  — `_reset_ms()` tolerates all four; pinned by tests.

### Cost plane

Estimated cost = per-request token deltas × a **pricing oracle** derived
from models.dev at collector build time (not mirrored in-repo; see
`tools/`), with Go-plan fixed-window logic. OpenCode SQLite is the only
store scanned; other agents contribute quota windows only.

## Quality infrastructure (all wired into the gate)

```sh
bash autoresearch.sh        # corpus → 4 test suites → 9 timed runs
```

- `tests/validate.py` — 41 golden invariants over a deterministic fake
  `$HOME` (30k-row OpenCode SQLite, JSONL transcripts, frozen fixtures).
- `tests/test_sql_fallback.py` — SQLite-locked / corrupt-DB degradation.
- `tests/test_add_key.py` — the panel Save-button contract (stdin-only
  values, 0600, line-based merge preserving comments, atomic replace,
  rejection leaves file byte-identical, no echo).
- `tests/test_qwen_console.py` — console branch fully offline: env-cookie
  flow, agent.db auto-detect, newest-row-wins both directions, China
  DataV2, numeric-string + ISO resets, secrets-absent, failure→census.
- `bench/run_bench.py` — median engine_ms + workload invariants
  (tokens/requests scanned must not move). Baseline on this M1: ~142 ms.
- `bench/last-validation.log` keeps one `== suite ==` transcript each.
- Corpus/console isolation: validate + bench pop ambient
  `QWEN_PLAN_COOKIE` so corpus runs stay on the census path.

## Setup on the Omarchy machine

```sh
git clone https://github.com/h3nr1.d14z/omarchy-ai-subcription-usage-tracker \
  ~/.config/omarchy/plugins/h3nr1.d14z.ai-usage
omarchy plugin enable h3nr1.d14z.ai-usage   # if not auto-enabled
omarchy-shell shell rescanPlugins           # if not listed
```

No symlinks allowed in the plugin folder; keep `manifest.json`
`entryPoints.barWidget = Panel.qml` as-is.

## Verification checklist (do these in order)

1. `bash autoresearch.sh` — expect 4× PASS + `METRIC engine_ms=…`.
   Linux numbers will differ; record the new baseline.
2. Bar widget appears; left-click opens the panel; right-click refreshes.
3. **Qwen row**: `detail` starts with `console ·` and shows real 5h/7d %
   → OMP store on that machine has the cookie, done. If it starts with
   `local ·` → machine lacks a cookie: paste one from the QwenCloud
   dashboard (DevTools → any request → `cookie:` header) into the row
   (stored as `QWEN_PLAN_COOKIE`), or accept the census fallback.
4. `qs log -p "$OMARCHY_PATH/shell" --tail 100` — no QML errors from
   `h3nr1.d14z.ai-usage`.
5. Optional: `qmllint -I "$OMARCHY_PATH/shell" Panel.qml` and
   `omarchy plugin validate ~/.config/omarchy/plugins/h3nr1.d14z.ai-usage`.

## Follow-ups / known limitations

- **Console = 5h + 7d only** (the API has no monthly). Census monthly
  is intentionally *not* spliced into a console record. If Alibaba ships
  a monthly field, extend `fetch_qwen_console`.
- **Session cookies expire** — the gateway 401s silently fall back to
  census; a "cookie stale" badge in the panel would be nice-to-have.
- **Census refinement idea (parked, do NOT do casually)**: newer Qwen
  CLIs write per-session aggregates to `~/.qwen/usage_record.jsonl`.
  Mixing them with the chats/*.jsonl line count risks double-counting,
  they're missing on crashes/older installs, and the census is tested
  gold as a fallback. Only worth it if the census proves unreliable in
  practice.
- `DASHSCOPE_API_KEY` (meviusisback's approach) is pay-as-you-go balance,
  not the Coding Plan — deliberately not used for qwen.
- Marketplace publishing (`plugins.omarchy.org/publish.html`) once the
  Omarchy-machine checklist is green; version is 0.2.0.
- Multi-account: engine keys everything by provider id; a second Go/plan
  account would need an ids-per-provider pass (not started, not asked).

## Commit trail

`fea0fb4` initial plugin → `37ef141`/`95b091b` review fixes + cost oracle
→ `0c20a33` nested-JSON hardening → `b9f9b5a` panel crash fix + in-panel
keys → `cfbcd23` Qwen console meter + test suites → `6935902` reset-time
dedup + ISO coverage → this commit (numeric-string resets, docstrings,
gate log, handoff).
