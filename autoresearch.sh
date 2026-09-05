#!/usr/bin/env bash
# Canonical benchmark entrypoint for the AI Usage Global harness.
#
# Workload: engine/usage.py, run through the exact production path (subprocess
# spawn, one JSON document on stdout) against a deterministic corpus:
#   - fake $HOME with real-shaped agent stores (OpenCode SQLite 30k rows,
#     Claude/Codex/Qwen/OMP JSONL transcripts)
#   - frozen quota responses (fixture transport; NO live network)
#   - frozen wall clock (AIUSAGE_NOW)
#
# Metrics:
#   METRIC engine_ms      — median wall time per full refresh (primary; lower better)
#   METRIC engine_min_ms / engine_max_ms — spread
#   METRIC tokens_scanned / requests_scanned — workload invariants (must not move)
#   METRIC output_bytes   — document size
#
# Correctness gate: validate + fallback + add-key + console + trollllm +
# newapi suites must pass before any timing is reported. Non-zero exit =>
# checks_failed.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

: > bench/last-validation.log  # one record per suite; truncate once

# 1. Corpus (idempotent; same seed/version => byte-identical).
"$PY" tools/make_corpus.py --out bench/corpus >/dev/null

# 2. Correctness gate — fail fast, never emit metrics on a broken engine.
for t in tests/validate.py tests/test_sql_fallback.py tests/test_add_key.py \
         tests/test_qwen_console.py tests/test_trollllm.py tests/test_newapi.py; do
  if ! "$PY" "$t" >> bench/last-validation.log 2>&1; then
    echo "VALIDATION FAILED ($t) — see bench/last-validation.log" >&2
    tail -n 20 bench/last-validation.log >&2 || true
    exit 1
  fi
done

# 3. Timed runs. Warmup absorbs disk-cache and interpreter-startup noise.
"$PY" bench/run_bench.py --repeats 9 --warmup 3
