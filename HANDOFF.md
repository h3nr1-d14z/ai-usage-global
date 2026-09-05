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
| New-API gateways (Agent Router) | **billing endpoints** (lifetime $, US-cents `total_usage`) | models.yml env-name → `~/.omp/agent/.env` |
| TrollLLM | **dashboard API** (dual ledger: plan daily credits + PAYG wallet) | session cookie (see below) |

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


### TrollLLM — dual ledger via dashboard cookie

- `chat.trollllm.xyz` is the API gateway (OpenAI + native Anthropic faces,
  browser UA required); **no quota endpoints for sk- keys** — dashboard
  routes at `trollllm.xyz` are session-cookie-only and `POST /api/login`
  requires a Cloudflare Turnstile token (self-login infeasible). Hence
  `TROLLLLM_COOKIE` (panel paste row, qwen-console pattern): two GETs
  (`/api/user/me`, `/api/user/credit-resources`), cookie never enters the
  document (test-asserted). Expired → `fetch-failed` (re-paste); missing →
  benign `no-cookie` + paste row.
- **Units**: 1 credit = $0.01 USD (site's usage doc; confirmed by tier
  economics — lite 339k₫ ≈ $13.6 for 1500 cr/mo ≈ $15 of usage, a normal
  reseller margin; at $1/cr it would be $1500 for $13.6, impossible. The
  /pricing "1.000đ = $1 credit" line is impossible under BOTH readings —
  marketing sloppiness, same as the stale "$20/day" tier strings. An OMP
  cost cross-check was inconclusive: the 5.25 cr burned outside OMP's
  records). Figures stay in credits — the unit the dashboard renders.
  **Ledgers**: plan `planDailyUsed`/`planDailyAllocation` (tiers lite 50 /
  standard 100 / plus 160 / premium 250 / … / elite 1800 cr per day;
  resets daily — `planDailyResetDate` lags into the past (it is the
  current window's start), engine rolls +24h to the next occurrence) and
  PAYG wallet `totalUsed`/(`totalRemaining`+`totalUsed`) — the depletion
  gauge the dashboard shows (purchased credits never expire; promo
  resources do, per-batch `expiresAt`). `creditPriority: plan_first`
  means plan credits burn first.
- Rendering: `kind:"balance"`, `label` = plan credits remaining
  (`"44.8 cr"`), `value` = plan used % (so the 70/90 bar-chip tone means
  plan pressure), windows `plan` (daily meter + reset countdown) and
  `payg` (wallet meter — only while a batch is live: `totalRemaining > 0`
  or `activeCount > 0`; an all-expired wallet hides the meter so it can't
  render a false red 100% or trip cap alerts, detail keeps `wallet X cr`),
  detail `"lite · daily 5.25/50 cr · wallet 12.50 cr"`. Pure-PAYG
  accounts (no plan) headline the wallet instead. Static registry entry
  (never a new-api gateway); OMP's `trollllm-anthropic` twin still drops
  as a non-new-api host.

### New-API gateways — how it works

- Discovery is disk-only: `newapi_gateways()` parses `~/.omp/agent/models.yml`
  `providers:` (2-space/4-space subset parser), resolves `apiKey` env-names
  against `~/.omp/agent/.env` (the file OMP loads — the panel process does
  NOT inherit OMP's env) then the panel's own env store, dedupes by host,
  and skips static-registry ids. `fetch_newapi()` then confirms new-api-ness
  via `GET /api/status` (needs `data.system_name` + `data.version`); any
  miss → `None` → the provider drops out of the document entirely.
- Billing: `GET /v1/dashboard/billing/subscription` (`hard_limit_usd` =
  remaining + used = TOTAL per controller/billing.go; `1e8` = new-api
  "unlimited" sentinel → uncapped) and `.../usage`
  (`total_usage` = lifetime US cents; **token-scoped on agentrouter** —
  the 1e8 subscription is the unlimited-TOKEN sentinel, proven by the
  dashboard: $40.60 token vs $47.82 account). **Date params are
  ignored by agentrouter's fork** (disjoint windows return identical sums,
  verified 2026-09-05) → the figure is lifetime-cumulative; today's spend
  = lifetime − yesterday's history snapshot (`_gateway_spent_today`,
  rotation/missing baseline → unknown, never negative). First day seeds
  silently.
- Rendering (token level): `kind:"balance"`, `label="$37.93"`,
  `value=lifetime` (drives tone at $70/$90), detail `"new-api · <name> ·
  token only · $X today"`. Key NEVER enters the document (test-asserted,
  incl. literal-key and env-name paths). No Panel.qml changes were needed.
- User level (dashboard numbers): `AGENTROUTER_ACCESS_TOKEN` +
  `AGENTROUTER_USER_ID` (panel env store, both stored 2026-09-05 — PAT
  pasted by the user, uid 44499 from the username slug, UNCONFIRMED) →
  `GET /api/user/self` with the fork-required `New-Api-User` header →
  quota/used_quota ÷ 500000 (QuotaPerUnit) → balance label + used detail
  + used-of-total meter, `spendScope:"user"`. Any failure (incl. the
  Aliyun WAF that challenges /api/user/* — flag earned by probing, cools
  down) falls back to token level annotated `· user fetch failed` when
  the upgrade was armed. History snapshots are `{usd, scope}`-tagged;
  today-diffs only within a scope.
- Corpus v9-v12: models.yml + `.omp/agent/.env` fixtures (agentrouter =
  gateway, trollllm-anthropic = non-new-api drop, qwen + trollllm =
  static-id skips), billing + user + trollllm dashboard fixtures, manifest
  `expected.newapi` / `expected.trollllm` goldens, engine-state wipe in
  make_corpus (stale history would drift day-relative goldens).

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
commit (tooltip: user report — hover showed only the default provider;
chipDetail now lists all configuredProviders, one per line, default
first) → `735d7c8` trail → second "what else" round: cut the dead
Tab-walk (selectedProvider/selected had no consumer since the footer
rework; Tab now swallowed by the catcher), hover feedback on tabs /
paste / show / Save / +add-provider, and the first-ever Consumption
tab audit (wtype 2 while panel focus-catcher holds keyboard focus
switches tabs headlessly — verified): model bars no longer use
tone(share) (share 1.0 always tripped urgent-red — cap semantics leaked
into a ranking), zero-token models filtered, sparkline gained weekday
initials (today accented), model-name column 42→46%, active tab tint
0.22→0.32. Machine state: opencode key added by the user 2026-09-04.

`be80e01` trail → third "do it all" round (user: "lets do it" + "in omp we
also use subagent and roles too and we havent count the token of them yet"):
audit PROVED task-subagent transcripts (sessions/<proj>/<sess>/<Name>.jsonl)
and advisor transcripts were already globbed+counted — spawned QuixoticCougar
live and watched its 39.9K tokens land; the real gap is attribution (one
blended number) and oneshot completion() role calls, which write NO
transcript anywhere (model_perf is a decaying perf window, client_usage is
empty) — uncountable from disk, marked "role calls untracked" in the panel.
scan_omp now attributes lanes (main/advisor/subagent) with corpus v7 golden.
Found + fixed while verifying Tab routing: key-field focus never blocked the
PanelKeyCatcher, so typing j/k/h/l/x into a paste field was hijacked and
Enter never reached onAccepted; and the tab strip's unqualified `active`
resolved to WINDOW focus state (both tabs painted active — qml6 headless
test proved it; explicit viewTab === index now). Also shipped: Settings tab
(refresh interval, default provider, Qwen census caps, opt-in ≥90% cap
alerts via notify-send — off by default, crossing-only, one per refresh —
local-usage toggle), scrollable content (Flickable, no-op at equal heights),
persisted daily history (~/.local/state/h3nr1.d14z.ai-usage/history.json,
60d cap, provider caps ride along unsurfaced) + 30d trend bars in
Consumption, 2px model-bar floor. persistSetting no longer closes the panel
(clock-panel pattern). All three tabs visually verified post-deploy; gate
4× PASSED with lanes + history checks; metrics stable
(tokens_scanned=1012451417 / requests_scanned=31500 at corpus v7).

`a5980e7` trail → advisory follow-up (claimed flat model_usage records were
dropped by extract_omp): the advisory's evidence was self-referential — the
only model_usage/auto-thinking strings in OUR transcripts were our own tool
outputs and the advisory text itself — but the record type is REAL: two
genuine auto-thinking probe records (role:"tiny", flat top-level usage,
non-duplicative against assistant messages) in -.omp-agent/2026-09-03T20-14.
Shipped: extract_omp model_usage branch + walk_jsonl per-record lane router
(probes attribute to a shared roles lane from any file); explicit
completion() oneshots still leave NO record (re-verified post-flush —
my earlier mtime-watch conclusion stands); panel suffix now "oneshots
unlogged". Corpus v8 golden (+80 requests, 15 models). Live-verified:
Lanes line renders "… roles 668 · oneshots unlogged". Stale advisories
(blocked-gate, active-tab, harness-cap) arrived after those fixes shipped —
ignored. Metrics at v8: tokens_scanned=1012768276 / requests_scanned=31580.
