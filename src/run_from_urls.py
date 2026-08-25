"""
run_from_urls.py — Manual override: skip Node 1 Scout, feed known leadership URLs directly.

Use when Scout picked the wrong site/sub-brand and the correct board/management
page URL(s) are already known. Runs Node 2's Phase 2 rich re-crawl (SHOW_ALL_TABS_JS,
same nav/header/footer stripping as node_bfs_navigator.py) directly on the given
URLs — skipping BFS discovery and the identity gate — then Node 3 Extractor,
Node 4 Auditor, and dedup, exactly as pipeline.py does for a normal run.

Output is a PipelineResult with the same schema as results_latest.json, written to
a separate file so it never touches the main checkpoint/resume state.

Usage:
    python run_from_urls.py --company "Example Holdings Ltd." \
        --url https://www.example.com/about-us/our-leadership/board-of-directors \
        --url https://www.example.com/about-us/our-leadership/management-committee \
        --homepage https://www.example.com/
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Optional

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

from config import (
    RESULTS_DIR,
    CRAWL_PAGE_TIMEOUT_MS, CRAWL_DELAY_BEFORE_RETURN_S, CRAWL_HARD_TIMEOUT_S,
    EXTRACTOR_PAGE_MIN_CHARS, EXTRACTOR_MAX_TOTAL_CHARS,
)
from config.bfs_loader import get_title_pattern, get_heading_pattern
from utils import extract_preview, empty_tokens, classify_result
from schemas import PipelineResult
from node_bfs_navigator import SHOW_ALL_TABS_JS  # identical tab-exposure JS as the real pipeline
import node_extractor
import node_auditor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_from_urls")

_CHROME_TAGS = ["nav", "header", "footer"]
_WAIT_FOR_READY = "js:() => document.readyState === 'complete'"

_LEADERSHIP_CRAWL_CFG = CrawlerRunConfig(
    js_code=SHOW_ALL_TABS_JS,
    wait_for=_WAIT_FOR_READY,
    page_timeout=CRAWL_PAGE_TIMEOUT_MS,
    delay_before_return_html=CRAWL_DELAY_BEFORE_RETURN_S,
    excluded_tags=_CHROME_TAGS,
)

_TITLE_PATTERN   = get_title_pattern()
_HEADING_PATTERN = get_heading_pattern()


def _dedup_by_name(executives: list) -> list:
    """Same rule as pipeline.py — case-insensitive full-name dedup, keep first occurrence."""
    seen: set[str] = set()
    result = []
    for e in executives:
        key = e.name.strip().lower()
        if key not in seen:
            seen.add(key)
            result.append(e)
    return result


async def _recrawl(urls: list[str]) -> str:
    """Phase 2-equivalent rich re-crawl of known-good leadership URLs."""
    found_content: list[str] = []
    per_page_chars = max(EXTRACTOR_PAGE_MIN_CHARS, EXTRACTOR_MAX_TOTAL_CHARS // len(urls))
    browser_cfg = BrowserConfig(headless=True, verbose=False, ignore_https_errors=True)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for url in urls:
            logger.info(f"[ReCrawl] {url}")
            try:
                result = await asyncio.wait_for(
                    crawler.arun(url, config=_LEADERSHIP_CRAWL_CFG), timeout=CRAWL_HARD_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                logger.warning(f"  hard timeout after {CRAWL_HARD_TIMEOUT_S}s — skipped")
                continue
            except Exception as e:
                logger.warning(f"  {e}")
                continue
            if not result.success:
                logger.warning(
                    f"  fetch failed | status={getattr(result, 'status_code', '?')} "
                    f"| error={getattr(result, 'error_message', '?')!r}"
                )
                continue
            markdown = (result.markdown or result.cleaned_html or "").strip()
            if markdown:
                trimmed = extract_preview(markdown, per_page_chars, _TITLE_PATTERN, _HEADING_PATTERN)
                logger.info(f"  OK {len(markdown)} chars -> {len(trimmed)} chars kept")
                found_content.append(f"--- Source: {url} ---\n{trimmed}")

    return "\n\n".join(found_content)


async def run(company_name: str, urls: list[str], homepage: Optional[str] = None) -> PipelineResult:
    tokens = empty_tokens()
    t0 = time.time()

    raw_content = await _recrawl(urls)
    logger.info(f"content: {len(raw_content)} chars from {len(urls)} page(s)")

    if not raw_content:
        result = PipelineResult(
            company_name=company_name,
            official_website=homepage,
            bfs_urls=urls,
            source_type="official_website",
            extraction_method="manual_url_bfs_deepseek",
            auditor_notes="Manual re-crawl of provided URL(s) yielded no content.",
            elapsed_s=round(time.time() - t0, 1),
            **tokens,
        )
        result.failure_reason = classify_result(result)
        return result

    executives = node_extractor.run(raw_content, company_name, tokens)
    logger.info(f"extracted: {len(executives)}")

    passed_execs, audit_notes = node_auditor.audit(executives, company_name)
    logger.info(f"passed: {len(passed_execs)}/{len(executives)}")

    before_dedup = len(passed_execs)
    passed_execs = _dedup_by_name(passed_execs)
    if len(passed_execs) < before_dedup:
        logger.info(f"dedup: {before_dedup} -> {len(passed_execs)}")

    result = PipelineResult(
        company_name=company_name,
        official_website=homepage,
        bfs_urls=urls,
        source_type="official_website",
        executives=passed_execs,
        extraction_method="manual_url_bfs_deepseek",
        auditor_passed=len(passed_execs) > 0,
        auditor_notes=audit_notes,
        elapsed_s=round(time.time() - t0, 1),
        **tokens,
    )
    result.failure_reason = classify_result(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Node2-4 (BFS re-crawl + Extractor + Auditor + dedup) on manually supplied "
                    "leadership URL(s), skipping Node1 Scout."
    )
    parser.add_argument("--company", required=True, help="Exact company_name as in target_companies.csv")
    parser.add_argument("--url", action="append", required=True, dest="urls",
                        help="Leadership page URL; repeat --url for multiple pages")
    parser.add_argument("--homepage", default=None,
                        help="Official homepage root, stored in official_website (informational only)")
    parser.add_argument("--output", default=os.path.join(RESULTS_DIR, "manual_url_results.json"))
    args = parser.parse_args()

    result = asyncio.run(run(args.company, args.urls, args.homepage))

    status = "PASS" if result.auditor_passed else "FAIL"
    print(f"\n{status} {result.company_name} - {len(result.executives)} executive(s)")
    for e in result.executives:
        print(f"  [{e.seniority_tier}] {e.name} - {e.title}")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump([result.model_dump()], f, indent=2, ensure_ascii=False)
    print(f"\nSaved -> {args.output}")


if __name__ == "__main__":
    main()
