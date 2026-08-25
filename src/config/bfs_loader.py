import re
import yaml
from pathlib import Path
from typing import Optional

_DATA: Optional[dict] = None


def _load() -> dict:
    global _DATA
    if _DATA is None:
        path = Path(__file__).parent / "bfs_patterns.yaml"
        with open(path, "r", encoding="utf-8") as f:
            _DATA = yaml.safe_load(f)
    return _DATA


def get_title_pattern() -> re.Pattern:
    """Regex that matches any leadership title keyword on a page."""
    keywords = _load()["title_keywords"]
    # Longest first so multi-word phrases (e.g. "Managing Director") match before "Director"
    parts = [re.escape(k) for k in sorted(keywords, key=len, reverse=True)]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)


def get_heading_pattern() -> re.Pattern:
    """Regex that matches section headings indicating a leadership list."""
    phrases = _load()["heading_phrases"]
    parts = [re.escape(p) for p in sorted(phrases, key=len, reverse=True)]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)
