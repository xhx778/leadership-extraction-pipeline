# Leadership Extraction Pipeline

## 1. Overview

This project automates the extraction of senior leadership information (board members, C-suite, VP-level, etc.) from the official corporate websites of a target company list. Given a CSV of company names, it first uses a **Scout** node to locate each company's official homepage via web search (Gemini grounding or Tavily), then runs a **BFS Navigator** to crawl the site and discover leadership listing pages, and finally passes the raw markdown to a **DeepSeek Extractor** to extract structured name/title pairs. An **Auditor** node filters the results by seniority tier. All results are persisted to a checkpoint file so runs can be interrupted and resumed.

---

## 2. Project Structure

```
leadership_extraction/
├── .env                        # Local secrets (git-ignored)
├── .env.example                # Template for .env — copy this first
├── .gitignore
├── requirements.txt
│
├── data/
│   ├── target_companies.example.csv  # Template — copy this to target_companies.csv
│   └── target_companies.csv          # Your input list (git-ignored): one "company_name" column
│
├── results/                   # All output files (git-ignored)
│   ├── checkpoint.jsonl       # Append-only audit log; one JSON line per company
│   ├── results_latest.json    # Canonical state of all processed companies
│   └── failures_latest.csv    # Companies that failed, for human review
│
└── src/
    ├── main.py                # Entry point: CLI args, checkpoint management, run loop
    ├── pipeline.py            # Orchestrator: wires Scout → BFS → Extractor → Auditor
    ├── schemas.py             # Pydantic models: Candidate, Executive, PipelineResult, ScoutResult
    ├── utils.py               # Shared helpers: load_prompt, deepseek_call, classify_result, extract_preview
    │
    ├── gemini_backend.py      # Scout search backend: Gemini grounding → Candidate list
    ├── tavily_backend.py      # Scout search backend: Tavily search → Candidate list
    ├── node_scout.py          # Node 1: cascades backends, calls DeepSeek to select official site
    ├── node_bfs_navigator.py  # Node 2: Crawl4AI BFS, identity gate, link pruning, page classification
    ├── node_extractor.py      # Node 3: DeepSeek extracts {name, title} pairs from markdown
    ├── node_auditor.py        # Node 4: filters by seniority tier, sets auditor_passed flag
    ├── config/
    │   ├── __init__.py        # Central config: API keys, model names, BFS tuning constants
    │   ├── bfs_patterns.yaml  # Title keywords and heading phrases for classify_page()
    │   ├── seniority_tiers.yaml # Seniority tier definitions (Board / C-suite / VP-level / etc.)
    │   ├── bfs_loader.py      # Loads bfs_patterns.yaml → compiled regex patterns
    │   └── seniority_loader.py # Loads seniority_tiers.yaml; implements classify_title() with Director rules
    │
    └── skills/                # LLM prompt templates (Markdown, loaded at runtime)
        ├── scout_search_gemini.md      # Gemini grounding search trigger prompt
        ├── scout_select_gemini.md      # DeepSeek official-site selector (Gemini backend)
        ├── scout_select_tavily.md      # DeepSeek official-site selector (Tavily backend)
        ├── identity_gate.md            # DeepSeek L0 identity verification
        ├── navigator_link_selector.md  # DeepSeek Step 2 link pruning
        ├── navigator_page_classifier.md # DeepSeek Step 3 page classification
        └── extractor.md                # DeepSeek executive extraction
```

---

## 3. Setup & Installation

**Python version:** 3.11 or above.

The floor is set by `pandas==3.0.3`, which declares `Requires-Python >=3.11`. The next-strictest pins — `Crawl4AI==0.8.6`, `google-genai==1.65.0`, `tldextract==5.3.1`, `python-dotenv==1.2.2` — all declare `>=3.10`; everything else in `requirements.txt` allows 3.9 or lower. So 3.10 is enough for the crawler and the LLM clients, and pandas alone is what pushes the requirement to 3.11.

**1. Install Python dependencies**

```bash
pip install -r requirements.txt
```

**2. Install Playwright browsers (required by Crawl4AI)**

Crawl4AI wraps Playwright for headless browsing. After installing the Python packages, run:

```bash
crawl4ai-setup
```

This installs the Chromium browser used by Crawl4AI. It is the canonical setup step for Crawl4AI 0.8.6 — `playwright install` alone is not sufficient.

**3. Configure API keys**

```bash
cp .env.example .env
```

Then open `.env` and fill in all three keys:

| Key | Used by |
|---|---|
| `GOOGLE_API_KEY` | Gemini grounding search (`gemini_backend.py`) |
| `DEEPSEEK_API_KEY` | All selection, identity, pruning, classification, and extraction calls |
| `TAVILY_API_KEY` | Tavily search backend (`tavily_backend.py`) |

At startup, `main.py` validates that the key for the configured `SCOUT_BACKEND` and `DEEPSEEK_API_KEY` are present; missing keys cause an immediate exit with an error message.

**4. Provide your input list**

```bash
cp data/target_companies.example.csv data/target_companies.csv
```

Then replace the placeholder rows with the companies you want to process — one
`company_name` column, one company per row, using each company's full legal name
(the Scout strips legal suffixes itself before searching). Like `.env`, your
`target_companies.csv` is git-ignored, so your list stays local; only the
`.example.csv` template is tracked.

---

## 4. How to Run

All commands are run from the project root, with `src/` on the Python path (or `cd src` first).

```bash
# Default: resume from checkpoint (skip done + permanent failures; retry transient)
python src/main.py

# Fresh run: back up existing checkpoint and process everything from scratch
python src/main.py --mode fresh

# Retry only transient failures (network errors, timeouts, JSON parse errors)
python src/main.py --mode rerun-failed

# Run a single company (for testing; ignores checkpoint mode)
python src/main.py --company "Example Holdings Ltd."

# Additional flags
python src/main.py --input path/to/companies.csv   # default: data/target_companies.csv
python src/main.py --output path/to/results/       # default: results/
python src/main.py --log DEBUG                     # default: INFO
```

### Checkpoint modes explained

| Mode | Behaviour |
|---|---|
| `resume` (default) | Loads `checkpoint.jsonl`; skips companies that already passed **or** have a permanent failure reason; retries companies with transient failures. Safe to run repeatedly. |
| `fresh` | Renames the existing checkpoint to `checkpoint_backup_YYYYMMDD_HHMM.jsonl`, then runs all companies from scratch. |
| `rerun-failed` | Loads checkpoint; builds a to-run list containing **only** companies whose `failure_reason` is **not** in `PERMANENT_FAILURE_REASONS`. Permanent failures (wrong entity, no website found, no executives, etc.) are skipped; transient failures (429, timeout, connection error, JSON parse error) are retried. |

**Permanent vs transient failure classification** (`src/utils.py`):

- **Permanent** reasons (never retried by `rerun-failed`): `false_url`, `identity_unconfirmed`, `all_filtered_out`, `no_executives_extracted`, `scout_failed_invalid_homepage`, `permanent_failure`, `other`
- **Transient** detection: failure note contains any of `"429"`, `"503"`, `"timeout"`, `"rate"`, `"connection"`, `"json"`, `"parse"` → classified as `transient_failure` and eligible for retry

### Regenerating results/failures from a checkpoint backup

`--mode fresh` only renames the old checkpoint to `checkpoint_backup_YYYYMMDD_HHMM.jsonl` before running from scratch — it does **not** also snapshot `results_latest.json`/`failures_latest.csv` for that backed-up state. Once the fresh run finishes, those two files reflect only the new run.

To reconstruct the results/failures pair for any checkpoint file (a backup, or any `.jsonl` you want to inspect) without re-running the pipeline, reuse `main.py`'s own helpers directly — this only reads and re-derives from the checkpoint, no API calls, seconds to run:

```bash
python -c "
import sys
sys.path.insert(0, 'src')
from main import load_checkpoint_dedup, write_results_latest, write_failures_latest

canonical = load_checkpoint_dedup('results/checkpoint_backup_YYYYMMDD_HHMM.jsonl')
write_results_latest(canonical, 'results/backup_snapshot')
write_failures_latest(canonical, 'results/backup_snapshot')
"
```

**Note:** `write_results_latest`/`write_failures_latest` always write to the fixed filenames `results_latest.json`/`failures_latest.csv` inside whatever `output_dir` you pass as the second argument — point that at a *new* directory (e.g. `results/backup_snapshot`, not `results/`) so this doesn't overwrite the live results from the current checkpoint.

---

## 5. Configuration Reference

All tuning constants live in **`src/config/__init__.py`** unless stated otherwise. Edit there; the value is imported by the relevant node on startup.

---

### 5.1 Scout backend

| Constant | Value | Location |
|---|---|---|
| `SCOUT_BACKEND` | `"gemini"` or `"tavily"` | `config/__init__.py` |
| `GEMINI_MODEL` | `"gemini-2.5-flash"` | `config/__init__.py` |
| `SCOUT_THINKING_BUDGET` | `256` (tokens) | `config/__init__.py` |
| `DEEPSEEK_MODEL` | `"deepseek-chat"` | `config/__init__.py` |

**Used by:** `node_scout.py → scout()`; backend modules are lazy-imported so only the active backend's API client is initialised.

**Cascade logic:** Scout runs at most 2 attempts — one per backend. It starts with `SCOUT_BACKEND`, then falls back to the other backend (`gemini ↔ tavily`) if the first produces no candidates, DeepSeek returns null, proxy resolution fails (Gemini only), or the homepage is inaccessible. On first success the result is returned immediately.

**How to change:** Set `SCOUT_BACKEND = "tavily"` to prefer Tavily as primary. The DeepSeek selection prompt switches automatically (`scout_select_{backend}.md`).

> *Why two backends:* Gemini grounding returns proxy URLs that must be resolved; Tavily returns real URLs directly. Cascading means a quota exhaustion or empty result on one backend does not fail the company.

**Both backends now fail the same way (important — this was a real gap):** `tavily_backend.py → search_tavily()` always wrapped its API call in `try/except`, returning `[]` on any error (network drop, rate limit, etc.) so `node_scout.py`'s `if not candidates: continue` cascade could pick it up transparently. `gemini_backend.py → search_gemini()` originally had **no such try/except** around its `_client.models.generate_content(...)` call — a transient Gemini-side error (observed in practice: `503 UNAVAILABLE`) propagated as an uncaught exception straight out of `scout()`, past `pipeline.py`, and killed the company's entire pipeline run immediately, without ever getting a chance to cascade to Tavily within the same run. The documented "on any failure → cascade to other backend" behavior was only half-true. `search_gemini()` now catches exceptions the same way `search_tavily()` does (logs a warning, returns `[]`), so a transient Gemini API error correctly falls back to Tavily in the same run instead of failing the company outright (it would otherwise still get retried on the next `resume`, since it's classified `transient_failure` — but as a full re-run rather than an in-run fallback).

---

### 5.2 Tavily search parameters

Defined in `src/tavily_backend.py → search_tavily()`:

| Parameter | Value | Notes |
|---|---|---|
| `query` | `"{company_name} official website"` | Company legal suffixes are stripped before the search (see `_clean_name` in `node_scout.py`) |
| `search_depth` | `"basic"` | Tavily's lighter search tier; sufficient for homepage discovery |
| `max_results` | `8` | Raw results before eTLD+1 deduplication |
| `exclude_domains` | Hardcoded list in `tavily_backend.py` | Pre-filters LinkedIn, Bloomberg, ACRA, Wikipedia, and ~25 other noise domains at the API level |

A post-filter (`_SOCIAL_KEYWORDS`) also drops social subdomains (e.g. `sg.linkedin.com`) that bypass `exclude_domains` when only the apex domain is listed.

Deduplication key: **eTLD+1** (e.g. `examplecap.sg`). When multiple results share the same domain, the URL closest to the root (shortest path depth) is kept, and snippets are merged sentence-level up to 600 chars.

**How to change:** To add more noise domains, append to `_EXCLUDE_DOMAINS` in `tavily_backend.py`. To increase result depth, change `search_depth` to `"advanced"` (higher cost).

> *Why `search_depth="basic"`:* Homepage discovery does not require deep content indexing; "basic" keeps API cost low while returning sufficient candidates.

---

### 5.3 BFS URL signal vocabularies

All three sets are defined in `src/config/__init__.py`. They control which links the BFS crawler follows at each depth.

**`STRONG_URL_SIGNALS`** (set of 18 strings):
Keywords that, when present in a link's absolute URL, indicate it is very likely a leadership listing page (e.g. `"leadership"`, `"board-of-directors"`, `"management-team"`, `"/team"`, `"/people"`, `"directors"`, `"committee"`).

**Used by:** `node_bfs_navigator.py` — (a) in `classify_page()` to mark a page as `"list"` when `has_strong_url=True` and `title_count >= 2`; (b) in link categorisation to populate `strong_links`.

**`WEAK_URL_SIGNALS`** (set of 6 strings):
Secondary keywords indicating the link may be an intermediate navigation page leading to leadership content (e.g. `"about"`, `"about-us"`, `"corporate"`, `"company"`).

**Used by:** `node_bfs_navigator.py` — forms `weak_links`; up to 5 weak links per layer are queued alongside all strong links.

**Fallback:** When `strong_links` is empty for a layer, **all** non-excluded links (`all_links`) are queued, then LLM pruning picks the best `BFS_LLM_LINK_MAX_SELECT` from that wider set.

**`LANG_HREF_SIGNALS` / `LANG_TEXT_SIGNALS`** (sets):
URL path segments (`/en/`, `/en-us/`, `/en-gb/`, `/english/`) and link text values (`en`, `en-gb`, etc.) that indicate an English-language version of the page. At **L0 (homepage) only**, language-switch links matching these signals are unconditionally merged into `layer_candidates` so the BFS can reach the English version of multilingual sites before the strong-bucket filter applies.

**How to change:** Add or remove strings from any of these sets in `config/__init__.py`. Extending `STRONG_URL_SIGNALS` with new patterns increases recall at the cost of more pages crawled per company.

> *Why separate strong/weak:* Strong signals near-guarantee a leadership page exists at that URL; weak signals need further descent. The two-tier split avoids crawling unrelated subtrees while keeping an escape hatch via `all_links` when the site uses non-standard URL structures.

---

### 5.4 `EXCLUDE_PATTERNS`

A set of URL path substrings that are hard-filtered before any link is queued. Applied to `urlparse(href).path.lower()`.

Covers: auth flows (`login`, `signin`, `register`), commerce (`cart`, `shop`, `product`, `pricing`), content (`news`, `blog`, `press`, `article`, `event`), HR (`careers`, `jobs`, `vacancy`), utility (`contact`, `faq`, `help`, `support`), legal (`privacy`, `terms`, `cookie`), social platforms, broken link indicators (`404`, `javascript:`, `#`), documents (`pdf`), and ESG/sustainability sub-sites (`esg`, `sustainability`).

> *Why `esg`/`sustainability` are excluded:* BFS was repeatedly descending into ESG/sustainability sections looking for leadership content. These sections almost never contain a true multi-person leadership list (at most a passing mention of an ESG committee), so excluding them cuts wasted crawls and DeepSeek calls with negligible recall loss.

**Used by:** `node_bfs_navigator.py` in the link-collection loop — any link whose path matches any pattern is dropped before being added to any bucket.

**How to change:** Add patterns to `EXCLUDE_PATTERNS` in `config/__init__.py` to suppress additional URL subtrees globally.

> *Why hard-filter before LLM pruning:* Reduces the token cost of Step 2 LLM calls by pre-cleaning obvious noise; the LLM sees only genuinely ambiguous candidates.

---

### 5.5 BFS LLM assistance thresholds

| Constant | Default | What it controls |
|---|---|---|
| `BFS_LLM_LINK_THRESHOLD` | `10` | If a BFS layer produces more than this many candidate links, Step 2 LLM pruning is triggered |
| `BFS_LLM_LINK_MAX_SELECT` | `5` | Maximum number of links the LLM picks per layer |
| `BFS_CONTENT_PREVIEW_CHARS` | `10000` | Characters of page content sent per candidate in Step 3 LLM classification |
| `BFS_MAX_DEPTH` | `3` | Maximum BFS depth from homepage |

**Used by:** `node_bfs_navigator.py → _llm_select_links()` (Step 2) and `_llm_classify_pages()` (Step 3).

**How to change:** Lower `BFS_LLM_LINK_THRESHOLD` to call the LLM more aggressively (better pruning, higher cost). Raise `BFS_MAX_DEPTH` to crawl deeper sites (higher cost, more false positives). Reduce `BFS_CONTENT_PREVIEW_CHARS` to cut Step 3 token usage — but see 5.5.1 first, since this value was deliberately raised from 600→3000→10000 over the course of fixing real classification failures.

> *Why threshold=10:* Below 10 candidates the signal-based bucketing is usually decisive enough without LLM cost; above 10 the layer is ambiguous and LLM pruning prevents exponential crawl growth.

---

#### 5.5.1 `extract_preview()` — how Step 3's content preview is chosen

**Function:** `utils.py → extract_preview(text, limit, pattern, heading_pattern=None, heading_proximity=HEADING_PROXIMITY_CHARS)`, shared by the Step 3 candidate preview ([node_bfs_navigator.py](src/node_bfs_navigator.py) `candidates.append(...)`) and the Phase 2 → Extractor page trimming (5.7.1). `HEADING_PROXIMITY_CHARS` (default `600`) lives in `config/__init__.py`.

Naively taking `markdown[:limit]` breaks on real sites in two distinct ways this constant/function combination was built to survive:

1. **Real content sits far past the page top.** Long "About Us" pages can carry 20k+ chars of boilerplate (nav, history, awards) before the actual leadership section begins. A first-N-chars slice never reaches it.
2. **A single verbose bio can locally out-density the real roster.** `_LEADERSHIP_TITLE_PATTERN` counts every keyword hit (`director`, `chairman`, etc.). One board member's bio that lists ten *other* companies they also direct can pack more title-keyword hits into a small span than the actual multi-person roster does (each person there is mentioned once). A pure "densest-window" heuristic can lock onto that single bio instead of the real list.

`extract_preview()` resolves both with a two-tier strategy:

- **Priority 1 — heading anchor:** if `heading_pattern` (`_LEADERSHIP_HEADING_PATTERN`, e.g. "Board of Directors", "Leadership Team") matches, **and** a real title-keyword hit (`pattern`) follows within `heading_proximity` (`HEADING_PROXIMITY_CHARS`, 600 chars) — confirming it's a real section header, not just a nav link repeating the same words — start the window there.
- **Priority 2 — density window (fallback):** when no qualifying heading is found, fall back to the original approach — a sliding window over all `pattern` hit positions, returning the `limit`-char span containing the most hits.

The heading-proximity check specifically guards against nav-menu bloat (Section 5.7.1) re-triggering priority 1 on a link labelled "Board of Directors" with no real bio content nearby.

**Related constant — `CLASSIFY_HEADING_SEARCH_CHARS`** (default `20000`, `config/__init__.py`): `classify_page()` itself does a separate, cheaper heading check — `_LEADERSHIP_HEADING_PATTERN.search(markdown[:CLASSIFY_HEADING_SEARCH_CHARS])` — to help decide `"list"` vs `"not-list"` during Step 1. This just caps how far into a page that check bothers looking; it's independent of `extract_preview()`'s own heading-anchor logic above (which runs later, only on pages already classified `"list"`, and searches the full text).

> *Why `BFS_CONTENT_PREVIEW_CHARS` grew to 10000:* Even once the window is anchored at the right heading, a single verbose bio can still run 2,000–3,000 chars. The Step 3 prompt requires seeing ≥3 distinct people before it will confirm a page as a real list; at 3,000 chars the window often only fit one complete bio plus a fragment of the next, so the LLM correctly (from its point of view) rejected genuine leadership pages as "looks like a single profile". 10,000 chars comfortably fits 3–5 people even at ~2,500 chars/bio.

---

### 5.6 Identity gate threshold

| Constant | Default | What it controls |
|---|---|---|
| `IDENTITY_GATE_EMPTY_THRESHOLD` | `200` chars | Homepage markdown shorter than this is classified as `empty_page` (transient failure) without calling DeepSeek |

**Used by:** `node_bfs_navigator.py → run()` at depth 0, before calling `verify_identity()`.

**How to change:** Raise this if parked domains with minimal content should also be detected as empty; lower it if some real homepages legitimately render very little text.

> *Why 200 chars:* A functional homepage renders hundreds of characters of markdown; 200 chars is well below any real site's minimum while catching blank pages and parking stubs.

---

### 5.7 Crawl4AI timing

| Constant | Default | What it controls |
|---|---|---|
| `CRAWL_PAGE_TIMEOUT_MS` | `30000` ms | Maximum time to wait for a page to load (crawl4ai's own internal timeout) |
| `CRAWL_DELAY_BEFORE_RETURN_S` | `1.5` s | Settling delay after `wait_for="body"` before capturing HTML |
| `CRAWL_HARD_TIMEOUT_S` | `60` s | **Asyncio-level backstop** — see below |

**Used by:** `node_bfs_navigator.py` — both `_CRAWL_CFG` (BFS discovery, no JS) and `_LEADERSHIP_CRAWL_CFG` (Phase 2 re-crawl with `SHOW_ALL_TABS_JS`).

**How to change:** Increase `CRAWL_PAGE_TIMEOUT_MS` for slow corporate sites; increase `CRAWL_DELAY_BEFORE_RETURN_S` for sites that load leadership content via lazy JS.

**`CRAWL_HARD_TIMEOUT_S` — why a second timeout on top of `CRAWL_PAGE_TIMEOUT_MS`:** In practice, some pages caused the pipeline to hang indefinitely well past `CRAWL_PAGE_TIMEOUT_MS` (30s) with no error ever surfacing — crawl4ai's internal timeout did not reliably fire for every hang mode (e.g. a stuck CDP/browser-communication channel isn't bound by the page-navigation timeout). Both `crawler.arun(...)` call sites (Step 1 discovery and Phase 2 re-crawl) are now wrapped in `asyncio.wait_for(..., timeout=CRAWL_HARD_TIMEOUT_S)`. On expiry this raises `asyncio.TimeoutError`, which is caught, logged as `✗ hard timeout after 60s — crawl4ai never returned`, and the loop moves on to the next URL — regardless of what crawl4ai was doing internally. This is a backstop, not a replacement for `CRAWL_PAGE_TIMEOUT_MS`; it should rarely fire in normal operation.

Both `crawler.arun()` failure paths (`result.success == False`, hard timeout, or any other exception) now log `status_code` and `error_message` where available, so a stuck/failed re-crawl can be diagnosed from the log alone without re-running a debug script.

---

### 5.7.1 Nav/menu noise stripping and dynamic per-page Extractor budget

**Problem this fixes:** Some corporate sites render their entire site navigation (mega-menus, per-language/per-property submenus) into the DOM regardless of CSS visibility. Crawl4AI's markdown conversion doesn't respect `display:none`, so a page can balloon to **millions of characters** of pure nav-link text with the real leadership content reduced to a tiny tail fraction. This was discovered on a REIT site where a single "Board of Directors" page produced 7.5M+ characters of markdown, ~99.97% of which was repeated navigation.

**Fix — `excluded_tags` (Phase 2 only):**

```python
_CHROME_TAGS = ["nav", "header", "footer"]

_LEADERSHIP_CRAWL_CFG = CrawlerRunConfig(
    js_code=SHOW_ALL_TABS_JS,
    ...,
    excluded_tags=_CHROME_TAGS,
)
```

`excluded_tags` tells crawl4ai to drop these DOM subtrees entirely before generating markdown. **Deliberately applied only to `_LEADERSHIP_CRAWL_CFG` (Phase 2 rich re-crawl), never to `_CRAWL_CFG` (Step 1 BFS discovery)** — Step 1 needs `result.links`, which is derived from the same DOM; excluding `<nav>` there was tried and measured to cut a homepage's discovered links from 37 to 7, breaking BFS discovery of the very pages Phase 2 needs to re-crawl. Nav text left in Step 1's markdown is a (much smaller, since the exclusion problem is length not link count at that stage) tolerable cost, handled instead by `extract_preview()`'s heading-anchor logic (5.5.1).

**Fix — `_strip_chrome_text()` for Step 1 page classification:**

`excluded_tags` cannot be used in `_CRAWL_CFG` (Step 1) because that would also strip `<nav>` links from `result.links`, breaking BFS discovery. Instead, `node_bfs_navigator.py → _strip_chrome_text()` post-processes the already-fetched `cleaned_html` with lxml: it removes `<nav>`, `<header>`, `<footer>`, and any `<div>`/`<section>` whose class tokens match `{"header", "footer", "nav", "navbar", "navigation"}` (exact token match — avoids removing content components like "CardHeader" or "TeamMemberHeader"). It also un-glues `"NameTitle"` CamelCase runs (e.g. `"Jane DoeChairman"`) that markdown conversion produces when a name and title are in adjacent inline DOM nodes with no separating whitespace — without this, `\b`-anchored title regexes never match the glued title half, so real leadership grids undercount to `"not-list"`. The resulting plain text feeds `_count_titles()` and `classify_page()` only — `result.links` (used for link discovery) is extracted by crawl4ai from the full un-stripped DOM, so link discovery is unaffected.

**Not all nav bloat lives inside semantic `<nav>` tags.** Sites that build custom menus with plain `<div>`s (common on non-semantic/legacy markup) may bypass `_strip_chrome_text()`'s class-token filter. This class of bloat is instead handled by `extract_preview()`'s density-window/heading-anchor logic, which locates the real content regardless of where the noise sits.

**Fix — dynamic per-page budget for the Extractor:**

| Constant | Default | What it controls |
|---|---|---|
| `EXTRACTOR_PAGE_MIN_CHARS` | `15000` | Floor per confirmed page, even when many pages are confirmed |
| `EXTRACTOR_MAX_TOTAL_CHARS` | `100000` | Total budget, split across confirmed pages; also the Extractor's final safety cap |

In the Phase 2 loop, each confirmed URL is trimmed independently via `extract_preview()` before being joined:

```python
per_page_chars = max(EXTRACTOR_PAGE_MIN_CHARS, EXTRACTOR_MAX_TOTAL_CHARS // len(confirmed_urls))
```

This replaces the old design where the *combined* markdown of all confirmed pages was truncated to a single flat 40,000-char slice in `node_extractor.py` (see removed limitation in 8.7). That design had two failure modes, both observed on real companies:
- With 2 confirmed pages, the first page's content (however long) always consumed the full 40,000 chars, so the second page's content was **never seen by the LLM at all**.
- With 1 confirmed page containing many long bios (e.g. a board of 9 with detailed biographies), a flat cap could cut off the last few executives even though the page had no nav bloat whatsoever.

Splitting the budget **per confirmed page** (with a floor, so a handful of pages don't each get starved) means every confirmed page gets a fair, guaranteed share regardless of how many pages Step 3 confirmed or how long any single page's content is.

`node_extractor.py` still applies `raw_content[:EXTRACTOR_MAX_TOTAL_CHARS]` as a final safety cap on the joined result (defends against an unexpectedly large number of confirmed pages), but with the per-page trimming in place this should rarely actually cut anything.

---

### 5.8 Seniority tier classification

**Files:** `src/config/seniority_tiers.yaml` (tier definitions) and `src/config/seniority_loader.py` (loading + `classify_title()`).

**Read by:** `node_extractor.py` (assigns `seniority_tier` to each extracted executive) and `node_auditor.py` (filters to keep only records with a recognised tier).

**Five tiers** (mutually exclusive):

| Tier | Examples | How classified |
|---|---|---|
| `Board` | Chairman, Board Member, Non-Executive, standalone "Director" | YAML patterns + Director Rule 1 + Rule 4 |
| `C-suite` | CEO, CFO, COO, President, General Counsel, Managing Director | YAML patterns + Director Rule 2 |
| `Partner/Advisor` | Managing Partner, General Partner, Principal | YAML patterns |
| `VP-level` | VP, SVP, Head of, GM, Senior Director, Senior Manager, Financial Controller, "MD, X", "Group Head" | YAML patterns + Director Rule 3 + `_classify_head()` + `_classify_md()` |
| `Director-level` | Director of X, Director, X | Director Rules 5–6 |

**`classify_title()` call order** (`seniority_loader.py`; programmatic rules take priority over YAML):

1. `_classify_director()` — Director six-rule logic (see below)
2. `_classify_head()` — "Head, X" / "Head of X" / "Group Head" → **VP-level**
3. `_classify_md()` — "MD, X" / "MD & Head, X" (abbreviated MD) → **VP-level**
4. YAML substring match (longest pattern first)

**Director six-rule logic** (`_classify_director()`):

1. Contains board governance modifier (`non-executive`, `independent`, `alternate`, `executive director`, etc.) → **Board** — checked **before** the "director" gate, so modifier-only titles like "Non-Executive and Non-Independent" (where "Director" was dropped by extraction) still resolve correctly
2. *(if "director" not in title → return None, defer to steps 2–4 above)*
3. Contains `"managing director"` → **C-suite**
4. Contains `"senior director"` → **VP-level**
5. Stripped title equals `"director"` exactly → **Board**
6. Starts with `"director of "` / `"director,"` / `"director -"` etc. → **Director-level**
7. All other Director variants → **Director-level**

**`_classify_head()` logic:** Matches titles where "Head" appears in a leading-position pattern with a separator — `"head of X"`, `"head, X"`, `"head - X"` — or where the title starts with `"group "` and contains `\bhead\b` (e.g. "Group Technical Head", "Group Head, Shipping"). All → **VP-level**. Uses the raw (pre-`_normalize`) lowercased title so that "," and "-" separators are not collapsed to spaces, which would make "Head, Group X" indistinguishable from "Team Head" or "Head Teller".

**`_classify_md()` logic:** Matches titles starting with `MD,` or `MD &` (case-sensitive, anchored). "MD" as an abbreviation in banks/asset managers means a senior divisional rank (one company can have a dozen "MD, Group IT" titles) — unlike the spelled-out "Managing Director" which maps to C-suite, the abbreviation → **VP-level**.

**How to change:** Edit `seniority_tiers.yaml` to add/remove patterns for existing tiers. Do **not** add `"director"` or `"head"` to YAML patterns — these are always routed through the programmatic logic first. To change Director/Head/MD classification rules, edit `_classify_director()`, `_classify_head()`, or `_classify_md()` in `seniority_loader.py`.

> *Why programmatic rules for Director/Head/MD:* All three title words span multiple seniority levels depending on modifier words and positional syntax that simple substring matching cannot resolve. Rule ordering makes the classification deterministic.

---

### 5.9 Checkpoint file paths and fields

| File | Path | Format |
|---|---|---|
| Checkpoint | `results/checkpoint.jsonl` | Append-only; one JSON object per line |
| Results | `results/results_latest.json` | JSON array of all `PipelineResult` objects |
| Failures | `results/failures_latest.csv` | CSV of companies where `auditor_passed=false` |

**Checkpoint entry fields** (outer wrapper):

| Field | Description |
|---|---|
| `timestamp` | ISO-8601 timestamp of when the entry was written |
| `company` | Company name string |
| `status` | `"passed"` or `"failed"` |
| `exec_count` | Number of executives extracted |
| `result` | Full `PipelineResult` object (see `schemas.py`) |

**Key `PipelineResult` fields for downstream use:**

| Field | Type | Description |
|---|---|---|
| `failure_reason` | `str \| null` | One of the permanent/transient reason codes (see Section 4); null on success |
| `auditor_passed` | `bool` | True only when executives were extracted and passed the auditor filter |
| `official_website` | `str \| null` | Homepage root URL found by Scout |
| `bfs_urls` | `list[str]` | Leadership page URLs confirmed by Step 3 LLM |
| `executives` | `list[Executive]` | Extracted records with `name`, `title`, `seniority_tier` |
| `extraction_method` | `str` | Derived from `SCOUT_BACKEND` + pipeline name (e.g. `"gemini_scout_bfs_deepseek"`) |
| `gemini_in/out/think` | `int` | Gemini token usage for this company |
| `deepseek_in/out` | `int` | DeepSeek token usage |
| `tavily_calls` | `int` | Number of Tavily API calls |
| `elapsed_s` | `float` | Wall-clock seconds for this company |

**Dedup rule** when loading checkpoint (`load_checkpoint_dedup`): for companies with multiple entries, prefer entries that have executives; among ties, keep the most recent by timestamp.

---

### 5.10 Language handling

**L0 language entry** (`node_bfs_navigator.py`, link collection at `depth == 0`):

At the homepage level only, links matching `LANG_HREF_SIGNALS` (URL path) or `LANG_TEXT_SIGNALS` (link text) are appended unconditionally to `layer_candidates` **after** Step 2 LLM pruning completes, bypassing both the strong/weak bucket filter and LLM pruning. This ensures language-switch links are never discarded — neither by strong-bucket filtering nor by the LLM — so the BFS can reach the English version of multilingual sites.

**Identity gate English preference** (`skills/identity_gate.md`):

DeepSeek is instructed to judge entity match using the **English company name** when both English and non-English names appear in the identity zones. Non-English names are only used as a fallback when no English name is present.

**Scout selection English preference** (`skills/scout_select_gemini.md` and `scout_select_tavily.md`):

When multiple candidates appear to be the same company's site in different languages, DeepSeek is instructed to prefer the English-language version (applied as a tiebreaker, not a disqualifier).

**Limitation:** `classify_page()` uses English keyword regex from `bfs_patterns.yaml`. Non-English leadership pages that cannot be reached via the L0 language-switch mechanism will not be detected. See [Section 8](#8-known-limitations--future-work).

---

## 6. Skills / Prompts Reference

All prompt files live in `src/skills/`. They are plain Markdown with `# comment lines` at the top (stripped at load time by `load_prompt()` in `src/utils.py`) and `{placeholder}` variables filled in at call time.

---

### `scout_search_gemini.md`

| | |
|---|---|
| **Model** | Gemini (`gemini-2.5-flash`) |
| **Node / function** | `gemini_backend.py → search_gemini()` |
| **Input** | `{company_name}` |
| **Output** | Not parsed — this is the search trigger prompt sent to Gemini. Gemini's text response is **discarded**; only grounding metadata (chunk titles + support segment texts) is extracted to build the candidate list. |
| **Key constraint** | A single natural-language search instruction; kept minimal so Gemini's grounding mechanism selects the search queries autonomously. |

> *Why the text response is discarded:* An earlier design let Gemini free-text the official URL directly. It would hallucinate domains that never appeared in the grounding citations (e.g. inventing `examplecapitalinvestments.com` for a company whose real site was at a shorter, unrelated domain). We now discard the free-text answer entirely and build candidates only from grounding metadata, so every returned URL traces back to a real search result.

---

### `scout_select_gemini.md`

| | |
|---|---|
| **Model** | DeepSeek (`deepseek-chat`) |
| **Node / function** | `node_scout.py → deepseek_select_official()`, when `backend="gemini"` |
| **Input** | `{company}` (cleaned name), `{candidates_block}` (indexed list of `Title` + `Info/snippet` — URL is **never shown**) |
| **Output** | JSON `{"reason": "...", "official_index": <int or null>}` |
| **Key constraint** | DeepSeek must select the company's own site, excluding directories, registries, social platforms, Wikipedia, PDFs, and stock filings. A "reference/referent" rule prevents selecting a page that merely *mentions* the company's URL. The Gemini backend's `Title` field is the **source domain** filled in by the search engine, not a real page title — the prompt explicitly notes this. |

> *Why Title = source domain for Gemini:* Gemini grounding chunks carry the site/company name as title, not the HTML page title; the prompt is calibrated to this format so DeepSeek does not mis-weight the domain string.

> *Why the URL is never shown to DeepSeek:* Domain spelling is a source of same-name-different-entity errors — a US `examplecapital.com` reads as a closer match to "Example Capital Investments" than the correct Singapore `examplecap.sg` does. Feeding only title + snippet forces the decision onto content (business, location), not URL string similarity.

---

### `scout_select_tavily.md`

| | |
|---|---|
| **Model** | DeepSeek (`deepseek-chat`) |
| **Node / function** | `node_scout.py → deepseek_select_official()`, when `backend="tavily"` |
| **Input** | `{company}`, `{candidates_block}` (same format — URL never shown) |
| **Output** | JSON `{"reason": "...", "official_index": <int or null>}` |
| **Key constraint** | Identical exclusion rules as the Gemini variant, but the `Title` field is the real HTML page title (not a domain). The prompt is adjusted accordingly — DeepSeek can use title evidence together with the snippet. |

> *Why two separate selection prompts:* Gemini and Tavily produce structurally different metadata (domain-as-title vs. real page title); unifying them into one prompt would require awkward conditional instructions or sacrifice precision.

---

### `identity_gate.md`

| | |
|---|---|
| **Model** | DeepSeek (`deepseek-chat`) |
| **Node / function** | `node_bfs_navigator.py → verify_identity()`, called at BFS depth 0 after the homepage is successfully crawled |
| **Input** | `{company}` (full legal name), `{identity_text}` (tagged zones: `[TITLE]`, `[FOOTER_COPYRIGHT]`, `[ABOUT_CONTACT_ANCHORS]`, `[PAGE_TOP]`, `[PAGE_TAIL]`) |
| **Output** | JSON `{"verdict": "YES"/"NO"/"UNSURE", "reason": "...", "matched_entity": "..."}` |
| **Key constraint** | Three verdicts: `YES` → `"verified"` (BFS proceeds); `NO` → `"false_url"` (permanent failure); `UNSURE` → `"identity_unconfirmed"` (permanent failure, goes to `failures_latest.csv` for human review). A parent-company override allows `YES` when the site is the global group with the same core brand name. Judge is restricted to entity attribution only — page quality issues (broken links, garbled text) must not influence the verdict. |

> *Why UNSURE is permanent (not transient):* An UNSURE result means there is genuinely insufficient identity evidence on that homepage, not a network error; retrying would hit the same page with the same result.

> *Why the verdict is an LLM call, not regex:* Keyword regex fails both ways on generic company names — it produces false positives (an unrelated site whose domain merely shares a common word — e.g. one containing "invest" matching a company named "… Investments") and false negatives (a real official site whose domain is a short brand name that does not literally contain the registered legal name). Entity attribution is a semantic judgment, so it is delegated to DeepSeek.

---

### `navigator_link_selector.md`

| | |
|---|---|
| **Model** | DeepSeek (`deepseek-chat`) |
| **Node / function** | `node_bfs_navigator.py → _llm_select_links()` (Step 2), called per BFS layer when candidate count exceeds `BFS_LLM_LINK_THRESHOLD` |
| **Input** | `{company_name}`, `{count}` (total links), `{max_select}` (`BFS_LLM_LINK_MAX_SELECT`), `{links}` (numbered list of `index \| URL path (no domain) \| HTML anchor text` — the visible `<a>` text, truncated to 60 chars; full URL is never shown) |
| **Output** | JSON array of selected URL paths: `["path1", "path2", ...]` |
| **Key constraint** | Must prefer LIST pages (board, management team) over individual profiles, news, careers, or investor sub-pages. English-language URL patterns are preferred when equivalent URLs exist in multiple languages. On LLM error, falls back to the top `BFS_LLM_LINK_MAX_SELECT` candidates by original order. |

> *Why paths-only (not full URLs):* Passing only the path reduces prompt length and avoids leaking query parameters or session tokens. It also prevents the model from being misled by host domains that happen to contain exclude-signal words (e.g. a domain with "store" or "careers" in the name would look like a noise URL if the full URL were shown). The code remaps paths back to absolute URLs after the call.

> *Why anchor text is passed alongside the path:* Leadership pages sometimes live at semantically meaningless URLs (e.g. /collection/item2) that keyword matching on the path alone cannot recognise. The visible anchor text ("Management", "Our Team", or its non-English equivalent) is often the only signal that reveals what the link leads to. Paths (not full URLs) keep the prompt short and avoid leaking query parameters; the code remaps paths back to absolute URLs after the call.

---

### `navigator_page_classifier.md`

| | |
|---|---|
| **Model** | DeepSeek (`deepseek-chat`) |
| **Node / function** | `node_bfs_navigator.py → _llm_classify_pages()` (Step 3), called once after BFS exploration with all `"list"`-classified candidate pages |
| **Input** | `{company_name}`, `{count}` (pages), `{pages}` (numbered list of URL + title-keyword hit count + content preview) |
| **Output** | JSON `{"confirmed": [{"index": <int>, "reason": "..."}]}` |
| **Key constraint** | Must distinguish true multi-person leadership listings (≥ 3 people, structured directory) from false positives: single-person profiles, about-page narratives, or press releases that mention executives in passing. All confirmed pages are passed to Phase 2 re-crawl. Each returned `index` is validated against the candidate list (1 ≤ index ≤ count); out-of-range or non-integer indices are skipped individually rather than discarding the whole response. On **exception** (LLM/parse error, malformed JSON), falls back to all rule-classified `"list"` pages. If the LLM returns a valid but **empty** `confirmed` list (i.e. genuinely decided none qualify), the BFS returns a hard failure (`no_executives_extracted`) rather than falling back — an empty but valid response is a deliberate judgment that must be respected. |

> *Why a two-stage classify (regex then LLM):* `classify_page()` is intentionally high-recall (loose); Step 3 adds high-precision filtering without the cost of calling the LLM on every crawled page.

> *Why index instead of a free-text URL:* The prompt already numbers each candidate for DeepSeek to read (the `{pages}` list), so returning that same index costs nothing in context the model doesn't already have. Originally DeepSeek echoed the full URL back in its JSON output, and the code used that string directly — with no check that it matched one of the input candidates. A URL is long enough to get subtly mistyped (trailing slash, encoding, a language-path segment) without the code ever noticing; an out-of-range index fails a cheap bounds check instead. This brings Step 3 in line with the same real-URL-never-comes-from-model-free-text guarantee already used by Scout (`official_index`) and Step 2 (path lookup against the candidate list).

---

### `extractor.md`

| | |
|---|---|
| **Model** | DeepSeek (`deepseek-chat`) |
| **Node / function** | `node_extractor.py → run()` (Step A), called once per company on the merged Phase 2 content |
| **Input** | `{company_name}`, `{raw_content}` (merged leadership page markdown, per-page-trimmed by `extract_preview()` — see 5.7.1 — then capped at `EXTRACTOR_MAX_TOTAL_CHARS` = 100,000 chars as a final safety net) |
| **Output** | JSON array `[{"name": "...", "title": "..."}]` |
| **Key constraint** | Must return exact titles as written in the source (no normalisation); skip former/past officeholders; deduplicate to most senior title per person; return `[]` on no valid records. Seniority classification is done separately in Step B (Python `classify_title()`). |

> *Why title verbatim:* Normalising titles at extraction time would corrupt the input to `classify_title()`, which relies on exact phrasing to apply the Director six-rule logic correctly.

**Step A.5 — grounding check (`node_extractor.py → _is_grounded()`):** After parsing the LLM's JSON, every returned `name` is checked against `content_slice` (the exact text sent to the model) before being accepted. Honorifics (`Dr.`, `Mr.`, `Ms.`, `Mrs.`, `Prof.`) are stripped, then the record is kept only if **at least one token** of the remaining name is found (case-insensitive substring) in the source text; otherwise it's dropped with a `[ungrounded]` warning log.

> *Why this exists:* On a company page where the real leadership content was buried in noise (before the 5.7.1 fixes), DeepSeek returned 10 plausible, well-formatted executive records for a well-known public company — but 9 of the 10 names could not be found anywhere in the ~230,000 chars of actually-crawled source text. The model had filled in real-sounding names from its own training-data knowledge of the company rather than reporting extraction failure. This is a distinct risk from truncation/incompleteness: it produces confident-looking, fully-formed **fabricated** data rather than an obviously incomplete or empty result, so it doesn't surface as an error anywhere downstream. The grounding check is a cheap, general-purpose safety net against this failure mode — it does not depend on the upstream content-quality fixes in 5.5.1/5.7.1 being effective, so it stays in place as a backstop even if a future site trips a noise pattern those fixes don't anticipate.

---

## 7. Data Flow / Pipeline Logic

```
Input CSV (data/target_companies.csv)
│
├─ [Node 1] Scout (node_scout.py)
│   ├─ Primary backend search (Gemini grounding OR Tavily)
│   │     Gemini: scout_search_gemini.md prompt → grounding metadata → Candidate list
│   │     Tavily: "{company} official website" query → Candidate list
│   ├─ Candidate dedup (before DeepSeek sees the list)
│   │     Gemini: dedup by title (= source domain/host returned by grounding)
│   │     Tavily: dedup by URL host
│   ├─ DeepSeek selection (scout_select_{backend}.md)
│   │     Sees: Title + Snippet only (URL never shown)
│   │     Returns: official_index or null
│   ├─ URL resolution
│   │     Gemini: follow proxy redirect (vertexaisearch.cloud.google.com → real URL)
│   │     Tavily: URL already real
│   ├─ Homepage root trim (pure string, no HTTP)
│   ├─ Accessibility check (HTTP < 400 or 403 treated as accessible)
│   └─ On any failure → cascade to other backend (1 attempt each; 2 total)
│
├─ [Node 2] BFS Navigator (node_bfs_navigator.py)
│   ├─ URL redirect resolution (follow HTTP redirects to final homepage)
│   ├─ L0 Identity Gate
│   │     Crawl homepage (no JS) → extract identity zones → DeepSeek verify
│   │     YES → proceed | NO → false_url (permanent) | UNSURE → identity_unconfirmed (permanent)
│   ├─ Step 1: BFS exploration (Crawl4AI, headless, no JS, max_depth=3)
│   │     Per page: classify_page() → "list" (candidate) or "not-list" (follow links)
│   │     "list" pages are added to Step 3 candidates and NOT descended into
│   │     "not-list" pages → collect outgoing links → layer candidates:
│   │       strong_links non-empty → strong_links + weak_links[:5]
│   │       strong_links empty     → all_links  (full fallback; LLM pruning picks best)
│   ├─ Step 2: LLM link pruning (navigator_link_selector.md, per layer if > 10 links)
│   │       L0 only: lang_links merged in AFTER pruning (survive both bucket filter and LLM)
│   ├─ Step 3: LLM page confirmation (navigator_page_classifier.md, 1 call total)
│   │     Preview per candidate: extract_preview() — heading-anchor, else density-window (5.5.1)
│   └─ Phase 2: Rich re-crawl of confirmed URLs with SHOW_ALL_TABS_JS (exposes hidden tabs)
│         excluded_tags=[nav,header,footer] strips site-chrome bloat (5.7.1)
│         Each page trimmed via extract_preview() to a dynamic per-page share of the
│         Extractor's total budget before merging (5.7.1)
│
├─ [Node 3] Extractor (node_extractor.py)
│   ├─ Step A: DeepSeek extracts [{name, title}] from merged Phase 2 markdown
│   │           (per-page-trimmed, ≤ EXTRACTOR_MAX_TOTAL_CHARS = 100 000 chars total; 5.7.1)
│   ├─ Step A.5: _is_grounded() drops any returned name not found in the source text sent
│   │            to the LLM — guards against hallucinated executives (see extractor.md, §6)
│   └─ Step B: Python classify_title() assigns seniority_tier to each record
│
├─ [Node 4] Auditor (node_auditor.py) — pure Python, zero LLM calls
│   ├─ Layer 1: name format — drop if < 2 or > 7 tokens, or contains UI artifact text
│   ├─ Layer 2: seniority — drop if seniority_tier is None (not recognised by classify_title)
│   └─ Layer 3: org-title — drop organisational units (e.g. "Committee", "Secretariat")
│              unless the title also contains a role keyword (chairman, chief, head, etc.)
│
├─ Dedup (pipeline.py → _dedup_by_name())
│   └─ Case-insensitive full-name dedup; keeps first occurrence
│         (handles same person on Board page + Management page)
│
└─ Output
    ├─ results/checkpoint.jsonl  (append per company)
    ├─ results/results_latest.json
    └─ results/failures_latest.csv
```

For the full exclusion rules and name-matching logic behind the Scout selection step, see [skills/scout_select_gemini.md](src/skills/scout_select_gemini.md) and [skills/scout_select_tavily.md](src/skills/scout_select_tavily.md). For the identity gate verdict rules (YES/NO/UNSURE, parent-company override, English-name priority), see [skills/identity_gate.md](src/skills/identity_gate.md).

---

## 8. Known Limitations & Future Work

These are known boundaries of the current pipeline, arrived at deliberately after testing against a wide range of real company sites. The guiding principle throughout is "prefer not_found over dirty data": when a case cannot be handled confidently, it is routed to `failures_latest.csv` for human review rather than allowed to produce an incorrect result. The nature of company websites is a long tail — new edge cases will always appear — so the goal is not to handle every site, but to ensure unhandled sites fail cleanly rather than contaminate the results.

---

### 8.1 Non-English leadership pages (language ceiling of the classifier)

`classify_page()` relies on English keyword regex (`bfs_patterns.yaml`). A leadership page available only in Chinese/Malay/Japanese/etc. is classified `"not-list"` and never reaches the extractor → `no_executives_extracted`.

**Current mitigation:** L0 language-switch merging (`LANG_HREF_SIGNALS`/`LANG_TEXT_SIGNALS`) steers the BFS to an English sub-path when a bilingual site offers a static `<a>` language link. This covers the common `/en/`, `English`/`EN` cases (most HKEX-listed companies).

**Not covered:** English entry via flag icon with no text, `?lang=en` query switching with no anchor, or companies with no English version at all. These go to review.

**Observed concrete case — JS `href="javascript:void(0)"` language dropdown (an HKEX-listed issuer's IR site):** The site's language switcher renders real `<a class="dropdown-item">` elements for "EN"/"繁"/"简", but each has `href="javascript:void(0)"` — the actual navigation is wired to a JS click handler, not a static href. `/en/` never appears anywhere in the crawled DOM, so `LANG_HREF_SIGNALS` has nothing to match regardless of how it's tuned. Confirmed content links (e.g. `/sc/management.aspx`, `/sc/board.aspx`) do have real hrefs and are correctly discovered, but they're Simplified Chinese pages that `classify_page()`'s English-only regex will not recognise as a leadership list — and even if it did, extracting Chinese names/titles wouldn't survive `classify_title()` or the audit step downstream, both of which are English-pattern-driven. So adding Chinese keyword recognition to `classify_page()` alone would not fix this class of case; the real fix has to land the English URL itself.

**Considered but deferred fix:** On sites following this `/sc/`, `/tc/`, `/en/` language-as-URL-path-segment convention (common across HKEX-listed company IR sites), a confirmed strong-signal URL's language segment could be substituted (`/sc/management.aspx` → `/en/management.aspx`) and probed for accessibility as a candidate. Not implemented — current volume of this specific pattern is low; handling case-by-case for now. Revisit if it recurs often enough in `failures_latest.csv`.

**Future improvement:** Before descending into a non-list page's links, detect whether the page offers multiple language versions; if so, select the English version and continue BFS from that URL instead of the current-language one. This would extend language-switch handling beyond L0 to any depth, covering sites where the language selector appears on interior pages rather than the homepage.

---

### 8.2 Strong-signal filtering can skip semantically-named URLs

When `strong_links` is non-empty, the layer queues only `strong + weak[:5]`; a leadership page reachable only via a signal-less URL (e.g. `/collection/item2`) is dropped at that layer.

**Current mitigation:** When `strong_links` is empty at any layer (including L0), the `all_links` fallback kicks in and LLM pruning picks the best candidates by anchor text. At L0 specifically, `lang_links` are unconditionally merged into the layer candidates so language-switch links always survive strong-bucket filtering.

**Remaining gap:** A signal-less leadership entry buried deeper than L0, at a layer where a strong-signal link co-exists (suppressing the `all_links` fallback), can still be missed. Rare (key entries are usually on the homepage); accepted as a boundary.

---

### 8.3 Scout cannot resolve pure-brand-abbreviation sites without corroborating snippet

When a company's official site uses only a brand acronym (e.g. a three-letter initialism standing in for a multi-word registered name) and the search snippet contains no text linking the brand to the legal name, DeepSeek correctly returns `null` — the evidence to make the match simply is not in the title/snippet. This is not a selection error; it is an information ceiling of the scout stage (which only sees title + snippet).

**Current mitigation:** The identity gate is the safety net — if the correct site is selected, the gate confirms it by reading the homepage (where the full legal name usually appears in the footer). If scout returns `null`, the company goes to review.

**Note:** A related failure mode — DeepSeek being lured by a third-party financial-data directory that lists the official URL (e.g. a stock-profile page stating "official website: …") — is guarded against by an explicit "mentioning a URL ≠ being the site" rule in the selection prompt, and backstopped by the identity gate.

---

### 8.4 Search-result instability across runs

Both Gemini grounding and Tavily can return different results for the same company on different runs (search ranking is inherently non-deterministic), and DeepSeek selection itself has run-to-run variance. Scout may succeed on one run and fail on another.

**Design stance:** Scout is not expected to be self-consistent. Its job is only to produce a homepage candidate; correctness is enforced downstream by the identity gate (wrong picks → `false_url` → review). Transient scout failures are retried automatically on the next `resume`/`rerun-failed`. This is why effort was invested in the gate as the authoritative verifier rather than in trying to stabilise scout.

---

### 8.5 Dead, parked, or suspended domains

Two paths depending on how the dead page renders:
- **Renders almost no content** (< `IDENTITY_GATE_EMPTY_THRESHOLD` chars): caught by the empty-page check → `empty_page` reason (transient; retried on next run, since the site may come back).
- **Renders enough content to parse but belongs to the wrong entity** (e.g. a parked-domain landing page, a suspension notice from the registrar): caught by the identity gate → `false_url` (permanent).

No main-domain fallback is attempted when only a language sub-site (e.g. `en.example.com`) is dead but the apex domain may be live.

**Future improvement (low priority, low frequency):** On a dead sub-site, optionally retry the apex domain. Deferred — too infrequent to justify the added logic.

---

### 8.6 Tab-based / XHR-loaded leadership content

Some sites hide board/management content inside JavaScript tabs. Phase 2 applies `SHOW_ALL_TABS_JS` to reveal tab content before capturing markdown. Sites that load tab content via XHR on click (rather than toggling pre-rendered visibility) may still yield incomplete content.

`SHOW_ALL_TABS_JS`'s click selectors (`[role="tab"]`, `button[class*="tab"]`, `.nav-pills li a`, etc.) skip any element inside `<nav>`/`<header>`/`<footer>` (`inChrome()` guard) so they don't accidentally expand a site's global navigation menu instead of an in-page content tab. This guard turned out not to be the fix for the nav-bloat problem it was originally added for (see 5.7.1 — that page's bloat was present in the DOM with or without any JS running), but it's kept as a cheap, harmless precaution against a genuinely tab-shaped mega-menu being clicked open.

**Future improvement:** Add targeted XHR-wait strategies for tab implementations `SHOW_ALL_TABS_JS` cannot reach.

---

### 8.7 Extractor content sizing (largely resolved)

**Previously:** `node_extractor.py` truncated the *combined* markdown of all confirmed pages to a flat 40,000 characters. With 2+ confirmed pages, every page after the first was silently invisible to the LLM; with 1 confirmed page of unusually long content (large board with detailed bios), the tail could still be cut.

**Now resolved by 5.7.1's dynamic per-page budget** (`EXTRACTOR_PAGE_MIN_CHARS` / `EXTRACTOR_MAX_TOTAL_CHARS`, each confirmed page pre-trimmed independently via `extract_preview()`) — every confirmed page gets a guaranteed share of the budget regardless of how many pages were confirmed.

**Remaining edge case:** If Step 3 confirms an unusually high number of pages (e.g. 6+), the per-page floor (`EXTRACTOR_PAGE_MIN_CHARS` = 15,000) means the total sent can exceed `EXTRACTOR_MAX_TOTAL_CHARS`, which is then hard-capped at the join — so the last confirmed page(s) can still be truncated in that scenario. Rare in practice (Step 3 usually confirms 1–3 pages).

**Future improvement:** Chunk long content and merge extraction results across chunks, if the multi-page-truncation edge case above is ever observed to matter in practice.

---

### 8.8 Residual crawl-hang risk

Some pages caused `crawler.arun()` to hang well past `CRAWL_PAGE_TIMEOUT_MS` (30s) with no error ever surfacing — crawl4ai's internal timeout doesn't cover every hang mode (e.g. a stuck browser/CDP communication channel). `CRAWL_HARD_TIMEOUT_S` (5.7) wraps both `crawler.arun()` call sites in `asyncio.wait_for(timeout=60)` as a backstop, so the pipeline can no longer hang indefinitely on one company — the worst case is now a bounded ~60s stall per stuck URL, logged and skipped.

**Root cause of the underlying hangs is still unconfirmed** — observed occurrences so far have not reproduced deterministically on retry, and the browser process CPU profile during a hang looked idle (waiting on something) rather than looping. The hard timeout is a safety net, not a fix for whatever the site/browser-level cause is.

---

### 8.9 Gemini backend uncaught exceptions broke the Scout cascade (fixed)

`tavily_backend.py → search_tavily()` always wrapped its API call in `try/except`, returning `[]` on any error so `node_scout.py`'s cascade (`if not candidates: continue`) could transparently fall back to the other backend. `gemini_backend.py → search_gemini()` had no equivalent guard — a transient Gemini-side error (observed: `503 UNAVAILABLE`) propagated as an uncaught exception straight out of `scout()`, killing the company's entire pipeline attempt immediately instead of falling back to Tavily within the same run. Fixed: `search_gemini()` now catches exceptions the same way, logs a warning, and returns `[]`. See 5.1.

---

### 8.10 SPA navigation with no real `<a href>` links

Some sites (observed: a Vue + Element Plus SPA) render their nav menu as `<li>`/`<div>` elements with JS `onclick` client-side routing — no `<a href="...">` anywhere in the DOM for the menu items. `result.links["internal"]` is correctly empty because there is genuinely no real hyperlink to extract; this isn't a timing or visibility problem, so neither a longer `delay_before_return_html` nor `SHOW_ALL_TABS_JS`-style clicking-then-reading-the-DOM helps — the menu item's target URL only exists as a client-side route pushed via the History API after a real click event, so the *page still renders the menu text itself* (confirmed via `cleaned_html`: labels like "Our Leadership" are visible), but its href/target is unrecoverable by static DOM/link scanning.

**Not fixed — accepted as a boundary.** Would require a categorically different mechanism: detecting near-zero `raw` links on a crawled page and falling back to an interactive step (click the matching menu label with Playwright, read `page.url()` after the client-side route change). Judged not worth the added architecture for what is currently a rare site pattern; revisit if it becomes a recurring failure cause in `failures_latest.csv`.

---

## 9. License

MIT — see [LICENSE](LICENSE).

Note that this project crawls third-party websites. You are responsible for
complying with the terms of service and `robots.txt` of any site you point it
at, and for the usage terms of the Gemini, DeepSeek, and Tavily APIs.

