"""
node_bfs_navigator.py — Node 2: BFS Navigator
Uses crawl4ai (Playwright) to BFS from the homepage to discover leadership page(s).

Three-step design:
  Step 1 — BFS exploration (lightweight crawl, no JS):
    classify_page() → "list" / "not-list"  (binary; intentionally loose recall)
      "list"     → candidate for Step 3; do NOT descend
      "not-list" → extract child links using strong/weak signal categorisation

  Step 2 — LLM link pruning (DeepSeek, per layer, only when > threshold):
    When a BFS layer yields > BFS_LLM_LINK_THRESHOLD candidate links,
    call DeepSeek to pick the BFS_LLM_LINK_MAX_SELECT most relevant ones.

  Step 3 — LLM page confirmation (DeepSeek, 1 call total):
    All candidates are sent to DeepSeek with a URL + content preview.
    LLM decides which are true leadership list pages. → confirmed_urls

  Phase 2 — Rich re-crawl (SHOW_ALL_TABS_JS, only on confirmed_urls):
    Re-crawl each confirmed URL with JS tab-exposure to get full content.
    Merge for Extractor.
"""
from __future__ import annotations
import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx
from lxml import html as lhtml

logging.getLogger("crawl4ai").setLevel(logging.ERROR)
logging.getLogger("asyncio").setLevel(logging.WARNING)

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

from config import (
    BFS_MAX_DEPTH,
    BFS_LLM_LINK_THRESHOLD, BFS_LLM_LINK_MAX_SELECT, BFS_CONTENT_PREVIEW_CHARS,
    EXTRACTOR_PAGE_MIN_CHARS, EXTRACTOR_MAX_TOTAL_CHARS,
    CLASSIFY_HEADING_SEARCH_CHARS,
    CRAWL_PAGE_TIMEOUT_MS, CRAWL_DELAY_BEFORE_RETURN_S, CRAWL_HARD_TIMEOUT_S,
    STRONG_URL_SIGNALS, WEAK_URL_SIGNALS, EXCLUDE_PATTERNS,
    LANG_HREF_SIGNALS, LANG_TEXT_SIGNALS,
    SKILLS_DIR,
    IDENTITY_GATE_EMPTY_THRESHOLD,
)
from utils import load_prompt, deepseek_call, parse_json_response, extract_preview
from config.bfs_loader import get_title_pattern, get_heading_pattern

logger = logging.getLogger("pipeline_gc_bfs.bfs_navigator")

# ── JS: expose all tab content ────────────────────────────────────────────────

SHOW_ALL_TABS_JS = """
(async () => {
    const inChrome = (el) => !!el.closest('nav, header, footer');
    const tabTriggers = [
        '[role="tab"]',
        'button[class*="tab"]', 'li[class*="tab"] > a',
        'a[data-toggle="tab"]', 'a[data-bs-toggle="tab"]',
        'button[data-bs-toggle="tab"]', 'button[data-toggle="tab"]',
        '.tab-item', '.tab-link',
        '.nav-pills li a', '.nav-tabs li a',
        'ul[role="tablist"] li',
    ];
    const clicked = new Set();
    for (const sel of tabTriggers) {
        for (const el of document.querySelectorAll(sel)) {
            if (inChrome(el)) continue;
            const key = (el.innerText || el.getAttribute('aria-label') || '').trim();
            if (!key || clicked.has(key)) continue;
            clicked.add(key);
            try { el.click(); await new Promise(r => setTimeout(r, 400)); } catch(e) {}
        }
    }
    for (const el of document.querySelectorAll('[role="tabpanel"], .tab-pane, .tab-content > *')) {
        if (inChrome(el)) continue;
        el.style.setProperty('display',    'block',   'important');
        el.style.setProperty('visibility', 'visible', 'important');
        el.style.setProperty('opacity',    '1',       'important');
        el.style.setProperty('height',     'auto',    'important');
        el.style.setProperty('overflow',   'visible', 'important');
        el.removeAttribute('hidden');
    }
    for (const el of document.querySelectorAll(
        '[role="tabpanel"][aria-hidden], .tab-pane[aria-hidden], ' +
        'section[aria-hidden="true"], article[aria-hidden="true"], ' +
        '[role="main"] [aria-hidden="true"]'
    )) {
        if (inChrome(el)) continue;
        el.removeAttribute('aria-hidden');
    }
})();
"""

# ── Crawler configs ───────────────────────────────────────────────────────────

_CHROME_TAGS = ["nav", "header", "footer"]

_WAIT_FOR_READY = "js:() => document.readyState === 'complete'"

_CRAWL_CFG = CrawlerRunConfig(
    wait_for=_WAIT_FOR_READY,
    page_timeout=CRAWL_PAGE_TIMEOUT_MS,
    delay_before_return_html=CRAWL_DELAY_BEFORE_RETURN_S,
)
_LEADERSHIP_CRAWL_CFG = CrawlerRunConfig(
    js_code=SHOW_ALL_TABS_JS,
    wait_for=_WAIT_FOR_READY,
    page_timeout=CRAWL_PAGE_TIMEOUT_MS,
    delay_before_return_html=CRAWL_DELAY_BEFORE_RETURN_S,
    excluded_tags=_CHROME_TAGS,
)

# ── Regex patterns (loaded from config/bfs_patterns.yaml) ────────────────────

_LEADERSHIP_TITLE_PATTERN   = get_title_pattern()
_LEADERSHIP_HEADING_PATTERN = get_heading_pattern()



# ── Content classification ────────────────────────────────────────────────────

_GLUED_WORD_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _count_titles(text: str) -> int:
    return len(_LEADERSHIP_TITLE_PATTERN.findall(text))


_CHROME_CLASS_TOKENS = {"header", "footer", "nav", "navbar", "navigation"}


def _strip_chrome_text(html: str) -> str:
    """
    Plain text with nav/header/footer removed, for classify_page() only.
    Runs on the already-fetched cleaned_html (no extra request) so it has zero
    effect on link discovery — result.links was extracted by crawl4ai from the
    full, un-stripped DOM during the crawl itself.

    Also strips div/section chrome that frameworks (styled-components etc.)
    build without real <nav>/<header>/<footer> tags — matched by an exact
    class-token match (e.g. class="Header") rather than substring, so nested
    content components like "CardHeader"/"TeamMemberHeader" are left alone.

    Also un-glues "NameTitle" runs (e.g. "Jane DoeChairman") that markdown
    conversion produces when a name and title sit in adjacent inline DOM nodes
    with no separating whitespace — otherwise \\b-anchored title regexes never
    match the title half, and real leadership grids undercount to "not-list".
    """
    if not html:
        return ""
    try:
        tree = lhtml.fromstring(html)
    except Exception:
        return ""
    for tag in _CHROME_TAGS:
        for el in tree.xpath(f".//{tag}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    for el in tree.xpath(".//*[@class]"):
        tokens = el.get("class", "").split()
        if any(t.lower() in _CHROME_CLASS_TOKENS for t in tokens):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    return _GLUED_WORD_RE.sub(" ", tree.text_content())


def classify_page(
    markdown: str,
    url: str,
    homepage_url: str = "",
    title_count: int = -1,
    has_strong_url: bool | None = None,
) -> str:
    """Return 'list' or 'not-list'. Intentionally loose — Step 3 LLM is the precision filter."""
    if homepage_url and url.rstrip("/") == homepage_url.rstrip("/"):
        return "not-list"

    if title_count < 0:
        title_count = _count_titles(markdown)
    if has_strong_url is None:
        has_strong_url = any(sig in url.lower() for sig in STRONG_URL_SIGNALS)
    heading_match = bool(_LEADERSHIP_HEADING_PATTERN.search(markdown[:CLASSIFY_HEADING_SEARCH_CHARS]))

    if has_strong_url and title_count >= 2:
        return "list"
    if heading_match and title_count >= 2:
        return "list"
    if title_count >= 10:
        return "list"
    return "not-list"


# ── URL helpers ───────────────────────────────────────────────────────────────

def _make_absolute(href: str, base: str) -> str:
    if not href or href.startswith(("mailto:", "tel:", "javascript:")):
        return ""
    return href if href.startswith("http") else urljoin(base, href)


def _same_domain(url: str, homepage: str) -> bool:
    return urlparse(url).netloc == urlparse(homepage).netloc


def _normalize(url: str) -> str:
    return urlparse(url)._replace(fragment="").geturl()



# ── Step 2: LLM link pruning ──────────────────────────────────────────────────

def _llm_select_links(
    candidates: list[tuple[str, str]],   # (abs_url, link_text)
    company_name: str,
    tokens: dict,
) -> list[str]:
    """
    When a BFS layer has > BFS_LLM_LINK_THRESHOLD candidates, ask DeepSeek
    to pick the BFS_LLM_LINK_MAX_SELECT most likely to lead to a leadership page.
    Returns a list of absolute URLs.
    """
    lines = "\n".join(
        f"{i+1:>2}. {urlparse(url).path or '/'} | {text[:60]}"
        for i, (url, text) in enumerate(candidates)
    )
    prompt = load_prompt(
        SKILLS_DIR, "navigator_link_selector.md",
        company_name=company_name,
        count=len(candidates),
        max_select=BFS_LLM_LINK_MAX_SELECT,
        links=lines,
    )
    try:
        response = deepseek_call(prompt, tokens, temperature=0.0)
        selected_paths = parse_json_response(response)
        if not isinstance(selected_paths, list):
            raise ValueError("Expected JSON array")

        # Match selected paths back to absolute URLs
        path_to_abs = {urlparse(url).path: url for url, _ in candidates}
        result = []
        for path in selected_paths:
            path = path.strip()
            if path in path_to_abs:
                result.append(path_to_abs[path])
            else:
                # Fallback: find any candidate whose path starts with the returned value
                for abs_url in path_to_abs.values():
                    if urlparse(abs_url).path == path:
                        result.append(abs_url)
                        break
        logger.info(
            f"    [LLM-links] {len(candidates)} → {len(result)} selected: "
            + str([urlparse(u).path for u in result])
        )
        return result or [url for url, _ in candidates[:BFS_LLM_LINK_MAX_SELECT]]
    except Exception as e:
        logger.warning(f"    [LLM-links] Error: {e} — using top {BFS_LLM_LINK_MAX_SELECT}")
        return [url for url, _ in candidates[:BFS_LLM_LINK_MAX_SELECT]]


# ── Step 3: LLM page classification ──────────────────────────────────────────

def _llm_classify_pages(
    candidates: list[dict],   # {url, preview, title_count, rule_type}
    company_name: str,
    tokens: dict,
) -> list[str]:
    """
    Ask DeepSeek to decide which candidate pages are true leadership list pages.
    Returns confirmed URLs only.
    """
    if not candidates:
        return []

    pages_text = ""
    for i, c in enumerate(candidates, 1):
        pages_text += (
            f"{i}. URL: {c['url']}\n"
            f"   Title hits: {c['title_count']}\n"
            f"   Preview: {c['preview'][:BFS_CONTENT_PREVIEW_CHARS]}\n\n"
        )

    prompt = load_prompt(
        SKILLS_DIR, "navigator_page_classifier.md",
        company_name=company_name,
        count=len(candidates),
        pages=pages_text.strip(),
    )
    try:
        response = deepseek_call(prompt, tokens, temperature=0.0)
        data = parse_json_response(response)
        if not isinstance(data, dict) or "confirmed" not in data:
            raise ValueError("Expected {\"confirmed\": [...]}")
        confirmed_items = data["confirmed"]

        confirmed: list[str] = []
        for item in confirmed_items:
            idx = item.get("index")
            if not isinstance(idx, int) or not (1 <= idx <= len(candidates)):
                logger.warning(f"    [LLM-pages] index {idx!r} out of range — skipped")
                continue
            url = candidates[idx - 1]["url"]
            confirmed.append(url)
            logger.info(f"    [LLM-pages] ✓ confirmed: [{idx}] {url} — {item.get('reason','')}")
        return confirmed
    except Exception as e:
        logger.warning(f"    [LLM-pages] Error: {e} — falling back to all rule-list candidates")
        return [c["url"] for c in candidates]


# ── Identity gate ────────────────────────────────────────────────────────────

def extract_identity_zones(markdown: str, metadata: dict, cleaned_html: str = "") -> str:
    """Build a short tagged string of identity signals from L0's already-crawled content."""
    parts: list[str] = []

    # [TITLE] — from result.metadata (Crawl4AI strips <head> from cleaned_html)
    title = ((metadata or {}).get("title") or "").strip()
    if title:
        parts.append(f"[TITLE] {title}")

    # [FOOTER_COPYRIGHT] — collect ALL copyright markers (up to 3).
    # Bilingual sites (CN + EN) often have two separate copyright lines;
    # taking only the first may capture the Chinese name and miss the English one.
    # Fall back to cleaned_html when nothing is found in markdown.
    _COPYRIGHT_RE = re.compile(
        r'(?:©|Â©|&copy;|\(c\)|copyright|版权所有|版权)',
        re.IGNORECASE,
    )
    copyright_snippets: list[str] = []
    seen_copy: set[str] = set()
    copy_m = None
    for copy_m in _COPYRIGHT_RE.finditer(markdown):
        pos = copy_m.start()
        snippet = markdown[max(0, pos - 20): pos + 200].strip()
        if snippet and snippet not in seen_copy:
            seen_copy.add(snippet)
            copyright_snippets.append(snippet)
        if len(copyright_snippets) >= 3:
            break
    if copyright_snippets:
        parts.append(f"[FOOTER_COPYRIGHT] {' || '.join(copyright_snippets)}")
    elif cleaned_html:
        html_snippets: list[str] = []
        for copy_m_html in _COPYRIGHT_RE.finditer(cleaned_html):
            pos = copy_m_html.start()
            raw = cleaned_html[max(0, pos - 50): pos + 300]
            text = re.sub(r'<[^>]+>', ' ', raw)
            text = re.sub(r'\s+', ' ', text).strip()
            if text and text not in html_snippets:
                html_snippets.append(text[:220])
            if len(html_snippets) >= 3:
                break
        if html_snippets:
            parts.append(f"[FOOTER_COPYRIGHT] {' || '.join(html_snippets)}")

    # [ABOUT_CONTACT_ANCHORS] — optional weak signal, link text only
    anchor_texts = re.findall(r'\[([^\]\n]{1,40})\]\(', markdown)
    kw = re.compile(r'about|contact|company|关于|联系', re.IGNORECASE)
    seen: set[str] = set()
    filtered: list[str] = []
    for t in anchor_texts:
        t = t.strip()
        if kw.search(t) and t not in seen:
            seen.add(t)
            filtered.append(t)
    if filtered:
        parts.append(f"[ABOUT_CONTACT_ANCHORS] {' | '.join(filtered[:8])}")

    # [PAGE_TOP] — always included; company name/logo text appears near the top.
    top = markdown[:600].strip()
    if top:
        parts.append(f"[PAGE_TOP] {top}")

    # [PAGE_TAIL] — included when no copyright found in markdown (plain tail fallback).
    if not copy_m:
        tail = markdown[-1000:].strip()
        if tail and tail != top:
            parts.append(f"[PAGE_TAIL] {tail}")

    return "\n".join(parts)


def verify_identity(
    company: str,
    identity_text: str,
    tokens: dict,
) -> tuple[str, str]:
    """
    Call DeepSeek to decide whether the homepage belongs to `company`.
    Returns (verdict_code, note):
      verdict_code ∈ {"verified", "false_url", "need_review"}
      note         = brief reason string for auditor_notes
    """
    prompt = load_prompt(
        SKILLS_DIR, "identity_gate.md",
        company=company,
        identity_text=identity_text,
    )
    try:
        response = deepseek_call(prompt, tokens, temperature=0.0)
        data = parse_json_response(response)
        raw_verdict    = str(data.get("verdict", "UNSURE")).upper()
        reason         = str(data.get("reason", ""))
        matched_entity = str(data.get("matched_entity", ""))

        if raw_verdict == "YES":
            verdict_code = "verified"
        elif raw_verdict == "NO":
            verdict_code = "false_url"
        else:
            verdict_code = "need_review"

        note = f"{matched_entity}; {reason}" if matched_entity else reason
        return verdict_code, note
    except Exception as e:
        logger.warning(f"    [Identity gate] verify_identity error: {e} — treating as need_review")
        return "need_review", f"parse error: {e}"


# ── URL resolution ───────────────────────────────────────────────────────────

async def _resolve_final_url(url: str, timeout: int = 10) -> str:
    """Follow HTTP redirects and return the final landed URL."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            response = await client.get(url)
            return str(response.url)
    except Exception:
        return url


# ── BFS ───────────────────────────────────────────────────────────────────────

async def run(
    homepage: str,
    company_name: str,
    tokens: dict,
    max_depth: int = BFS_MAX_DEPTH,
) -> tuple[str, list[str], Optional[dict]]:
    """
    Step 1  BFS discovery → collect candidates
    Step 2  LLM link pruning per layer (when > threshold)
    Step 3  LLM page classification → confirmed list URLs
    Phase 2 Rich re-crawl with SHOW_ALL_TABS_JS → merged content

    Returns:
        combined_content : str
        confirmed_urls   : list[str]
        bfs_failure      : None on success; {"reason": str, "note": str} on identity gate halt
    """
    scout_homepage = homepage
    homepage = await _resolve_final_url(homepage)
    if homepage != scout_homepage:
        logger.info(f"  [URL resolve] {scout_homepage} → {homepage}")

    visited:    set[str]              = set()
    queue:      list[tuple[str, int]] = [(homepage, 0)]
    candidates: list[dict]            = []   # pages for Step 3

    browser_cfg = BrowserConfig(headless=True, verbose=False, ignore_https_errors=True)

    async with AsyncWebCrawler(config=browser_cfg) as crawler:

        # ── Step 1 + 2: BFS with per-layer LLM pruning ────────────────────
        while queue:
            url, depth = queue.pop(0)
            url = _normalize(url)

            if url in visited or depth > max_depth:
                continue

            visited.add(url)
            logger.info(f"  [BFS L{depth}] {url}")

            try:
                result = await asyncio.wait_for(
                    crawler.arun(url, config=_CRAWL_CFG), timeout=CRAWL_HARD_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                logger.info(f"    ✗ hard timeout after {CRAWL_HARD_TIMEOUT_S}s — crawl4ai never returned")
                continue
            except Exception as e:
                logger.info(f"    ✗ {e}")
                continue

            if not result.success:
                logger.info(
                    f"    ✗ fetch failed | status={getattr(result, 'status_code', '?')} "
                    f"| error={getattr(result, 'error_message', '?')!r}"
                )
                continue

            markdown = (result.markdown or result.cleaned_html or "").strip()

            # ── Identity gate: fires only at L0 (homepage), after successful fetch ──
            if depth == 0:
                if len(markdown) < IDENTITY_GATE_EMPTY_THRESHOLD:
                    note = (
                        f"Homepage rendered only {len(markdown)} chars "
                        f"(threshold {IDENTITY_GATE_EMPTY_THRESHOLD})"
                    )
                    logger.info(f"    [Identity gate] empty_page — {note}")
                    return "", [], {"reason": "empty_page", "note": note}

                identity_text = extract_identity_zones(
                    markdown, result.metadata or {}, result.cleaned_html or ""
                )
                logger.info(f"    [Identity gate] zones extracted ({len(identity_text)} chars)")
                verdict, note = verify_identity(company_name, identity_text, tokens)
                logger.info(f"    [Identity gate] verdict={verdict} — {note}")

                if verdict != "verified":
                    reason = "false_url" if verdict == "false_url" else "identity_unconfirmed"
                    return "", [], {"reason": reason, "note": f"Identity gate [{verdict}]: {note}"}

                logger.info("    [Identity gate] ✓ verified — proceeding with BFS")

            elif not markdown:
                continue
            # ── end identity gate ────────────────────────────────────────────────

            classify_text  = _strip_chrome_text(result.cleaned_html or "") or _GLUED_WORD_RE.sub(" ", markdown)
            title_count    = _count_titles(classify_text)
            has_strong_url = any(sig in url.lower() for sig in STRONG_URL_SIGNALS)
            page_type      = classify_page(classify_text, url, homepage, title_count=title_count, has_strong_url=has_strong_url)
            logger.info(
                f"    → {page_type} ({title_count} title hits, {len(markdown)} chars, "
                f"strong_url={has_strong_url})"
            )

            if page_type == "list":
                candidates.append({
                    "url":         url,
                    "preview":     extract_preview(
                        markdown, BFS_CONTENT_PREVIEW_CHARS,
                        _LEADERSHIP_TITLE_PATTERN, _LEADERSHIP_HEADING_PATTERN,
                    ),
                    "title_count": title_count,
                    "rule_type":   page_type,
                })
                logger.info(f"    [candidates] appended → total={len(candidates)}")
                continue   # do NOT descend — list pages lead to profiles

            # not-list → explore outgoing links
            if depth >= max_depth:
                continue

            raw_links = (
                result.links.get("internal", [])
                if isinstance(result.links, dict) else []
            )

            strong_links: list[tuple[str, str]] = []
            weak_links:   list[tuple[str, str]] = []
            lang_links:   list[tuple[str, str]] = []
            all_links:    list[tuple[str, str]] = []
            seen_next: set[str] = set()

            for link in raw_links:
                href = link.get("href", "") if isinstance(link, dict) else str(link)
                text = (link.get("text", "") if isinstance(link, dict) else "")
                abs_href = _make_absolute(href, url)
                if not abs_href:
                    continue
                if not _same_domain(abs_href, homepage):
                    continue
                n = _normalize(abs_href)
                if n in visited or n in seen_next:
                    continue
                if any(p in urlparse(abs_href).path.lower() for p in EXCLUDE_PATTERNS):
                    continue
                seen_next.add(n)
                text_stripped = text.strip()
                all_links.append((abs_href, text_stripped))
                href_lower = abs_href.lower()
                if any(sig in href_lower for sig in STRONG_URL_SIGNALS):
                    strong_links.append((abs_href, text_stripped))
                elif any(sig in href_lower for sig in WEAK_URL_SIGNALS):
                    weak_links.append((abs_href, text_stripped))
                if (any(sig in href_lower for sig in LANG_HREF_SIGNALS)
                        or text_stripped.lower() in LANG_TEXT_SIGNALS):
                    lang_links.append((abs_href, text_stripped))

            # Normal path: strong + weak[:5].
            # Fallback when strong bucket is empty: all non-excluded links,
            # letting LLM pruning pick the most promising ones.
            if strong_links:
                layer_candidates = strong_links + weak_links[:5]
            else:
                layer_candidates = all_links

            # Step 2: LLM link pruning when too many candidates
            if len(layer_candidates) > BFS_LLM_LINK_THRESHOLD:
                logger.info(
                    f"    [Step 2] {len(layer_candidates)} links → LLM selecting "
                    f"{BFS_LLM_LINK_MAX_SELECT}..."
                )
                selected_urls = _llm_select_links(layer_candidates, company_name, tokens)
                layer_candidates = [(u, t) for u, t in layer_candidates if u in selected_urls]

            # L0 only: merge language-switch links unconditionally so they survive
            # both strong-bucket filtering and LLM pruning.
            if depth == 0 and lang_links:
                included = {u for u, _ in layer_candidates}
                layer_candidates += [(u, t) for u, t in lang_links if u not in included]

            logger.info(
                f"    [links] raw={len(raw_links)} all={len(all_links)} "
                f"strong={len(strong_links)} weak={len(weak_links)} "
                f"lang={len(lang_links)} layer_candidates={len(layer_candidates)}"
            )
            logger.info(f"    → queuing {len(layer_candidates)} link(s)")
            for next_url, _ in layer_candidates:
                queue.append((next_url, depth + 1))

        # ── Step 3: LLM page classification ───────────────────────────────
        if not candidates:
            logger.info("  [BFS] No candidates found after exploration")
            return "", [], None

        logger.info(
            f"  [Step 3] LLM classifying {len(candidates)} candidate page(s)..."
        )
        confirmed_urls = _llm_classify_pages(candidates, company_name, tokens)

        if not confirmed_urls:
            logger.warning("  [BFS] LLM confirmed no list pages — failing")
            return "", [], {"reason": "no_executives_extracted", "note": "LLM found no confirmed leadership list pages among BFS candidates."}

        logger.info(f"  [BFS] Confirmed list pages: {confirmed_urls}")

        # ── Phase 2: Rich re-crawl with SHOW_ALL_TABS_JS ──────────────────
        logger.info(f"  [Phase 2] Re-crawling {len(confirmed_urls)} page(s) with tab JS...")
        found_content: list[str] = []
        per_page_chars = max(
            EXTRACTOR_PAGE_MIN_CHARS,
            EXTRACTOR_MAX_TOTAL_CHARS // len(confirmed_urls),
        )

        for url in confirmed_urls:
            logger.info(f"  [ReCrawl] {url}")
            try:
                result = await asyncio.wait_for(
                    crawler.arun(url, config=_LEADERSHIP_CRAWL_CFG), timeout=CRAWL_HARD_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                logger.info(f"    ✗ hard timeout after {CRAWL_HARD_TIMEOUT_S}s — crawl4ai never returned")
                continue
            except Exception as e:
                logger.info(f"    ✗ {e}")
                continue
            if not result.success:
                logger.info(
                    f"    ✗ fetch failed | status={getattr(result, 'status_code', '?')} "
                    f"| error={getattr(result, 'error_message', '?')!r}"
                )
                continue
            markdown = (result.markdown or result.cleaned_html or "").strip()
            if markdown:
                trimmed = extract_preview(
                    markdown, per_page_chars,
                    _LEADERSHIP_TITLE_PATTERN, _LEADERSHIP_HEADING_PATTERN,
                )
                logger.info(
                    f"    ✓ {len(markdown)} chars (with tabs exposed) → "
                    f"{len(trimmed)} chars kept for extraction"
                )
                found_content.append(f"--- Source: {url} ---\n{trimmed}")

    combined = "\n\n".join(found_content)
    logger.info(
        f"  [BFS] Done — {len(found_content)} page(s), {len(combined)} chars total"
    )
    return combined, confirmed_urls, None
