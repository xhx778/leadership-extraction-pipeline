"""
schemas.py — Pydantic models shared across all nodes.
"""
from __future__ import annotations
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


# ── Scout types ───────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    # Sent to DeepSeek (title + snippet only — url is NEVER included in the prompt)
    title: str    # Gemini: web.title (site name); Tavily: real page title
    snippet: str  # Gemini: merged support segments; Tavily: content excerpt

    # Used by code after DeepSeek returns an index — never shown to the model
    url: str      # Gemini: proxy URL (unresolved); Tavily: real URL


@dataclass
class ScoutResult:
    status: str               # "found" | "not_found"
    url: Optional[str] = None
    evidence: Dict[str, Any] = dc_field(default_factory=dict)


class Executive(BaseModel):
    name: str
    title: str
    seniority_tier: Optional[Literal[
        "Board", "C-suite", "Partner/Advisor", "VP-level", "Director-level"
    ]] = None


class PipelineResult(BaseModel):
    company_name: str
    official_website: Optional[str] = None
    bfs_urls: List[str] = []          # URLs of leadership pages found via BFS
    source_type: Literal["official_website", "no_website"]
    executives: List[Executive] = []
    extraction_method: str = ""
    auditor_passed: bool = False
    auditor_notes: str = ""
    extraction_timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat()
    )
    elapsed_s: float = 0.0
    failure_reason: Optional[str] = None
    retry_count: int = 0
    # Token / call usage (split by model)
    gemini_in: int = 0
    gemini_out: int = 0
    gemini_think: int = 0
    deepseek_in: int = 0
    deepseek_out: int = 0
    tavily_calls: int = 0
