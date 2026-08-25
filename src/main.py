"""
main.py — Pipeline A-BFS entry point

Usage:
    python main.py                              # resume (default): skip done + permanent failures
    python main.py --mode fresh                 # back up checkpoint, run everything from scratch
    python main.py --mode rerun-failed          # retry only transient failures
    python main.py --company "Example Holdings Ltd."   # single company (for testing)

Output:
    results/results_latest.json   — canonical state of all companies
    results/failures_latest.csv   — failed companies for human review
    results/checkpoint.jsonl      — append-only audit log
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from typing import List

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import config as cfg
from schemas import PipelineResult
from pipeline import run_pipeline
from utils import classify_result, PERMANENT_FAILURE_REASONS


def _should_retry(r: PipelineResult) -> bool:
    """Transient failures retry unconditionally (until they pass or turn
    permanent). Permanent failures get exactly one retry, tracked via
    retry_count — a second permanent failure is left alone for good."""
    if r.auditor_passed:
        return False
    if r.failure_reason not in PERMANENT_FAILURE_REASONS:
        return True
    return r.retry_count == 0


# ── Logger ────────────────────────────────────────────────────────────────────

def _setup_logger(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("pipeline_gc_bfs")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        ))
        logger.addHandler(h)
    return logger


# ── Checkpoint helpers ────────────────────────────────────────────────────────

def append_to_checkpoint(path: str, result: PipelineResult) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "timestamp":  datetime.now().isoformat(),
        "company":    result.company_name,
        "status":     "passed" if result.auditor_passed else "failed",
        "exec_count": len(result.executives),
        "result":     result.model_dump(),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_checkpoint_dedup(path: str) -> dict[str, PipelineResult]:
    """Load checkpoint.jsonl and return one canonical PipelineResult per company.

    Dedup rule:
      1. Prefer records that have executives over those that don't.
      2. Among same exec-status, keep the most recent by timestamp.
    """
    if not os.path.exists(path):
        return {}

    grouped: dict[str, list[tuple[str, PipelineResult]]] = {}

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if "result" in entry:
                    result = PipelineResult(**entry["result"])
                    ts = entry["timestamp"]
                else:
                    result = PipelineResult(**entry)
                    ts = entry.get("extraction_timestamp", "")
                grouped.setdefault(result.company_name, []).append((ts, result))
            except Exception as e:
                logging.warning(f"Skipping corrupt checkpoint line: {e}")

    canonical: dict[str, PipelineResult] = {}
    for company, records in grouped.items():
        with_execs    = [(ts, r) for ts, r in records if r.executives]
        without_execs = [(ts, r) for ts, r in records if not r.executives]
        pool = with_execs if with_execs else without_execs
        canonical[company] = max(pool, key=lambda x: x[0])[1]

    return canonical


# ── Output helpers ────────────────────────────────────────────────────────────

def write_results_latest(
    canonical_results: dict[str, PipelineResult],
    output_dir: str,
) -> None:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "results_latest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            [r.model_dump() for r in canonical_results.values()],
            f, indent=2, ensure_ascii=False,
        )
    logging.getLogger("pipeline_gc_bfs").info(f"Saved results → {path}")


def write_failures_latest(
    canonical_results: dict[str, PipelineResult],
    output_dir: str,
) -> None:
    os.makedirs(output_dir, exist_ok=True)
    failures = [
        {
            "company_name":     r.company_name,
            "failure_reason":   r.failure_reason or "",
            "official_website": r.official_website or "",
            "exec_count":       len(r.executives),
            "source_type":      r.source_type,
            "auditor_notes":    r.auditor_notes[:200],
            "retry_count":      r.retry_count,
            "timestamp":        r.extraction_timestamp,
        }
        for r in canonical_results.values()
        if not r.auditor_passed
    ]
    path = os.path.join(output_dir, "failures_latest.csv")
    pd.DataFrame(failures).to_csv(path, index=False, encoding="utf-8-sig")
    logging.getLogger("pipeline_gc_bfs").info(f"Saved failures → {path}")


def _print_summary(
    session_results: List[PipelineResult],
    all_results: List[PipelineResult],
) -> None:
    n_session = len(session_results)
    n_all     = len(all_results)

    print(f"\n{'='*55}")
    print(f"  PIPELINE A-BFS — FINAL SUMMARY ({n_session} ran this session)")
    print(f"{'='*55}")

    for r in session_results:
        status = "✓" if r.auditor_passed else "✗"
        print(f"  {status} {r.company_name:<40} execs={len(r.executives):>3}")
        print(f"       homepage : {r.official_website}")
        print(f"       bfs_urls : {r.bfs_urls}")
        for e in r.executives:
            print(f"       [{(e.seniority_tier or ''):<15}] {e.name} — {e.title}")

    # ── Session token / call detail ────────────────────────────────────────
    s_g_in    = sum(r.gemini_in      for r in session_results)
    s_g_out   = sum(r.gemini_out     for r in session_results)
    s_g_think = sum(r.gemini_think   for r in session_results)
    s_ds_in   = sum(r.deepseek_in    for r in session_results)
    s_ds_out  = sum(r.deepseek_out   for r in session_results)
    s_tavily  = sum(r.tavily_calls   for r in session_results)
    s_elapsed = sum(r.elapsed_s      for r in session_results)

    print(f"\n{'='*55}")
    print(f"  SESSION USAGE ({n_session} companies)")
    print(f"{'='*55}")
    if s_g_in or s_g_out or s_g_think:
        print(f"  Gemini 2.5 Flash  input  : {s_g_in:>10,}")
        print(f"  Gemini 2.5 Flash  output : {s_g_out:>10,}")
        print(f"  Gemini 2.5 Flash  think  : {s_g_think:>10,}")
        print(f"  Gemini total              : {s_g_in+s_g_out+s_g_think:>10,}")
    print(f"  DeepSeek chat     input  : {s_ds_in:>10,}")
    print(f"  DeepSeek chat     output : {s_ds_out:>10,}")
    print(f"  DeepSeek total            : {s_ds_in+s_ds_out:>10,}")
    if s_tavily:
        print(f"  Tavily searches           : {s_tavily:>10,}")
    print(f"  Total elapsed (s)         : {s_elapsed:>10.1f}")

    # ── Cumulative totals across full checkpoint ───────────────────────────
    a_g_total  = sum(r.gemini_in + r.gemini_out + r.gemini_think for r in all_results)
    a_ds_total = sum(r.deepseek_in + r.deepseek_out               for r in all_results)
    a_tavily   = sum(r.tavily_calls                                for r in all_results)

    print(f"\n{'='*55}")
    print(f"  CUMULATIVE TOTALS ({n_all} companies in checkpoint)")
    print(f"{'='*55}")
    if a_g_total:
        print(f"  Gemini total              : {a_g_total:>10,}")
    print(f"  DeepSeek total            : {a_ds_total:>10,}")
    if a_tavily:
        print(f"  Tavily searches           : {a_tavily:>10,}")

    # ── Outcome breakdown (full checkpoint) ───────────────────────────────
    passed  = sum(1 for r in all_results if r.auditor_passed)
    failed  = n_all - passed
    reasons = Counter(r.failure_reason for r in all_results if not r.auditor_passed)
    print(f"\n{'='*55}")
    print(f"  OUTCOME BREAKDOWN ({n_all} companies in checkpoint)")
    print(f"{'='*55}")
    print(f"  Passed                    : {passed:>5}")
    print(f"  Failed                    : {failed:>5}")
    for reason, count in reasons.most_common():
        label = reason or "None"
        print(f"    {label:<29}: {count:>5}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def _run_all(
    to_run: List[str],
    canonical_results: dict[str, PipelineResult],
    checkpoint_path: str,
) -> tuple[dict[str, PipelineResult], List[PipelineResult]]:
    logger = logging.getLogger("pipeline_gc_bfs")
    session_results: List[PipelineResult] = []

    for i, company in enumerate(to_run, 1):
        logger.info(f"\nCompany {i}/{len(to_run)}: {company}")
        prior = canonical_results.get(company)
        try:
            result = await run_pipeline(company)
        except Exception as e:
            logger.error(f"Pipeline error for {company}: {e}")
            traceback.print_exc()
            result = PipelineResult(
                company_name      = company,
                source_type       = "no_website",
                extraction_method = cfg.EXTRACTION_METHOD,
                auditor_notes     = f"Pipeline error: {e}",
            )
            result.failure_reason = classify_result(result)

        if prior and not prior.auditor_passed and prior.failure_reason in PERMANENT_FAILURE_REASONS:
            result.retry_count = prior.retry_count + 1

        canonical_results[company] = result
        session_results.append(result)
        append_to_checkpoint(checkpoint_path, result)

        if i < len(to_run):
            time.sleep(1)

    return canonical_results, session_results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline A-BFS — Scout + Crawl4AI BFS + DeepSeek"
    )
    parser.add_argument("--input",   default=cfg.INPUT_CSV)
    parser.add_argument("--output",  default=cfg.RESULTS_DIR)
    parser.add_argument("--mode",    default="resume",
                        choices=["fresh", "resume", "rerun-failed"])
    parser.add_argument("--company", default=None, help="Run a single company (overrides mode)")
    parser.add_argument("--log",     default="INFO")
    args = parser.parse_args()

    logger = _setup_logger(args.log)

    # ── Startup key validation ─────────────────────────────────────────────
    if cfg.SCOUT_BACKEND == "gemini" and not cfg.GOOGLE_API_KEY:
        logger.error("GOOGLE_API_KEY not set — check .env")
        sys.exit(1)
    if cfg.SCOUT_BACKEND == "tavily" and not cfg.TAVILY_API_KEY:
        logger.error("TAVILY_API_KEY not set — check .env")
        sys.exit(1)
    if not cfg.DEEPSEEK_API_KEY or "your_" in cfg.DEEPSEEK_API_KEY:
        logger.error("DEEPSEEK_API_KEY not set — check .env")
        sys.exit(1)

    logger.info(f"Scout backend : {cfg.SCOUT_BACKEND}")
    logger.info(f"DeepSeek      : {cfg.DEEPSEEK_MODEL}")
    logger.info(f"BFS           : max_depth={cfg.BFS_MAX_DEPTH}")

    os.makedirs(args.output, exist_ok=True)
    checkpoint_path = os.path.join(args.output, "checkpoint.jsonl")

    # ── Initialise canonical_results based on mode ─────────────────────────
    if args.mode == "fresh":
        if os.path.exists(checkpoint_path):
            backup = os.path.join(
                args.output,
                f"checkpoint_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.jsonl",
            )
            os.rename(checkpoint_path, backup)
            logger.info(f"Backed up checkpoint → {backup}")
        canonical_results: dict[str, PipelineResult] = {}
    else:
        canonical_results = load_checkpoint_dedup(checkpoint_path)
        logger.info(f"Loaded {len(canonical_results)} companies from checkpoint")

    # ── Build to_run ───────────────────────────────────────────────────────
    if args.company:
        to_run = [args.company]
    elif args.mode == "rerun-failed":
        to_run = [
            company for company, r in canonical_results.items()
            if _should_retry(r)
        ]
        logger.info(f"rerun-failed: {len(to_run)} failure(s) to retry")
    else:
        df = pd.read_csv(args.input)
        company_list = df["company_name"].dropna().tolist()
        logger.info(f"Loaded {len(company_list)} companies from {args.input}")
        if args.mode == "resume":
            to_run = [
                c for c in company_list
                if c not in canonical_results
                or _should_retry(canonical_results[c])
            ]
            logger.info(f"resume: {len(company_list) - len(to_run)} skipped, {len(to_run)} to run")
        else:
            to_run = company_list

    # ── Run ────────────────────────────────────────────────────────────────
    canonical_results, session_results = asyncio.run(
        _run_all(to_run, canonical_results, checkpoint_path)
    )

    # ── Persist outputs ────────────────────────────────────────────────────
    write_results_latest(canonical_results, args.output)
    write_failures_latest(canonical_results, args.output)
    _print_summary(session_results, list(canonical_results.values()))


if __name__ == "__main__":
    main()
