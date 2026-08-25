"""
node_auditor.py — Node 4: Auditor
Pure Python three-layer filter. Zero LLM calls.
"""
from __future__ import annotations
import logging
from typing import List, Tuple

from schemas import Executive

logger = logging.getLogger("pipeline_gc_bfs.auditor")

_MIN_NAME_PARTS = 2
_MAX_NAME_PARTS = 7

UI_ARTIFACT_PATTERNS = [
    "click", "read more", "view profile", "http", "@", "tel:", "fax",
    "download", "menu", "home", "contact", "about", "more info",
    "learn more", "see all", "view all",
]

ORG_TITLE_PATTERNS = [
    "committee", "department of", "office of", "team of",
    "board secretariat", "secretariat",
]

# Roles that legitimately contain org words (e.g. "Audit Committee Chairman")
ROLE_OVERRIDES = ["chairman", "chair", "head", "chief", "president"]


def _check_name_format(name: str) -> Tuple[bool, str]:
    parts = name.split()
    if len(parts) < _MIN_NAME_PARTS:
        return False, f"Name too short ({len(parts)} parts): '{name}'"
    if len(parts) > _MAX_NAME_PARTS:
        return False, f"Name too long ({len(parts)} parts): '{name}'"
    if any(p in name.lower() for p in UI_ARTIFACT_PATTERNS):
        return False, f"UI artifact in name: '{name}'"
    return True, "OK"


def _check_org_title(title: str) -> Tuple[bool, str]:
    t = title.lower()
    if any(p in t for p in ORG_TITLE_PATTERNS):
        if not any(r in t for r in ROLE_OVERRIDES):
            return False, f"Organisational title (no role keyword): '{title}'"
    return True, "OK"


def audit(
    executives: List[Executive],
    company_name: str,
) -> Tuple[List[Executive], str]:
    """Filter executives. Returns (passed_execs, audit_notes_string)."""
    passed: List[Executive] = []
    notes: List[str] = []

    for exec in executives:
        # Layer 1: name format
        ok, reason = _check_name_format(exec.name)
        if not ok:
            notes.append(f"FAIL[name] {exec.name}: {reason}")
            continue

        # Layer 2: seniority
        if exec.seniority_tier is None:
            notes.append(f"FAIL[tier] {exec.name} '{exec.title}': not senior")
            continue

        # Layer 3: org title
        ok, reason = _check_org_title(exec.title)
        if not ok:
            notes.append(f"FAIL[org] {exec.name}: {reason}")
            continue

        passed.append(exec)
        notes.append(f"PASS {exec.name} [{exec.seniority_tier}]")

    return passed, " | ".join(notes)
