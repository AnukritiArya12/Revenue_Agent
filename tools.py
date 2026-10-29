"""Document navigation and source-verified revenue submission tools."""

import re
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional, Annotated

import pymupdf
from langchain_core.tools import tool, InjectedToolCallId
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.graph import END

from tools.pdf_loader import (
    get_document_outline,
    extract_page_content_and_tables,
    format_consecutive_pages,
    _resolve_document_path,
)
from tools.docx_loader import load_uploaded_docx, sections_to_text
from agent.schemas import RevenueDetails

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CURRENT = {"path": None}


# ---------------------------------------------------------------- helpers

def set_current_document(path) -> None:
    """Called by the agent: tools keep working if the model mistypes the path."""
    _CURRENT["path"] = str(path)


def _resolve(name: Optional[str] = None) -> Path:
    if name:
        try:
            return _resolve_document_path(name)
        except Exception:
            pass
    if _CURRENT["path"]:
        return Path(_CURRENT["path"])
    raise FileNotFoundError(f"Document not found: {name}")


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).casefold())


def _clean(amount) -> str:
    """'Rs 1,30,144.5 crore' -> '130144.5'"""
    return re.sub(r"[^\d.]", "", str(amount or "")).strip(".")


def _has(line: str, target: str) -> bool:
    return any(t.replace(",", "") == target for t in _NUM.findall(line))


def _numbers(line: str) -> list:
    out = []
    for tok in _NUM.findall(line):
        if re.fullmatch(r"(19|20)\d\d", tok):  # a year, not an amount
            continue
        try:
            out.append(float(tok.replace(",", "")))
        except ValueError:
            pass
    return out


def _looks_like_note(row: str, target: str) -> bool:
    """A Note number (e.g. 3.19) sits next to a much larger real amount."""
    try:
        amount = float(target)
    except ValueError:
        return False
    return amount > 0 and any(v > amount * 20 for v in _numbers(row))


def _label_ok(label: str, row: str) -> bool:
    key = _norm(str(label).split("|")[0])[:18]
    return not key or key in _norm(row)


def _locate_pdf(path: Path, pages: list, target: str):
    """Find the row holding `target` on the given pages. Returns (page, row)."""
    for pno in pages:
        try:
            data = extract_page_content_and_tables(str(path), pno)
        except Exception:
            continue
        for table in data["tables"]:
            for line in table.splitlines():
                if _has(line, target):
                    return pno, line.strip()
        lines = [l.strip() for l in data["text"].splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if _has(line, target):
                if i > 0 and not re.search(r"[A-Za-z]{3}", line):
                    return pno, f"{lines[i - 1]} {line}"  # label on previous line
                return pno, line
    return None, None


def _locate_docx(path: Path, target: str) -> Optional[str]:
    with open(path, "rb") as f:
        data = load_uploaded_docx(path.name, f.read())
    for section in data["sections"]:
        if _has(section["text"], target):
            return f"{section['location']}: {section['text']}"
    return None


def _normalize_source_pages(value: Any) -> List[int]:
    """Accept 311, '311', ['311'], [311] or '311, 312'."""
    if value is None or value == "":
        return []
    if isinstance(value, bool):
        raise ValueError("Boolean page number is invalid")
    if isinstance(value, int):
        values = [value]
    elif isinstance(value, str):
        values = [v for v in re.split(r"[,;\s\[\]]+", value.strip()) if v]
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise ValueError(f"Unsupported source_pages: {type(value).__name__}")
    out = []
    for item in values:
        text = str(item).strip()
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f"Invalid source page: {item!r}")
        if int(text) not in out:
            out.append(int(text))
    return out


def _scan(path: Path, first: int, last: int) -> str:
    """One line per page: its own first lines (running headers removed) and a
    marker: [T] = mostly numbers (a table page), [text] = narrative page."""
    pages = {}
    with pymupdf.open(path) as doc:
        total = len(doc)
        first = max(1, min(first, total))
        last = max(first, min(last, total))
        for p in range(first, last + 1):
            text = doc[p - 1].get_text("text", sort=True)
            pages[p] = (text, [l.strip() for l in text.splitlines() if l.strip()])

    counts = Counter(
        re.sub(r"\d+", "", line) for _, lines in pages.values() for line in set(lines)
    )
    limit = max(3, len(pages) // 2)

    out = [
        "[T] = mostly numbers (table page), [text] = narrative page; "
        "repeated running headers removed."
    ]
    for p, (text, lines) in pages.items():
        if len(text.strip()) < 40:
            out.append(f"Page {p} [blank]")
            continue
        own = [l for l in lines if counts[re.sub(r"\d+", "", l)] < limit]
        ratio = sum(c.isdigit() for c in text) / max(1, len(text))
        tag = "T" if ratio >= 0.10 else "text"
        out.append(f"Page {p} [{tag}]: {' | '.join(own[:4])[:120]}")
    return "\n".join(out)


# ------------------------------------------------------------------ tools

@tool
def inspect_document_outline(document_name: Optional[str] = None) -> str:
    """Show total pages and bookmarks/contents with page ranges. Call this first, once."""
    try:
        path = _resolve(document_name)

        if path.suffix.lower() == ".docx":
            with open(path, "rb") as f:
                data = load_uploaded_docx(path.name, f.read())
            return (
                f"Document: {data['filename']}\nFormat: DOCX\n"
                f"Total sections: {data['total_sections']}\n"
                "Use read_page with section offsets 1, 21, 41, ... (20 sections each)."
            )

        data = get_document_outline(str(path))
        total = data["total_pages"]
        lines = [f"Document: {data['filename']}", f"Total Pages: {total}"]
        outline = data.get("outline", [])

        if outline:
            starts = sorted({i["page"] for i in outline})
            lines.append("=== BOOKMARKS (page ranges) ===")
            for item in outline[:120]:
                later = [p for p in starts if p > item["page"]]
                end = later[0] - 1 if later else total
                indent = "  " * max(0, int(item.get("level", 1)) - 1)
                lines.append(f"{indent}Pages {item['page']}-{end}: {item['title'][:90]}")
        elif data.get("printed_toc_preview"):
            lines.append("=== PRINTED CONTENTS (first pages; printed page numbers may "
                         "differ from PDF page numbers) ===")
            lines.append(data["printed_toc_preview"][:4000])
        else:
            lines.append("No bookmarks or contents found. Financial statements usually "
                         "sit in the back 60% of an annual report.")
        return "\n".join(lines)
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def scan_document_page_headers(
    start_page: int,
    end_page: int,
    document_name: Optional[str] = None,
) -> str:
    """Show the title lines of at most 25 PDF pages, marking table pages [T]."""
    try:
        path = _resolve(document_name)
        if path.suffix.lower() != ".pdf":
            return "For DOCX use read_page."
        start = max(1, int(start_page))
        end = max(start, min(int(end_page), start + 24))
        return _scan(path, start, end)
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def read_page(
    page_number: int,
    document_name: Optional[str] = None,
    num_pages: int = 1,
) -> str:
    """Read ONE PDF page (tables and text), or 20 DOCX sections from an offset."""
    try:
        path = _resolve(document_name)

        if path.suffix.lower() == ".docx":
            with open(path, "rb") as f:
                data = load_uploaded_docx(path.name, f.read())
            sections = data["sections"]
            total = len(sections)
            if not total:
                return "ERROR: Empty DOCX document."
            offset = max(0, min(int(page_number) - 1, total - 1))
            selected = sections[offset:offset + 20]
            return (f"Document: {path.name}\nSections: {offset + 1} to "
                    f"{offset + len(selected)} of {total}\n\n" + sections_to_text(selected))

        note = "[One page is returned per call.]\n" if num_pages > 1 else ""
        return note + format_consecutive_pages(str(path), int(page_number), 1)
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def submit_revenue_details(
    company_name: Optional[str] = None,
    fiscal_year: Optional[str] = None,
    scope: Optional[str] = "unknown",
    revenue_from_operations: Optional[str] = None,
    other_income: Optional[str] = None,
    total_income: Optional[str] = None,
    currency: Optional[str] = None,
    unit_scale: Optional[str] = None,
    source_pages: Any = None,
    matched_line: Optional[str] = None,
    supporting_evidence: Optional[str] = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> str:
    """Submit the revenue. The tool checks the amount and row label against the cited
    PDF page and writes the evidence row itself (do not pass evidence).
    revenue_from_operations = exact printed amount of the latest-year column (never a
    Note number). matched_line = row label only. source_pages = PDF page number(s).
    currency = ISO code (INR, USD, GBP). If the result starts with ERROR, fix that
    and submit again."""
    try:
        target = _clean(revenue_from_operations)
        if not target:
            return "ERROR: Revenue amount missing. Copy it from the revenue row."
        if not matched_line or not str(matched_line).strip():
            return "ERROR: Revenue row label missing (matched_line)."

        path = _resolve()
        if path.suffix.lower() == ".docx":
            row = _locate_docx(path, target)
            pages_out: List[int] = []
            if row is None:
                return (f"ERROR: Amount {revenue_from_operations} was not found as a "
                        "complete number in the document. Re-read the row and copy it exactly.")
        else:
            pages = _normalize_source_pages(source_pages)
            if not pages:
                return "ERROR: source_pages missing. Pass the PDF page number you read."
            page, row = _locate_pdf(path, pages, target)
            if row is None:
                return (f"ERROR: Amount {revenue_from_operations} was not found as a complete "
                        f"number on page(s) {pages}. Call read_page on the income statement "
                        "page and copy the amount exactly as printed.")
            pages_out = [page]

        if _looks_like_note(row, target):
            return (f"ERROR: {revenue_from_operations} looks like a Note number: the same row "
                    f"holds much larger amounts ({row[:200]}). Submit the large amount of the "
                    "latest-year column instead.")
        if not _label_ok(matched_line, row):
            return (f"ERROR: matched_line {matched_line!r} is not the label of the row that "
                    f"contains {revenue_from_operations}: {row[:200]}. Use the row's own label.")

        scope_value = str(scope or "unknown").strip().lower()
        if scope_value not in {"consolidated", "standalone", "unknown"}:
            scope_value = "unknown"

        printed = next(
            (t.strip(",") for t in _NUM.findall(row) if t.replace(",", "") == target),
            str(revenue_from_operations).strip(),
        )
        details = RevenueDetails(
            company_name=company_name,
            fiscal_year=fiscal_year,
            scope=scope_value,
            revenue_from_operations=printed,
            other_income=other_income,
            total_income=total_income,
            currency=currency,
            unit_scale=unit_scale,
            source_pages=pages_out,
            matched_line=str(matched_line).split("|")[0].strip() or str(matched_line).strip(),
            supporting_evidence=row,
        )
        # End the graph through its normal tool routing after verified submission.
        # Errors above return ordinary strings and permit the agent to retry.
        return Command(
            update={"messages": [ToolMessage(
                content=details.model_dump_json(),
                tool_call_id=tool_call_id,
                name="submit_revenue_details",
            )]},
            goto=END,
        )
    except Exception as exc:
        return f"ERROR: Invalid revenue submission: {exc}"
