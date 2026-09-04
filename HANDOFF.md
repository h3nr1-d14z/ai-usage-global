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
  (tokens/requests scanned must not move). Baselines: M1 ~142 ms;
  Ryzen 7 5800H Linux ~190 ms median, 182–198 spread, planes ~128 ms.
  Note: `output_bytes` carries ±1 byte of inherent noise — the engine
  embeds its own `timings.totalMs` (0.1 ms resolution) in the document,
  so the digit before the decimal can shift run to run. Proven
  byte-identical otherwise (cfbcd23 vs HEAD corpus docs diff to nothing
  once `timings` is stripped). Treat tokens/requests as the real
  invariants; output_bytes only flags gross document changes.
- `bench/last-validation.log` keeps one `== suite ==` transcript each.
- Corpus/console isolation: validate + bench pop ambient
  `QWEN_PLAN_COOKIE` so corpus runs stay on the census path.

## Setup on the Omarchy machine

```sh
git clone https://github.com/h3nr1-d14z/ai-usage-global.git \
  ~/.config/omarchy/plugins/h3nr1.d14z.ai-usage
omarchy plugin enable h3nr1.d14z.ai-usage   # if not auto-enabled
omarchy restart shell                   # apply plugin QML (hot-reload is unreliable)
```

No symlinks allowed in the plugin folder; keep `manifest.json`
`entryPoints.barWidget = Panel.qml` as-is.

## Linux verification — 2026-09-04 (Omarchy machine)

Checklist executed end-to-end; everything below is live-verified.

- Gate: 4× PASS, `engine_ms` ~190 median (see baselines above). Workload
  invariants byte-identical: 31,340 requests / 1.01B tokens / 6,483 B.
  `qmllint` not installed there (optional item, skipped); `omarchy plugin
  validate` exits 0.
- Bar chip renders `AB W 42%`; `qs log` clean (the wrangler WARNs belong
  to another plugin).
- **Qwen row bug found & fixed** (`df9ba17`): the live international
  gateway nests usage one level deeper than any pinned envelope —
  `data.DataV2.data.data.{per…}` (dv2.data is a msg/code envelope). The
  ported parse stopped at `DataV2.data`, so every field read None →
  silent census fallback → `no-local-store` without `~/.qwen` transcripts.
  Fix mirrors OMP's `X8r`: the final `.data` descent is unconditional
  (`elif` → `if`). This account also returns ONLY the week window, as a
  fraction (0.417 → 41.7%) — both shapes pinned (suite now 17 checks).
  Live record: `console · W 41.7%`, reset 2026-09-07T05:35Z, via the
  agent.db cookie auto-detect (row shape `{"token","cookie"}`, no
  baseUrl → international).
- **Key-input UX** (user direction: keep keys manual, improve the input
  tab): clipboard paste (`wl-paste`, strips a leading `cookie:`),
  show/hide mask toggle, per-provider hint lines, paste rows that stay
  reachable once a provider is configured (`shownProviders` used to hide
  every unconfigured provider the moment one lit up), and a census-mode
  qwen re-offers its row with a "showing local counts" hint. Panel height
  fits content now (the old 640px cap clipped the 7-block list).
- **Deploy caveat**: plugin hot-reload logs "Local plugin changed,
  reloading" but does NOT re-instantiate the QML component — engine
  changes apply immediately (fresh subprocess per refresh), QML changes
  do not; `omarchy-shell shell rescanPlugins` is a no-op for changed
  files. Apply QML changes with `omarchy restart shell`.
- IPC: `qs -p "$OMARCHY_PATH/shell" ipc call h3nr1.d14z.ai-usage
  refresh|open|close` (all exercised during verification).

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
- **Session cookies expire** — handled: a configured qwen that falls back
  to census re-offers its paste row with a "showing local counts" hint.
- **First refresh after shell start**: the engine runs only on the
  refresh-interval timer (900s default) or panel-open, so the bar reads
  `AI —` for up to 15 min after login/restart. Left alone on purpose (no
  unrequested automatic behaviour); `Component.onCompleted: refresh()`
  would close it if ever wanted.
- **OMP agent.db holds more credentials than the engine uses** (e.g.
  `opencode-go` keys on this machine). Auto-detecting them was proposed
  and declined: keys are pasted manually by design; the Qwen cookie
  auto-detect stays the single exception.
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
dedup + ISO coverage → numeric-string resets, docstrings, gate log,
handoff → `28608d0` placeholderText fix → `df9ba17` live-intl
`DataV2.data.data` unwrap → `18cc1df`/`2650ac7`/`b9eb6dd` key-input UX +
panel fit → `11baf86` credential hints wrap (no eliding) → `27026ab`/`451ab27`
Linux verification record + trail → `6fbeef1`/`10c3352` panel de-clutter
(one number per window — no % echoes in header/detail; header status
slot = soonest reset for multi-window providers; footer help always
visible; block spacing 14) → `9883e58`/`8f2ddbe` user: "the ui is dumb as
shit" — unconfigured paste rows collapse behind a "+ add provider"
toggle (click or A key; auto-expanded only while nothing configured;
census-qwen re-offer still renders inside its configured block; the
expanded filter is the pre-`6fbeef1` shownProviders shape) → this
commit (chip: short text `OC W 51%` + tone color — foreground <70 /
accent ≥70 / urgent ≥90 via WidgetButton active/activeColor; full
per-window detail moved to hover tooltip; dead `alarming` property
removed; user direction: "just use color and a short text") → `baef0cd`
trail → this commit (tooltip: user report — hover showed only the
default provider; chipDetail now lists all configuredProviders, one
per line, default first). Machine state: opencode key added by the
user 2026-09-04 (defaultProvider defaults to opencode; qwen showed
while opencode was unconfigured).
