"""
utils.py — Shared helpers: prompt loader, JSON parser, DeepSeek caller, token tracker.
"""
from __future__ import annotations
import json
import os
import re
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from schemas import PipelineResult

from openai import OpenAI

from config import DEEPSEEK_MODEL, DEEPSEEK_API_KEY, HEADING_PROXIMITY_CHARS

deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)


# ── Token tracker ─────────────────────────────────────────────────────────────

def empty_tokens() -> dict:
    return {
        "gemini_in": 0, "gemini_out": 0, "gemini_think": 0,
        "deepseek_in": 0, "deepseek_out": 0,
        "tavily_calls": 0,
    }


# ── Prompt loader ─────────────────────────────────────────────────────────────

def load_prompt(skills_dir: str, filename: str, **kwargs) -> str:
    """Load a prompt template from skills/<filename>, strip leading # comment lines,
    then fill {placeholders} with kwargs."""
    path = os.path.join(skills_dir, filename)
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    lines, header_done = [], False
    for line in raw.splitlines():
        if not header_done:
            if line.startswith("#") or line.strip() == "":
                continue
            header_done = True
        lines.append(line)

    template = "\n".join(lines).strip()
    return template.format(**kwargs) if kwargs else template


# ── Content window extraction ─────────────────────────────────────────────────

def extract_preview(
    text: str,
    limit: int,
    pattern: "re.Pattern",
    heading_pattern: "Optional[re.Pattern]" = None,
    heading_proximity: int = HEADING_PROXIMITY_CHARS,
) -> str:
    """
    Return a LIMIT-char window of TEXT that's likely to hold real leadership content.

    Priority 1 — heading anchor: if HEADING_PATTERN matches a section title
    (e.g. "Board of Directors") that is itself followed within HEADING_PROXIMITY
    chars by a PATTERN hit (a real title word), start the window there. This
    avoids the density heuristic below being fooled by a single verbose bio that
    happens to mention "director" many times while describing someone's OTHER
    board seats — that can locally out-density the actual multi-person roster.

    Priority 2 — density window: the LIMIT-char span with the highest density of
    PATTERN hits, instead of always the first LIMIT chars — real content often
    sits well past nav/menu boilerplate on long pages.
    """
    if len(text) <= limit:
        return text

    if heading_pattern:
        for hm in heading_pattern.finditer(text):
            nearby = text[hm.end(): hm.end() + heading_proximity]
            if pattern.search(nearby):
                start = max(0, min(hm.start(), len(text) - limit))
                return text[start:start + limit]

    positions = [m.start() for m in pattern.finditer(text)]
    if not positions:
        return text[:limit]

    best_start, best_count, left = positions[0], 0, 0
    for right in range(len(positions)):
        while positions[right] - positions[left] > limit:
            left += 1
        count = right - left + 1
        if count > best_count:
            best_count, best_start = count, positions[left]

    start = max(0, min(best_start, len(text) - limit))
    return text[start:start + limit]


# ── JSON response parser ──────────────────────────────────────────────────────

def parse_json_response(text: str) -> Any:
    """Strip markdown fences and extract the first valid JSON array or object."""
    text = text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pattern in (r'\[[\s\S]*\]', r'\{[\s\S]*\}'):
        m = re.search(pattern, text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise json.JSONDecodeError("No JSON found in response", text, 0)


# ── DeepSeek caller ───────────────────────────────────────────────────────────

def deepseek_call(prompt: str, tokens: dict, temperature: float = 0.1) -> str:
    """Call DeepSeek chat API, update tokens dict in-place, return response text."""
    response = deepseek_client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    if response.usage:
        tokens["deepseek_in"]  += response.usage.prompt_tokens
        tokens["deepseek_out"] += response.usage.completion_tokens
    return response.choices[0].message.content or ""


# ── Result classification ──────────────────────────────────────────────────────

PERMANENT_FAILURE_REASONS = {
    "scout_failed_invalid_homepage",
    "all_filtered_out",
    "no_executives_extracted",
    "permanent_failure",
    "other",
    # Identity gate permanent failures (rerun-failed must skip these)
    "false_url",            # DeepSeek NO: wrong entity or parked/coming-soon/error page
    "identity_unconfirmed", # DeepSeek UNSURE: real content but entity not confirmable; needs human review
}

_TRANSIENT_MARKERS = ("429", "503", "timeout", "rate", "connection", "json", "parse")


def classify_result(result: "PipelineResult") -> Optional[str]:
    """Return a failure_reason string, or None if the result is a success."""
    if result.auditor_passed and result.executives:
        return None

    notes = result.auditor_notes

    if notes.startswith("Pipeline error"):
        if any(marker in notes.lower() for marker in _TRANSIENT_MARKERS):
            return "transient_failure"
        return "permanent_failure"

    if not result.executives:
        if "FAIL" in notes:
            return "all_filtered_out"
        return "no_executives_extracted"

    return "other"
