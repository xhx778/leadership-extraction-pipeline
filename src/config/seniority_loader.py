import re
import yaml
from pathlib import Path
from typing import Optional

_TIERS_CACHE: Optional[list] = None
_SORTED_PATTERNS_CACHE: Optional[list] = None

_BOARD_DIRECTOR_MODIFIERS = {
    "non-executive",
    "non executive",
    "independent",
    "lead independent",
    "executive director",
    "alternate",
}


def load_seniority_tiers() -> list:
    global _TIERS_CACHE
    if _TIERS_CACHE is None:
        path = Path(__file__).parent / "seniority_tiers.yaml"
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        _TIERS_CACHE = data["tiers"]
    return _TIERS_CACHE


def _get_sorted_patterns() -> list:
    """All (tier_name, pattern) pairs, sorted by pattern length descending."""
    global _SORTED_PATTERNS_CACHE
    if _SORTED_PATTERNS_CACHE is None:
        pairs = []
        for tier in load_seniority_tiers():
            for p in tier["title_patterns"]:
                pairs.append((tier["name"], p.lower()))
        pairs.sort(key=lambda x: -len(x[1]))
        _SORTED_PATTERNS_CACHE = pairs
    return _SORTED_PATTERNS_CACHE


def _normalize(title: str) -> str:
    t = title.lower().strip()
    t = re.sub(r"[^\w\s-]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return f" {t} "


def _classify_director(title_lower: str) -> Optional[str]:
    """Six-rule logic for Director-related titles."""
    stripped = title_lower.strip()

    # Rule 1: Board governance modifier → Board. Checked before the
    # "director" gate because extraction sometimes drops the word
    # "Director" and leaves only the modifier (e.g. "Non-Executive and
    # Non-Independent").
    if any(mod in title_lower for mod in _BOARD_DIRECTOR_MODIFIERS):
        return "Board"

    if "director" not in title_lower:
        return None

    # Rule 2: Managing Director → C-suite
    if "managing director" in title_lower:
        return "C-suite"

    # Rule 3: Senior Director → VP-level
    if "senior director" in title_lower:
        return "VP-level"

    # Rule 4: Standalone "Director" → Board
    if stripped == "director":
        return "Board"

    # Rule 5: Director of / , / - → Director-level
    if (stripped.startswith("director of ")
            or stripped.startswith("director,")
            or stripped.startswith("director -")
            or stripped.startswith("director–")
            or stripped.startswith("director –")):
        return "Director-level"

    # Rule 6: Fallback
    return "Director-level"


def _classify_head(title_raw_lower: str) -> Optional[str]:
    """"Head, X" / "Head of X" → VP-level (mirrors Director rule 5).

    Takes the raw (pre-_normalize) lowercased title, not the
    punctuation-stripped one classify_title uses elsewhere: _normalize()
    collapses "," and "-" into plain spaces, which would make "Head,
    Group X" indistinguishable from "Team Head" / "Head Teller". Checking
    the raw string preserves the separator so only titles that actually
    start with "Head<punct>" match — mid/low-level titles are left alone.
    """
    stripped = title_raw_lower.strip()
    if (stripped.startswith("head of ")
            or stripped.startswith("head,")
            or stripped.startswith("head -")
            or stripped.startswith("head–")
            or stripped.startswith("head –")):
        return "VP-level"

    # "Group Head" roles (e.g. "Group Technical Head", "Group Head,
    # Shipping") — senior divisional heads reporting at group level.
    # The "Group" qualifier is what distinguishes these from junior
    # "Team Head" / "Head Teller" titles, so require it regardless of
    # where "Head" falls in the string.
    if stripped.startswith("group ") and re.search(r"\bhead\b", stripped):
        return "VP-level"

    return None


_MD_ABBREV_RE = re.compile(r"^MD\s*[,&]")


def _classify_md(title_raw: str) -> Optional[str]:
    """"MD, X" / "MD & Head, X" → VP-level.

    "MD" is the abbreviated form of "Managing Director" used as a rank
    title in banks/insurers/asset managers (e.g. "MD, Group IT", "MD,
    Private Funds (India)") — one company can have a dozen of these, so
    unlike the spelled-out "Managing Director" (→ C-suite, see
    _classify_director), the abbreviation is treated as a senior
    divisional/rank title rather than a company-level chief executive.
    Case-sensitive and anchored to the start so it never matches inside
    a name or an unrelated word.
    """
    if _MD_ABBREV_RE.match(title_raw.strip()):
        return "VP-level"
    return None


def classify_title(title: str) -> Optional[str]:
    """Classify a job title into a seniority tier.
    Returns tier name or None if not senior."""
    if not title or not title.strip():
        return None

    title_norm = _normalize(title)
    title_lower = title_norm.strip()

    # First: handle Director-related titles
    director_tier = _classify_director(title_lower)
    if director_tier is not None:
        return director_tier

    # Then: handle "Head, X" / "Head of X" titles (raw string — see
    # _classify_head docstring for why it can't use title_lower)
    head_tier = _classify_head(title.lower().strip())
    if head_tier is not None:
        return head_tier

    # Then: handle "MD, X" abbreviated titles (raw, case-sensitive — see
    # _classify_md docstring)
    md_tier = _classify_md(title)
    if md_tier is not None:
        return md_tier

    # Then: substring match against yaml patterns, longest first
    for tier_name, pattern in _get_sorted_patterns():
        if f" {pattern} " in title_norm:
            return tier_name

    return None
