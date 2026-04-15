from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from ccr_runtime.common import write_text

SEVERITY_ORDER = {"bug": 0, "warning": 1, "info": 2}
REPORT_PERSONA_ORDER = ("requirements", "logic", "security", "concurrency", "performance")
REPORT_LABELS = {
    "logic": "LOGIC",
    "security": "SECURITY",
    "concurrency": "CONCURRENCY",
    "performance": "PERFORMANCE",
    "requirements": "REQUIREMENTS",
}


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.get(severity, 99)


def format_report(verified_findings: list[dict[str, Any]]) -> str:
    if not verified_findings:
        return "Проверенных замечаний не найдено.\n"

    lines: list[str] = []
    fallback_finding_number = 1
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in verified_findings:
        grouped[str(finding["persona"])].append(finding)

    for persona in REPORT_PERSONA_ORDER:
        items = grouped.get(persona)
        if not items:
            continue
        lines.append(f"## [{REPORT_LABELS[persona]}]")
        for item in sorted(
            items,
            key=lambda entry: (
                severity_rank(str(entry["severity"])),
                str(entry["file"]),
                int(entry["line"]),
                str(entry["candidate_id"]),
            ),
        ):
            confidence = str(item["consensus"])
            if item.get("tentative"):
                confidence = f"{confidence} — tentative"
            finding_number = int(item.get("finding_number") or 0)
            if finding_number <= 0:
                finding_number = fallback_finding_number
            lines.append(
                f"{finding_number}. [{str(item['severity']).upper()}] {item['file']}:{item['line']} — {confidence} — {item['message']}"
            )
            if item.get("evidence"):
                lines.append(f"   Evidence: {item['evidence']}")
            fallback_finding_number = max(fallback_finding_number, finding_number + 1)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(report_file: Path, verified_findings: list[dict[str, Any]]) -> str:
    report_text = format_report(verified_findings)
    write_text(report_file, report_text)
    return report_text
