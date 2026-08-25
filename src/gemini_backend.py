"""
gemini_backend.py — Gemini grounding search backend for Scout.

Gemini's text output is discarded entirely.
Candidates are built from grounding metadata (grounding_chunks + grounding_supports).
"""
from __future__ import annotations
import logging

from google import genai
from google.genai import types

from config import GOOGLE_API_KEY, GEMINI_MODEL, SKILLS_DIR, SCOUT_THINKING_BUDGET
from schemas import Candidate
from utils import load_prompt

logger = logging.getLogger("pipeline_gc_bfs.gemini_backend")

_client = genai.Client(api_key=GOOGLE_API_KEY)


def search_gemini(company_name: str, tokens: dict) -> list[Candidate]:
    """
    Trigger a Gemini grounding search for company_name.
    Accumulates token counts into tokens in-place.
    Returns deduplicated Candidates built from grounding metadata.
    """
    prompt = load_prompt(SKILLS_DIR, "scout_search_gemini.md", company_name=company_name)

    try:
        response = _client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0,
                thinking_config=types.ThinkingConfig(thinking_budget=SCOUT_THINKING_BUDGET),
            ),
        )
    except Exception as e:
        logger.warning(f"[gemini_backend] search failed: {e}")
        return []

    meta = getattr(response, "usage_metadata", None)
    if meta:
        tokens["gemini_in"]    += getattr(meta, "prompt_token_count",     0)
        tokens["gemini_out"]   += getattr(meta, "candidates_token_count", 0)
        tokens["gemini_think"] += getattr(meta, "thoughts_token_count",   0)

    try:
        queries: list[str] = []
        for cand in response.candidates:
            gmeta = getattr(cand, "grounding_metadata", None)
            if gmeta:
                queries.extend(getattr(gmeta, "web_search_queries", None) or [])
        if queries:
            logger.info(f"[gemini_backend] search queries: {queries}")
    except Exception:
        pass

    return dedupe_gemini(response)


def dedupe_gemini(response) -> list[Candidate]:
    """
    Extract and deduplicate Candidates from Gemini grounding metadata.

    snippet = all support segment texts that cite this chunk, sentence-level deduped,
              merged across duplicate-title chunks, truncated to 600 chars.
    Dedup key = title.lower() — Gemini web.title is typically the site/company name.
    """
    chunk_list: list[dict] = []

    try:
        for cand in response.candidates:
            gmeta = getattr(cand, "grounding_metadata", None)
            if not gmeta:
                continue

            raw_chunks = getattr(gmeta, "grounding_chunks", None) or []
            base_idx = len(chunk_list)
            for chunk in raw_chunks:
                web = getattr(chunk, "web", None)
                if not web:
                    chunk_list.append(None)  # keep index alignment
                    continue
                chunk_list.append({
                    "title":    (getattr(web, "title", "") or "").strip(),
                    "url":      (getattr(web, "uri",   "") or "").strip(),
                    "segments": [],
                })

            # reverse-index: each support's segment text → every chunk it cites
            supports = getattr(gmeta, "grounding_supports", None) or []
            for support in supports:
                seg = getattr(support, "segment", None)
                text = (getattr(seg, "text", "") or "").strip()
                if not text:
                    continue
                for idx in (getattr(support, "grounding_chunk_indices", None) or []):
                    abs_idx = base_idx + idx
                    if 0 <= abs_idx < len(chunk_list) and chunk_list[abs_idx] is not None:
                        chunk_list[abs_idx]["segments"].append(text)

    except Exception as e:
        logger.debug(f"[gemini_backend] metadata extraction error: {e}")
        return []

    real_chunks = [ch for ch in chunk_list if ch is not None]
    if not real_chunks:
        logger.warning("[gemini_backend] no grounding chunks in response")
        return []

    # dedup by title; merge segments across same-title chunks
    seen: dict[str, Candidate] = {}
    order: list[str] = []

    for ch in real_chunks:
        title = ch["title"]
        if not title:
            continue
        key = title.lower()

        if key in seen:
            existing = seen[key]
            existing_segs = set(existing.snippet.split(" | "))
            new_parts: list[str] = []
            for seg in ch["segments"]:
                if seg not in existing_segs:
                    existing_segs.add(seg)
                    new_parts.append(seg)
            if new_parts:
                joined = existing.snippet.rstrip(" |") + " | " + " | ".join(new_parts)
                existing.snippet = joined.lstrip(" |")[:600]
        else:
            seen_segs: set[str] = set()
            deduped: list[str] = []
            for seg in ch["segments"]:
                if seg not in seen_segs:
                    seen_segs.add(seg)
                    deduped.append(seg)
            seen[key] = Candidate(
                title=title,
                snippet=" | ".join(deduped)[:600],
                url=ch["url"],
            )
            order.append(key)

    candidates = [seen[k] for k in order]
    logger.info(
        f"[gemini_backend] {len(real_chunks)} chunks → {len(candidates)} candidates after dedup"
    )
    for i, c in enumerate(candidates):
        logger.debug(f"  [{i}] {c.title!r} | snippet={c.snippet[:80]!r}")

    return candidates
