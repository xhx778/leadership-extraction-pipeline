"""
node_scout.py — Node 1: Scout (orchestration layer)

Flow per attempt:
  1. Search backend (Gemini or Tavily) → list[Candidate]
  2. DeepSeek selects by index (sees title + snippet only, never URL)
  3. Resolve proxy URL (Gemini only) → real URL
  4. to_homepage_root() — pure string, no request
  5. Accessibility check → accept or retry

URL guarantee: every returned URL traces back to a real search result,
never to model free-text output.
"""
from __future__ import annotations
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from config import SKILLS_DIR, SCOUT_BACKEND
from schemas import Candidate, ScoutResult
from utils import load_prompt, parse_json_response, deepseek_call

logger = logging.getLogger("pipeline_gc_bfs.scout")

_LEGAL_SUFFIX = re.compile(
    r'\s*\b(PTE\.?\s*LTD\.?|PTE|LTD\.?|LIMITED|INC\.?|CORP\.?|CO\.?)\b\.?\s*$',
    re.IGNORECASE,
)

_PROXY_DOMAINS = {
    "vertexaisearch.cloud.google.com",
    "vertexaisearch.google.com",
}

# Known investor-relations subdomain prefixes. A page found on one of these
# still belongs to the company, but BFS's same-domain check is host-exact
# (see node_bfs_navigator._same_domain), so starting BFS there would trap
# it on the IR subdomain and never reach the main corporate site (or vice
# versa). Resolved to the apex domain here, before BFS runs.
_IR_SUBDOMAIN_PREFIXES = {
    "investor", "investors", "ir", "shareholder", "shareholders",
}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_name(company_name: str) -> str:
    cleaned = _LEGAL_SUFFIX.sub('', company_name.strip()).strip()
    return cleaned or company_name


def to_homepage_root(url: str) -> str:
    """Pure string operation — scheme + host + '/'. No HTTP request made."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def _apex_candidate(root_url: str) -> Optional[str]:
    """
    If root_url's host starts with a known IR subdomain prefix (e.g.
    "investor.example.com"), return the apex host candidate
    ("https://example.com/"). Returns None if there's no such prefix, or
    if the host has too few labels to safely strip one (e.g. a bare
    "example.com" or "example.co.uk").
    """
    p = urlparse(root_url)
    labels = p.netloc.split(".")
    if len(labels) < 3:
        return None
    if labels[0].lower() not in _IR_SUBDOMAIN_PREFIXES:
        return None
    apex_netloc = ".".join(labels[1:])
    return f"{p.scheme}://{apex_netloc}/"


async def resolve_proxy(proxy_url: str) -> Optional[str]:
    """
    Follow a Gemini grounding proxy redirect to reach the real URL.
    HEAD first; falls back to GET if HEAD fails.
    Returns None if still on a proxy domain after redirect.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=10,
            headers=_BROWSER_HEADERS,
        ) as client:
            try:
                r = await client.head(proxy_url)
            except Exception:
                r = await client.get(proxy_url)
            final_url = str(r.url)
            netloc = urlparse(final_url).netloc.lower()
            if netloc in _PROXY_DOMAINS:
                logger.debug(f"[scout] resolve_proxy: still on proxy after redirect: {final_url}")
                return None
            return final_url
    except Exception as e:
        logger.debug(f"[scout] resolve_proxy failed for {proxy_url!r}: {e}")
        return None


async def _verify_accessible(url: str) -> tuple[bool, str]:
    """
    Return (True, reason) if URL is reachable.
    403 is treated as reachable — site exists but blocks crawlers.
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15,
            headers=_BROWSER_HEADERS,
        ) as client:
            r = await client.get(url)
            if r.status_code < 400 or r.status_code == 403:
                return True, f"HTTP {r.status_code}"
            return False, f"HTTP {r.status_code}"
    except httpx.TimeoutException:
        return False, "Timeout"
    except httpx.ConnectError:
        return False, "Connection failed"
    except Exception as e:
        return False, type(e).__name__


# ── DeepSeek selector ─────────────────────────────────────────────────────────

def _build_candidates_block(candidates: list[Candidate]) -> str:
    lines: list[str] = []
    for i, c in enumerate(candidates):
        lines.append(f"[{i}] Title: {c.title}")
        lines.append(f"    Info: {c.snippet if c.snippet else '(no excerpt available)'}")
        lines.append("")
    return "\n".join(lines).strip()


def deepseek_select_official(
    company: str,
    candidates: list[Candidate],
    backend: str,
    tokens: dict,
) -> Optional[int]:
    """
    Ask DeepSeek to pick the candidate whose content matches the company's
    official website. Receives title + snippet only — URL is never shown.
    Returns selected index, or None if no match.
    """
    skill_file = f"scout_select_{backend}.md"
    candidates_block = _build_candidates_block(candidates)
    prompt = load_prompt(
        SKILLS_DIR, skill_file,
        company=company,
        candidates_block=candidates_block,
    )

    text = deepseek_call(prompt, tokens)
    try:
        data = parse_json_response(text)
        idx = data.get("official_index")
        reason = data.get("reason", "")
        logger.info(f"  [DeepSeek] index={idx}  reason={reason!r}")
        if idx is None:
            return None
        return int(idx)
    except Exception as e:
        logger.warning(f"  [DeepSeek] parse error: {e} | response: {text[:200]!r}")
        return None


# ── Main entry point ──────────────────────────────────────────────────────────

_FALLBACK_BACKEND = {"gemini": "tavily", "tavily": "gemini"}


async def scout(
    company_name: str,
    tokens: dict,
    backend: str = SCOUT_BACKEND,
) -> ScoutResult:
    """
    Find the official homepage root URL for company_name.
    Strategy: 2 attempts with the primary backend, then 2 attempts with the
    other backend if both primary attempts fail. Returns ScoutResult(status="found")
    on first success, or ScoutResult(status="not_found") after all 4 attempts.
    """
    search_name = _clean_name(company_name)
    if search_name != company_name:
        logger.info(f"  [Scout] Cleaned name: '{company_name}' → '{search_name}'")

    backends = [backend, _FALLBACK_BACKEND.get(backend, backend)]

    for attempt, current_backend in enumerate(backends, 1):
        logger.info(f"  [Scout] Attempt {attempt}/2 (backend={current_backend})")

        # ❶ Search (lazy import so unused backend's client is never initialised)
        if current_backend == "gemini":
            from gemini_backend import search_gemini
            candidates = search_gemini(search_name, tokens)
        else:
            from tavily_backend import search_tavily
            candidates = search_tavily(search_name, tokens)

        if not candidates:
            logger.info("  [Scout] No candidates returned — switching backend")
            continue

        logger.info(f"  [Scout] Candidates after dedup ({len(candidates)}):")
        for i, c in enumerate(candidates):
            logger.info(f"    [{i}] {c.title!r} | {c.url}")
            logger.info(f"         snippet: {c.snippet[:200]!r}")

        # ❷ DeepSeek selection (title + snippet only)
        idx = deepseek_select_official(search_name, candidates, current_backend, tokens)

        if idx is None:
            logger.info("  [Scout] DeepSeek returned null — switching backend")
            continue
        if idx >= len(candidates):
            logger.warning(f"  [Scout] DeepSeek index {idx} out of range ({len(candidates)}) — switching backend")
            continue

        selected = candidates[idx]
        logger.info(f"  [Scout] Selected [{idx}] {selected.title!r} | url={selected.url}")

        # ❸ Resolve URL (Gemini proxy → real; Tavily already real)
        if current_backend == "gemini":
            real_url = await resolve_proxy(selected.url)
            if not real_url:
                logger.info("  [Scout] Proxy resolve failed — switching backend")
                continue
        else:
            real_url = selected.url

        # ❹ Trim to homepage root — pure string, zero HTTP cost
        root_url = to_homepage_root(real_url)

        # ❺ Accessibility check
        accessible, reason = await _verify_accessible(root_url)
        logger.info(f"  [Scout] Accessibility: {root_url} → {reason}")

        if not accessible:
            logger.info(f"  [Scout] Not accessible ({reason}) — switching backend")
            continue

        # ❻ IR subdomain → prefer apex domain as the BFS starting point
        apex_url = _apex_candidate(root_url)
        if apex_url:
            apex_accessible, apex_reason = await _verify_accessible(apex_url)
            logger.info(f"  [Scout] IR subdomain detected — apex check: {apex_url} → {apex_reason}")
            if apex_accessible:
                logger.info(f"  [Scout] Preferring apex domain over IR subdomain: {root_url} → {apex_url}")
                root_url = apex_url

        logger.info(f"  [Scout] ✓ Found on attempt {attempt} (backend={current_backend}): {root_url}")
        return ScoutResult(
            status="found",
            url=root_url,
            evidence={
                "company":        company_name,
                "backend":        current_backend,
                "attempt":        attempt,
                "selected_index": idx,
                "selected_title": selected.title,
            },
        )

    logger.warning("  [Scout] Both attempts exhausted — not_found")
    return ScoutResult(status="not_found")
