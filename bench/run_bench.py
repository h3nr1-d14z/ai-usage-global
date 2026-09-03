#!/usr/bin/env python3
"""Benchmark harness for engine/usage.py — the canonical autoresearch workload.

Runs the production path (subprocess spawn of the engine, exactly as the QML
shell does on every refresh) against the deterministic corpus: fixture quota
transport, frozen clock, fake $HOME. Reports wall-time stats plus workload
invariants (tokens/requests scanned, output bytes) so an optimization that
quietly skips work is visible, not just faster.

Emits METRIC lines for autoresearch:
  METRIC engine_ms=<median wall ms>        (primary; lower is better)
  METRIC engine_min_ms=<min>
  METRIC engine_max_ms=<max>
  METRIC tokens_scanned=<local totalTokens>   (must stay constant)
  METRIC requests_scanned=<local totalRequests> (must stay constant)
  METRIC output_bytes=<stdout size>
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Time the usage engine")
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--warmup", type=int, default=2)
    args = parser.parse_args()

    corpus = ROOT / "bench/corpus"
    if not (corpus / "corpus.json").is_file():
        subprocess.run([sys.executable, str(ROOT / "tools/make_corpus.py"),
                        "--out", str(corpus)], check=True)

    manifest = json.loads((corpus / "corpus.json").read_text(encoding="utf-8"))
    env = dict(os.environ)
    env.update({
        "AIUSAGE_NOW": str(manifest["nowMs"]),
        "AIUSAGE_HOME": str(corpus / "home"),
        "AIUSAGE_FIXTURES": str(corpus / "fixtures"),
        "AIUSAGE_ENV_FILE": str(corpus / "env"),
        "TZ": "UTC",  # corpus day goldens are UTC-pinned
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    # census path must own the corpus runs (console branch = test_qwen_console)
    env.pop("QWEN_PLAN_COOKIE", None)
    cmd = [sys.executable, str(ROOT / "engine/usage.py"),
           "--settings", str(ROOT / "bench/settings.json")]

    def run_once() -> tuple[float, dict, int]:
        started = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=300)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if proc.returncode != 0:
            print(proc.stderr[-400:], file=sys.stderr)
            raise SystemExit(f"engine failed rc={proc.returncode}")
        doc = json.loads(proc.stdout)
        return elapsed_ms, doc, len(proc.stdout.encode("utf-8"))

    for _ in range(args.warmup):
        run_once()

    samples: list[float] = []
    doc: dict = {}
    out_bytes = 0
    for _ in range(args.repeats):
        ms, doc, out_bytes = run_once()
        samples.append(ms)

    local = doc.get("local", {})
    print(f"runs={args.repeats} median={statistics.median(samples):.1f}ms "
          f"min={min(samples):.1f} max={max(samples):.1f} "
          f"planes={doc['timings'].get('totalMs', doc['timings'].get('localMs'))}ms",
          file=sys.stderr)
    print(f"METRIC engine_ms={statistics.median(samples):.2f}")
    print(f"METRIC engine_min_ms={min(samples):.2f}")
    print(f"METRIC engine_max_ms={max(samples):.2f}")
    print(f"METRIC tokens_scanned={local.get('totalTokens', 0)}")
    print(f"METRIC requests_scanned={local.get('totalRequests', 0)}")
    print(f"METRIC output_bytes={out_bytes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
