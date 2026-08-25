"""
tavily_backend.py — Tavily search backend for Scout.
Returns real URLs directly — no proxy resolution needed.
Requires: pip install tavily-python tldextract
"""
from __future__ import annotations
import logging
from urllib.parse import urlparse

import tldextract
from tavily import TavilyClient

from config import TAVILY_API_KEY
from schemas import Candidate

logger = logging.getLogger("pipeline_gc_bfs.tavily_backend")

_client = TavilyClient(api_key=TAVILY_API_KEY)

# News / directory / social sites — excluded at source so DeepSeek sees less noise
_EXCLUDE_DOMAINS = [
    "linkedin.com", "bloomberg.com", "crunchbase.com", "pitchbook.com",
    "reuters.com", "businesstimes.com.sg", "straitstimes.com",
    "channelnewsasia.com", "sgpbusiness.com", "bizfile.gov.sg", "acra.gov.sg",
    "wikipedia.org", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "dealstreetasia.com", "techinasia.com",
    "forbes.com", "fortune.com", "wsj.com", "ft.com", "cnbc.com",
    "futu.com", "futubull.com", "hkex.com.hk", "moomoo.com", "itiger.com", "etnet.com.hk",
]

# eTLD+1 keywords used as a post-filter safety net.
# Catches country-specific subdomains (e.g. sg.linkedin.com) that Tavily's
# exclude_domains param may not block when given only the apex domain.
_SOCIAL_KEYWORDS = {"linkedin", "facebook", "instagram", "twitter", "youtube", "xing"}


def _etld1(url: str) -> str:
    """Return eTLD+1 as dedup key, e.g. 'examplecap.sg'."""
    ext = tldextract.extract(url)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return urlparse(url).netloc.lower()  # fallback


def _path_depth(url: str) -> int:
    """Number of non-empty path segments — lower = closer to root = preferred."""
    path = urlparse(url).path.rstrip("/")
    return len([p for p in path.split("/") if p])


def search_tavily(company_name: str, tokens: dict) -> list[Candidate]:
    """
    Search Tavily for company_name official website.
    Increments tokens["tavily_calls"] by 1.
    Returns deduplicated Candidates (dedup key: eTLD+1).
    """
    tokens["tavily_calls"] += 1

    try:
        response = _client.search(
            query=f"{company_name} official website",
            search_depth="basic",
            max_results=8,
            exclude_domains=_EXCLUDE_DOMAINS,
        )
    except Exception as e:
        logger.warning(f"[tavily_backend] search failed: {e}")
        return []

    if isinstance(response, dict):
        results = response.get("results", [])
    else:
        results = getattr(response, "results", [])

    if not results:
        logger.warning("[tavily_backend] no results returned")
        return []

    logger.info(f"[tavily_backend] {len(results)} raw results")
    return dedupe_tavily(results)


def dedupe_tavily(results: list) -> list[Candidate]:
    """
    Deduplicate Tavily results by eTLD+1.

    URL:     keep the one closest to root (shortest path depth).
    snippet: merge all content excerpts from same host,
             sentence-level deduped, truncated to 600 chars.
    """
    seen: dict[str, Candidate] = {}
    url_depth: dict[str, int] = {}
    order: list[str] = []

    for r in results:
        if isinstance(r, dict):
            title   = (r.get("title",   "") or "").strip()
            url     = (r.get("url",     "") or "").strip()
            content = (r.get("content", "") or "").strip()
        else:
            title   = (getattr(r, "title",   "") or "").strip()
            url     = (getattr(r, "url",     "") or "").strip()
            content = (getattr(r, "content", "") or "").strip()

        if not url:
            continue

        # Post-filter: catch social subdomains (e.g. sg.linkedin.com) that
        # bypass Tavily's exclude_domains when only the apex domain is listed.
        ext = tldextract.extract(url)
        if ext.domain.lower() in _SOCIAL_KEYWORDS:
            logger.debug(f"[tavily_backend] post-filter dropped social URL: {url}")
            continue

        key   = _etld1(url)
        depth = _path_depth(url)

        if key in seen:
            existing = seen[key]
            # prefer URL closer to root; update title to match that entry
            if depth < url_depth[key]:
                existing.url = url
                existing.title = title
                url_depth[key] = depth
            # merge new content if not already present
            if content:
                existing_segs = set(existing.snippet.split(" | "))
                if content not in existing_segs:
                    joined = existing.snippet.rstrip(" |") + " | " + content
                    existing.snippet = joined.lstrip(" |")[:600]
        else:
            seen[key] = Candidate(title=title, snippet=content[:600], url=url)
            url_depth[key] = depth
            order.append(key)

    candidates = [seen[k] for k in order]
    logger.info(
        f"[tavily_backend] {len(results)} results → {len(candidates)} candidates after dedup"
    )
    for i, c in enumerate(candidates):
        logger.debug(f"  [{i}] {c.title!r} | url={c.url} | snippet={c.snippet[:80]!r}")

    return candidates
