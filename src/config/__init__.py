"""
config — Central configuration for Pipeline A-BFS (Gemini Scout + Crawl4AI BFS Navigator)
All secrets come from the project-root .env file.
"""
from __future__ import annotations
import os
from dotenv import load_dotenv

# Load .env from project root (one level up from the config/ package)
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_BASE)
load_dotenv(os.path.join(_ROOT, ".env"))

# ── API keys ──────────────────────────────────────────────────────────────────
GOOGLE_API_KEY  : str = os.getenv("GOOGLE_API_KEY",   "")
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
TAVILY_API_KEY  : str = os.getenv("TAVILY_API_KEY",   "")

# ── Models ────────────────────────────────────────────────────────────────────
GEMINI_MODEL   = "gemini-2.5-flash"
DEEPSEEK_MODEL = "deepseek-chat"

# ── Paths ─────────────────────────────────────────────────────────────────────
SKILLS_DIR  = os.path.join(_BASE, "skills")
DATA_DIR    = os.path.join(_ROOT, "data")
RESULTS_DIR = os.path.join(_ROOT, "results")
INPUT_CSV   = os.path.join(DATA_DIR, "target_companies.csv")

# ── Scout ─────────────────────────────────────────────────────────────────────
SCOUT_BACKEND         = "gemini"   # "gemini" | "tavily"
SCOUT_THINKING_BUDGET = 256

EXTRACTION_METHOD = f"{SCOUT_BACKEND}_scout_bfs_deepseek"

# ── BFS Navigator ─────────────────────────────────────────────────────────────
BFS_MAX_DEPTH                  = 3
CRAWL_PAGE_TIMEOUT_MS          = 30_000
CRAWL_DELAY_BEFORE_RETURN_S    = 3.0   # JS-heavy nav (e.g. custom-element mega-menus) needs this; 1.5s was flaky
CRAWL_HARD_TIMEOUT_S           = 60   # asyncio-level backstop if crawl4ai's own page_timeout doesn't fire
IDENTITY_GATE_EMPTY_THRESHOLD  = 200   # homepage markdown below this → empty_page (transient)

# ── BFS LLM assistance ────────────────────────────────────────────────────────
BFS_LLM_LINK_THRESHOLD    = 10   # call LLM to prune links when a layer has > this many candidates
BFS_LLM_LINK_MAX_SELECT   = 5    # max links LLM picks per layer
BFS_CONTENT_PREVIEW_CHARS = 10000  # content sent per page to Step 3 LLM
EXTRACTOR_PAGE_MIN_CHARS  = 15000   # floor per confirmed page, even when many pages are confirmed
EXTRACTOR_MAX_TOTAL_CHARS = 100000  # total budget, split across confirmed pages (also the Extractor's final safety cap)
CLASSIFY_HEADING_SEARCH_CHARS = 20000  # classify_page() only searches this many chars for a heading match
HEADING_PROXIMITY_CHARS       = 600    # extract_preview(): max gap between a heading and the title hit that confirms it

# ── BFS URL signal vocabulary ─────────────────────────────────────────────────
STRONG_URL_SIGNALS: set[str] = {
    "leadership", "board-of-directors", "board-of-director",
    "management-team","management", "executive-team", "executive-committee",
    "senior-management", "our-leaders", "key-management",
    "/team", "/people", "/our-team", "/our-people", "/team.html",
    "corporate governance", "company governance", "directors", "committee",
}
WEAK_URL_SIGNALS: set[str] = {
    "about", "about-us", "who-we-are",
    "corporate", "company", "investors-relations",
}
LANG_HREF_SIGNALS: set[str] = {"/en/", "/en-us/", "/en-gb/", "/english/"}
LANG_TEXT_SIGNALS: set[str] = {"en", "en-us", "en-gb", "english"}
EXCLUDE_PATTERNS: set[str] = {
    "login", "signin", "sign-in", "register", "logout",
    "cart", "checkout", "shop", "store", "product", "pricing",
    "news", "blog", "press", "media", "article", "event",
    "careers", "jobs", "vacancy", "recruit",
    "contact", "faq", "help", "support", "feedback",
    "privacy", "terms", "cookie", "legal", "disclaimer",
    "social", "facebook", "twitter", "linkedin", "instagram", "youtube",
    "search", "sitemap", "404", "javascript:", "#","pdf",
    "esg", "sustainability",
}
