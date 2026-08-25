"""
node_extractor.py — Node 3: Extractor
Two-step extraction:
  Step A (LLM)    : DeepSeek returns JSON [{name, title}] pairs
  Step B (Python) : classify_title() assigns seniority tier
Cost: 1 DeepSeek call per company.
"""
from __future__ import annotations
import json
import logging
import re
from typing import List

from schemas import Executive
from config import SKILLS_DIR, EXTRACTOR_MAX_TOTAL_CHARS
from config.seniority_loader import classify_title
from utils import load_prompt, deepseek_call, parse_json_response

logger = logging.getLogger("pipeline_gc_bfs.extractor")

_HONORIFICS = re.compile(r'^(Dr|Mr|Ms|Mrs|Prof)\.?\s+', re.IGNORECASE)


def _is_grounded(name: str, content: str) -> bool:
    """
    Return True if at least one token of NAME (honorifics stripped) literally
    appears in CONTENT. Guards against the LLM filling in plausible-looking
    executives from its own background knowledge when the real source text
    doesn't actually contain a usable leadership list.
    """
    core = _HONORIFICS.sub('', name).strip()
    tokens = [t for t in core.split() if len(t) >= 2]
    if not tokens:
        return False
    content_lower = content.lower()
    return any(t.lower() in content_lower for t in tokens)


def run(
    raw_content: str,
    company_name: str,
    tokens: dict,
) -> List[Executive]:
    """
    Extract executives from raw markdown content.

    Step A — LLM call : DeepSeek returns a JSON array of {name, title} pairs.
    Step B — Python   : classify_title() sets seniority_tier for each record.
    """
    logger.info(f"  Parsing {len(raw_content)} chars with DeepSeek...")
    if not raw_content or len(raw_content.strip()) < 20:
        return []

    content_slice = raw_content[:EXTRACTOR_MAX_TOTAL_CHARS]
    prompt = load_prompt(
        SKILLS_DIR, "extractor.md",
        company_name=company_name,
        raw_content=content_slice,
    )

    try:
        text = deepseek_call(prompt, tokens, temperature=0.1)
        logger.info(
            f"  tokens → DeepSeek in={tokens['deepseek_in']} out={tokens['deepseek_out']}"
        )
    except Exception as e:
        logger.error(f"  DeepSeek call failed: {e}")
        return []

    if not text or text.strip().upper() == "NONE":
        return []

    # Step A: parse JSON response
    try:
        raw_records = parse_json_response(text)
    except json.JSONDecodeError:
        logger.warning("  Could not parse JSON from extractor response")
        return []

    if not isinstance(raw_records, list):
        return []

    # Step B: Python classify
    executives: List[Executive] = []
    for record in raw_records:
        name  = record.get("name",  "").strip()
        title = record.get("title", "").strip()
        if not name or not title:
            continue

        if not _is_grounded(name, content_slice):
            logger.warning(f"  [ungrounded] Dropped {name!r} — not found in source content")
            continue

        tier = classify_title(title)

        executives.append(Executive(
            name=name,
            title=title,
            seniority_tier=tier,
        ))

    logger.info(f"  Extracted {len(executives)} candidates")
    return executives
