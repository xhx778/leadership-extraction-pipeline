"""
pipeline.py — Run the full pipeline for a single company.

Node 1  Scout          (Gemini or Tavily + DeepSeek select)  → official_website (homepage root)
Node 2  BFS Navigator  (crawl4ai, 0 API calls)               → BFS discovery + rich re-crawl
Node 3  Extractor      (DeepSeek, 1 call)                    → Executive list
Node 4  Auditor        (pure Python, 0 calls)                → filtered Executive list
       Dedup           (Python, 0 calls)                     → name-dedup (Board + Mgmt overlap)
"""
from __future__ import annotations
import logging
import time

from config import EXTRACTION_METHOD
from schemas import PipelineResult
from utils import empty_tokens, classify_result
import node_scout
import node_bfs_navigator
import node_extractor
import node_auditor

logger = logging.getLogger("pipeline_gc_bfs.pipeline")


def _dedup_by_name(executives: list) -> list:
    """Remove duplicate executives that appear on multiple pages (e.g. Board + Mgmt).
    Keeps the first occurrence; comparison is case-insensitive on the full name."""
    seen: set[str] = set()
    result = []
    for e in executives:
        key = e.name.strip().lower()
        if key not in seen:
            seen.add(key)
            result.append(e)
    return result


async def run_pipeline(company_name: str) -> PipelineResult:
    logger.info(f"\n{'='*55}\n  {company_name}\n{'='*55}")
    tokens = empty_tokens()
    t0 = time.time()

    # ── Node 1: Scout ─────────────────────────────────────────────────────
    scout_result = await node_scout.scout(company_name, tokens)

    if scout_result.status != "found" or not scout_result.url:
        logger.warning("  Scout could not find an accessible official website — pipeline stopped.")
        result = PipelineResult(
            company_name      = company_name,
            source_type       = "no_website",
            extraction_method = EXTRACTION_METHOD,
            auditor_notes     = "Scout could not find an accessible official website after all retries.",
            elapsed_s         = round(time.time() - t0, 1),
            **tokens,
        )
        result.failure_reason = "scout_failed_invalid_homepage"
        return result

    official_website = scout_result.url

    # ── Node 2: BFS Navigator ─────────────────────────────────────────────
    raw_content, bfs_urls, bfs_failure = await node_bfs_navigator.run(
        official_website, company_name, tokens
    )
    logger.info(f"  bfs_urls : {bfs_urls}")
    logger.info(f"  content  : {len(raw_content)} chars from {len(bfs_urls)} page(s)")

    if bfs_failure:
        logger.warning(f"  [Identity gate] halted — {bfs_failure['reason']}: {bfs_failure['note']}")
        result = PipelineResult(
            company_name      = company_name,
            official_website  = official_website,
            source_type       = "official_website",
            extraction_method = EXTRACTION_METHOD,
            auditor_notes     = bfs_failure["note"],
            elapsed_s         = round(time.time() - t0, 1),
            **tokens,
        )
        result.failure_reason = bfs_failure["reason"]
        return result

    if not raw_content:
        result = PipelineResult(
            company_name      = company_name,
            official_website  = official_website,
            source_type       = "official_website",
            extraction_method = EXTRACTION_METHOD,
            auditor_notes     = "BFS found no leadership page content.",
            elapsed_s         = round(time.time() - t0, 1),
            **tokens,
        )
        result.failure_reason = classify_result(result)
        return result

    # ── Node 3: Extractor ─────────────────────────────────────────────────
    executives = node_extractor.run(raw_content, company_name, tokens)
    logger.info(f"  extracted: {len(executives)}")

    # ── Node 4: Auditor ───────────────────────────────────────────────────
    passed_execs, audit_notes = node_auditor.audit(executives, company_name)
    logger.info(f"  passed   : {len(passed_execs)}/{len(executives)}")

    before_dedup = len(passed_execs)
    passed_execs = _dedup_by_name(passed_execs)
    if len(passed_execs) < before_dedup:
        logger.info(f"  dedup    : {before_dedup} → {len(passed_execs)} (removed {before_dedup - len(passed_execs)} duplicate(s))")

    g_total  = tokens["gemini_in"] + tokens["gemini_out"] + tokens["gemini_think"]
    ds_total = tokens["deepseek_in"] + tokens["deepseek_out"]
    elapsed  = round(time.time() - t0, 1)
    logger.info(
        f"  tokens → Gemini {g_total:,} | DeepSeek {ds_total:,} | "
        f"Tavily {tokens['tavily_calls']} call(s) | {elapsed}s"
    )

    result = PipelineResult(
        company_name      = company_name,
        official_website  = official_website,
        bfs_urls          = bfs_urls,
        source_type       = "official_website",
        executives        = passed_execs,
        extraction_method = EXTRACTION_METHOD,
        auditor_passed    = len(passed_execs) > 0,
        auditor_notes     = audit_notes,
        elapsed_s         = elapsed,
        **tokens,
    )
    result.failure_reason = classify_result(result)
    return result
