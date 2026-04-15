from __future__ import annotations

import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ccr_runtime.common import dedupe_preserve_order

SEVERITY_COMMENT_LABELS = {
    "bug": "BUG",
    "warning": "WARNING",
    "info": "INFO",
}

_FIX_PREFIXES = (
    "use ",
    "add ",
    "change ",
    "replace ",
    "preserve ",
    "clamp ",
    "check ",
    "return ",
    "pass ",
    "load ",
    "initialize ",
    "pre-allocate ",
    "move ",
    "batch ",
    "wrap ",
    "guard ",
    "reuse ",
    "avoid ",
    "compute ",
    "cap ",
    "short-circuit ",
    "store ",
    "thread ",
    "refactor ",
)

_PERSONA_IMPACT_FALLBACKS = {
    "logic": "This can produce incorrect behavior on the affected code path.",
    "requirements": "This can make the implementation diverge from the expected product behavior.",
    "security": "This can weaken a safety boundary or leave a risky path unguarded.",
    "concurrency": "This can introduce blocking, races, leaks, or other concurrent execution hazards.",
    "performance": "This adds avoidable work on the affected path and can compound under load.",
}

_PERSONA_FIX_FALLBACKS = {
    "logic": "Restore the intended conditional/branch behavior and add a focused regression test for the failing case.",
    "requirements": "Align the implementation with the specified behavior and add a regression test that covers the requirement directly.",
    "security": "Harden the affected boundary with an explicit guard and add a test that exercises the risky input or path.",
    "concurrency": "Add the missing synchronization or cancellation guard and cover the failure mode with a focused concurrency test.",
    "performance": "Remove the redundant work on the hot path and add a benchmark or regression check so it does not come back.",
}

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _clean_text(text: Any) -> str:
    collapsed = re.sub(r"\s+", " ", str(text or "").strip())
    return collapsed.strip()


def _split_sentences(text: str) -> list[str]:
    cleaned = _clean_text(text)
    if not cleaned:
        return []
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(cleaned) if part.strip()]


def _strip_terminal_punctuation(text: str) -> str:
    return text.rstrip(" .!?:;")


def _coerce_fix_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in (_clean_text(entry) for entry in value) if item]
    if isinstance(value, str):
        cleaned = _clean_text(value)
        return [cleaned] if cleaned else []
    return []


def _looks_like_fix(sentence: str) -> bool:
    lowered = sentence.lower()
    if lowered.startswith(_FIX_PREFIXES):
        return True
    return any(token in lowered for token in (
        " for example, use ",
        " for example use ",
        " e.g. use ",
        " add: ",
        " use: ",
        " change ",
        " replace with ",
        " replace it with ",
    ))


def _extract_fixes(message: str, evidence: str, persona: str) -> list[str]:
    fixes: list[str] = []
    for sentence in [*_split_sentences(message), *_split_sentences(evidence)]:
        if _looks_like_fix(sentence):
            fixes.append(sentence)
    deduped = dedupe_preserve_order([item for item in fixes if item])
    if deduped:
        return deduped[:2]
    fallback = _PERSONA_FIX_FALLBACKS.get(persona) or _PERSONA_FIX_FALLBACKS["logic"]
    return [fallback]


def structured_finding_fields(finding: dict[str, Any]) -> dict[str, Any]:
    severity = str(finding.get("severity") or "info").strip().lower() or "info"
    persona = str(finding.get("persona") or "logic").strip().lower() or "logic"
    message = _clean_text(finding.get("message"))
    evidence = _clean_text(finding.get("evidence"))
    raw_title = _clean_text(finding.get("title"))
    raw_problem = _clean_text(finding.get("problem"))
    raw_impact = _clean_text(finding.get("impact"))
    raw_fixes = _coerce_fix_list(finding.get("suggested_fixes"))

    message_sentences = _split_sentences(message)
    evidence_sentences = _split_sentences(evidence)

    title = raw_title or _strip_terminal_punctuation(message_sentences[0] if message_sentences else "Issue requires attention")
    if not title:
        title = "Issue requires attention"

    fix_like_sentences = {sentence for sentence in message_sentences if _looks_like_fix(sentence)}
    non_fix_sentences = [sentence for sentence in message_sentences if sentence not in fix_like_sentences]

    if raw_problem:
        problem = raw_problem
    elif non_fix_sentences:
        problem = " ".join(non_fix_sentences)
    elif message_sentences:
        problem = message
    else:
        problem = title

    if raw_impact:
        impact = raw_impact
    elif len(non_fix_sentences) >= 2:
        impact = non_fix_sentences[1]
    elif evidence_sentences:
        impact = evidence_sentences[0]
    else:
        impact = _PERSONA_IMPACT_FALLBACKS.get(persona) or _PERSONA_IMPACT_FALLBACKS["logic"]

    suggested_fixes = raw_fixes[:2] if raw_fixes else _extract_fixes(message, evidence, persona)
    if not suggested_fixes:
        suggested_fixes = [_PERSONA_FIX_FALLBACKS.get(persona) or _PERSONA_FIX_FALLBACKS["logic"]]

    return {
        "severity_label": SEVERITY_COMMENT_LABELS.get(severity, severity.upper() or "INFO"),
        "title": _strip_terminal_punctuation(title),
        "problem": problem,
        "impact": impact,
        "suggested_fixes": suggested_fixes[:2],
    }


def render_comment_body(finding: dict[str, Any]) -> str:
    sections = structured_finding_fields(finding)
    lines = [f"**{sections['severity_label']}** — {sections['title']}."]
    lines.append(f"**Problem**: {sections['problem']}")
    lines.append(f"**Impact**: {sections['impact']}")
    fixes = list(sections["suggested_fixes"])
    if fixes:
        lines.append("**Suggested fixes**:")
        for index, fix in enumerate(fixes, start=1):
            prefix = f"{index}. "
            if index == 1:
                lines.append(f"{prefix}**(Recommended)** {fix}")
            else:
                lines.append(f"{prefix}{fix}")
    return "\n".join(lines)
