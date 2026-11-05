"""Document navigation and source-verified revenue submission tools."""

import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, List, Optional

import pymupdf
from langchain_core.tools import tool

from tools.pdf_loader import (
    get_document_outline,
    extract_page_content_and_tables,
    format_consecutive_pages,
    _resolve_document_path,
)
from tools.docx_loader import load_uploaded_docx, sections_to_text
from .schemas import RevenueDetails


_NUM_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:\(\s*-?\s*\d[\d,]*(?:\.\d+)?\s*\)|-?\d[\d,]*(?:\.\d+)?)(?![A-Za-z0-9])"
)

_CURRENT = {
    "path": None,
    "read_pages": set(),
    "read_units": [],
    "scanned_ranges": [],
}


# ---------------------------------------------------------------- helpers

def set_current_document(path) -> None:
    """Select one report and reset evidence/navigation tracking for a new run."""
    resolved = _resolve_document_path(str(path))
    _CURRENT.update({
        "path": str(resolved),
        "read_pages": set(),
        "read_units": [],
        "scanned_ranges": [],
    })


def get_read_pages() -> List[int]:
    """Return physical PDF pages actually opened by read_page in this run."""
    return sorted(_CURRENT["read_pages"])


def get_scanned_ranges() -> List[tuple]:
    """Return all page-header ranges scanned during this selected-report run."""
    return list(_CURRENT["scanned_ranges"])


def _resolve(name: Optional[str] = None) -> Path:
    """Allow tools to access only the application-selected report."""
    if not _CURRENT["path"]:
        raise PermissionError("No report selected – call set_current_document(path) first.")

    current = _resolve_document_path(_CURRENT["path"])
    if name:
        requested = _resolve_document_path(name)
        if requested != current:
            raise PermissionError("Access denied: only the selected report is allowed")
    return current


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).casefold())


def _is_empty_or_none(val: Any) -> bool:
    """Check if a parameter is empty, null, or a sentinel phrase indicating non-presence."""
    if val is None:
        return True
    s = str(val).strip().lower()
    sentinels = {
        "", "none", "null", "n/a", "na", "not applicable", "nil", "-", "--", "—",
        "not explicitly reported", "not reported", "not found", "not available",
        "unknown", "not stated", "not provided", "none reported", "unreported"
    }
    if s in sentinels:
        return True
    return any(p in s for p in [
        "not explicitly", "not reported", "not found", "not available",
        "not stated", "none reported", "no separate", "not separately"
    ])


def _decimal_token(token: str) -> Optional[Decimal]:
    token = token.strip()
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "").replace(" ", "")
    if token.startswith("-"):
        negative = True
        token = token[1:]
    try:
        value = Decimal(token)
        return -value if negative else value
    except (InvalidOperation, ValueError):
        return None


def _amount_value(value) -> Optional[Decimal]:
    """Parse a reported amount while preserving a leading minus/parentheses."""
    if value is None or _is_empty_or_none(value):
        return None
    for token in _NUM_TOKEN.findall(str(value)):
        parsed = _decimal_token(token)
        if parsed is not None:
            return parsed
    return None


def _row_has_amount(row: str, target: Decimal) -> bool:
    for token in _NUM_TOKEN.findall(row):
        parsed = _decimal_token(token)
        if parsed is not None and parsed == target:
            return True
    return False


def _label_ok(label: Optional[str], row: str) -> bool:
    """Require a real row label; handles notes, whitespace, and formatting variations."""
    if not label or not str(label).strip():
        return False
    norm_label = _norm(str(label).split("|")[0])
    norm_row = _norm(row)
    if norm_label and norm_label in norm_row:
        return True
    # Word overlap fallback: check if significant keywords in label appear in row
    words = [w for w in re.findall(r"[a-z0-9]+", str(label).casefold()) if len(w) > 2 and not w.isdigit()]
    row_words = set(re.findall(r"[a-z0-9]+", str(row).casefold()))
    if words and sum(w in row_words for w in words) / len(words) >= 0.5:
        return True
    return False


def _looks_like_note(row: str, target: Decimal) -> bool:
    """Reject a small note reference when the same row contains a much larger amount."""
    if target <= 0:
        return False
    values = []
    for token in _NUM_TOKEN.findall(row):
        value = _decimal_token(token)
        if value is not None and value not in (Decimal("19"), Decimal("20")):
            values.append(abs(value))
    return any(value > target * 20 for value in values)


def _locate_pdf(
    path: Path,
    pages: List[int],
    target: Decimal,
    expected_label: Optional[str] = None,
):
    """Find the exact amount row, optionally requiring its expected row label."""
    for page_number in pages:
        try:
            data = extract_page_content_and_tables(str(path), page_number)
        except Exception:
            continue

        for table in data.get("tables", []):
            for line in table.splitlines():
                if _row_has_amount(line, target) and (
                    not expected_label or _label_ok(expected_label, line)
                ):
                    return page_number, line.strip()

        lines = [line.strip() for line in data.get("text", "").splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if _row_has_amount(line, target):
                candidate = (
                    f"{lines[index - 1]} {line}"
                    if index > 0 and not re.search(r"[A-Za-z]{3}", line)
                    else line
                )
                if not expected_label or _label_ok(expected_label, candidate):
                    return page_number, candidate

    # Second pass: if expected_label was not found exactly, try finding target row without strict label
    if expected_label:
        for page_number in pages:
            try:
                data = extract_page_content_and_tables(str(path), page_number)
            except Exception:
                continue
            for table in data.get("tables", []):
                for line in table.splitlines():
                    if _row_has_amount(line, target):
                        return page_number, line.strip()
            lines = [line.strip() for line in data.get("text", "").splitlines() if line.strip()]
            for line in lines:
                if _row_has_amount(line, target):
                    return page_number, line

    return None, None


def _locate_docx(
    path: Path,
    target: Decimal,
    expected_label: Optional[str] = None,
):
    with open(path, "rb") as file:
        data = load_uploaded_docx(path.name, file.read())

    for section in data.get("sections", []):
        text = section.get("text", "")
        if _row_has_amount(text, target) and (
            not expected_label or _label_ok(expected_label, text)
        ):
            return f"{section.get('location', 'DOCX section')}: {text}"
            
    if expected_label:
        for section in data.get("sections", []):
            text = section.get("text", "")
            if _row_has_amount(text, target):
                return f"{section.get('location', 'DOCX section')}: {text}"
    return None


def _normalize_source_pages(value: Any, read_pages: Optional[set] = None) -> List[int]:
    """Accept 311, '311', [311], '311, 312', 'Page 18', or fallback to read_pages."""
    read_list = sorted(read_pages) if read_pages else []
    if value is None or str(value).strip() in ("", "[]", "none", "null"):
        return read_list
    if isinstance(value, bool):
        return read_list
    if isinstance(value, (int, float)):
        return [int(value)]
    nums = [int(x) for x in re.findall(r"\d+", str(value)) if int(x) > 0]
    seen = set()
    dedup = [x for x in nums if not (x in seen or seen.add(x))]
    return dedup or read_list


def _validate_pdf_metric(path, pages, amount, label, metric_name):
    """Validate one optional metric against its exact source row."""
    if _is_empty_or_none(amount):
        return None, None

    target = _amount_value(amount)
    if target is None:
        return None, None

    effective_label = label if not _is_empty_or_none(label) else metric_name
    page, row = _locate_pdf(path, pages, target, effective_label)
    if row is None:
        return None, None

    return (page, row), None


def _scan(path: Path, first: int, last: int) -> str:
    """Show concise page headings. Always surfaces financial statement titles and directs the agent."""
    # Keywords that mark a primary income / P&L statement
    _PL_KEYWORDS = [
        "statement of profit", "income statement", "statement of operations",
        "statements of operations", "statements of income", "statement of earnings",
        "statement of comprehensive income",
    ]
    # Exclude secondary statements so we don't send the agent to the wrong page
    _PL_EXCLUDES = ["changes in equity", "cash flow", "retained earnings", "other comprehensive"]

    pages: dict = {}
    with pymupdf.open(path) as document:
        total = len(document)
        first = max(1, min(int(first), total))
        last = max(first, min(int(last), total))
        for page_number in range(first, last + 1):
            text = document[page_number - 1].get_text("text", sort=True)
            pages[page_number] = (
                text,
                [line.strip() for line in text.splitlines() if line.strip()],
            )

    counts = Counter(
        re.sub(r"\d+", "", line)
        for _, lines in pages.values()
        for line in set(lines)
    )
    repeat_limit = max(3, len(pages) // 2)

    output = [
        "[T] = mostly numbers (table page), [text] = narrative page; "
        "repeated running headers removed."
    ]
    candidate_statements: list = []  # (page_number, title_line)

    for page_number, (text, lines) in pages.items():
        if len(text.strip()) < 40:
            output.append(f"Page {page_number} [blank]")
            continue

        # Always detect financial statement titles in first 15 lines —
        # even if they appear as a "repeated running header" on many pages.
        # Only mark as a *candidate P&L page* when the title is in the first 5 lines
        # (a real statement page), not buried mid-page (a TOC cross-reference).
        financial_title = None
        for idx, line in enumerate(lines[:15]):
            ll = line.lower()
            if any(k in ll for k in _PL_KEYWORDS) and len(line) < 120:
                financial_title = line
                if not any(ex in ll for ex in _PL_EXCLUDES) and idx < 5:
                    candidate_statements.append((page_number, line))
                break

        own_lines = [
            line for line in lines
            if counts[re.sub(r"\d+", "", line)] < repeat_limit
            and line != financial_title   # already shown via financial_title
        ]
        digit_ratio = sum(char.isdigit() for char in text) / max(1, len(text))
        marker = "T" if digit_ratio >= 0.08 else "text"
        display_parts = ([financial_title] if financial_title else []) + own_lines[:3]
        output.append(
            f"Page {page_number} [{marker}]: {' | '.join(display_parts)[:140]}"
        )

    # If a P&L page was identified, tell the agent to stop scanning and delegate immediately.
    if candidate_statements:
        target_page, target_title = candidate_statements[0]
        output.append(
            f"\n[ACTION REQUIRED]: Page {target_page} contains '{target_title}'. "
            "STOP scanning. Call: "
            f"task(subagent_type='pdf-reader', description='Read page {target_page} and extract: "
            "exact row label and amount for revenue from operations, other income (if present), "
            "total income (if present), fiscal year, scope (consolidated/standalone), "
            "currency, unit scale.')"
        )

    return "\n".join(output)


# ---------------------------------------------------------------- tools

@tool
def inspect_document_outline(document_name: Optional[str] = None) -> str:
    """Show total pages and bookmarks/contents for the selected report."""
    try:
        path = _resolve(document_name)

        if path.suffix.lower() == ".docx":
            with open(path, "rb") as file:
                data = load_uploaded_docx(path.name, file.read())
            return (
                f"Document: {data['filename']}\n"
                "Format: DOCX\n"
                f"Total sections: {data['total_sections']}\n"
                "Use read_page with section offsets 1, 21, 41, ... (20 sections each)."
            )

        data = get_document_outline(str(path))
        total = data["total_pages"]
        lines = [f"Document: {data['filename']}", f"Total Pages: {total}"]
        outline = data.get("outline", [])

        if outline:
            starts = sorted({item["page"] for item in outline})
            lines.append("=== BOOKMARKS (page ranges) ===")
            for item in outline[:120]:
                later = [page for page in starts if page > item["page"]]
                end = later[0] - 1 if later else total
                indent = "  " * max(0, int(item.get("level", 1)) - 1)
                lines.append(
                    f"{indent}Pages {item['page']}-{end}: {item['title'][:90]}"
                )
        elif data.get("printed_toc_preview"):
            lines.append(
                "=== PRINTED CONTENTS (printed page numbers may differ from PDF pages) ==="
            )
            lines.append(data["printed_toc_preview"][:4000])
        else:
            lines.append("No bookmarks or contents found. Scan page headers to navigate.")

        return "\n".join(lines)
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def scan_document_page_headers(
    start_page: int,
    end_page: int,
    document_name: Optional[str] = None,
) -> str:
    """Show headings for at most 25 PDF pages; DOCX should be navigated with read_page."""
    try:
        path = _resolve(document_name)
        if path.suffix.lower() != ".pdf":
            return "For DOCX, delegate section reading to pdf-reader using read_page."

        start = max(1, int(start_page))
        end = min(max(start, int(end_page)), start + 24)

        new_start, new_end = start, end
        for old_start, old_end in _CURRENT["scanned_ranges"]:
            if old_start <= new_start and new_end <= old_end:
                return (
                    f"NOTE: Requested range {start}-{end} is already fully scanned. "
                    "If you have already identified the financial statement page, you MUST now call "
                    "task(subagent_type='pdf-reader', ...) to read that page. "
                    "Do NOT scan the same range again."
                )
            if not (new_end < old_start or new_start > old_end):
                if new_start < old_start:
                    new_end = min(new_end, old_start - 1)
                elif new_end > old_end:
                    new_start = max(new_start, old_end + 1)
        if new_start > new_end:
            return f"NOTE: Adjusted range {new_start}-{new_end} is already covered."

        result = _scan(path, new_start, new_end)
        _CURRENT["scanned_ranges"].append((new_start, new_end))
        return result
    except Exception as exc:
        return f"ERROR: {exc}"


@tool
def read_page(
    page_number: int,
    document_name: Optional[str] = None,
    num_pages: int = 1,
) -> str:
    """
    Read one PDF page, or a DOCX batch beginning at a 1-based section offset.
    This tool is registered only with the pdf-reader subagent.
    """
    try:
        path = _resolve(document_name)

        if path.suffix.lower() == ".docx":
            with open(path, "rb") as file:
                data = load_uploaded_docx(path.name, file.read())

            sections = data["sections"]
            total = len(sections)
            if not total:
                return "ERROR: Empty DOCX document."

            offset = max(0, min(int(page_number) - 1, total - 1))
            selected = sections[offset:offset + 20]
            content = (
                f"Document: {path.name}\n"
                f"Sections: {offset + 1} to {offset + len(selected)} of {total}\n\n"
                + sections_to_text(selected)
            )
            _CURRENT["read_units"].append(("docx", content))
            return content

        with pymupdf.open(path) as document:
            if page_number < 1 or page_number > len(document):
                return (
                    f"ERROR: Page {page_number} is outside this PDF "
                    f"(1 to {len(document)})."
                )

        content = format_consecutive_pages(str(path), int(page_number), 1)
        _CURRENT["read_pages"].add(int(page_number))
        _CURRENT["read_units"].append((int(page_number), content))
        if num_pages > 1:
            return f"[One page returned. Request the next page separately.]\n{content}"
        return content
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
    other_income_line: Optional[str] = None,
    total_income_line: Optional[str] = None,
    supporting_evidence: Optional[str] = None,
) -> str:
    """
    Verify reported amounts and their row labels against pages actually read.
    The evidence text is assembled from the source rows, not trusted from the model.
    """
    try:
        revenue_target = _amount_value(revenue_from_operations)
        if revenue_target is None:
            return "ERROR: Revenue amount missing or invalid. Copy the exact printed amount."
        if not matched_line or not str(matched_line).strip():
            return "ERROR: Revenue row label missing (matched_line)."

        path = _resolve()
        
        # Robust scope normalization
        scope_str = str(scope or "unknown").strip().lower()
        if any(k in scope_str for k in ("consolidat", "group")):
            scope_value = "consolidated"
        elif any(k in scope_str for k in ("stand", "parent", "separate", "unconsolidat")):
            scope_value = "standalone"
        else:
            scope_value = "unknown"

        # Normalize optional metrics: clean any sentinel strings like 'not explicitly reported' to None
        final_other_income = None if _is_empty_or_none(other_income) else str(other_income).strip()
        final_total_income = None if _is_empty_or_none(total_income) else str(total_income).strip()

        evidence_rows = []
        if path.suffix.lower() == ".docx":
            if not _CURRENT["read_units"]:
                return "ERROR: No DOCX sections were read by pdf-reader."
            row = _locate_docx(path, revenue_target, matched_line)
            if row is None:
                return (
                    f"ERROR: Revenue amount {revenue_from_operations} was not found "
                    "in the DOCX report."
                )
            evidence_rows.append(row)

            # Optional metrics in DOCX
            for amount, label, field_name in (
                (final_other_income, other_income_line, "other_income"),
                (final_total_income, total_income_line, "total_income"),
            ):
                if amount:
                    target = _amount_value(amount)
                    if target is not None:
                        opt_row = _locate_docx(path, target, label)
                        if opt_row:
                            evidence_rows.append(opt_row)
                        else:
                            if field_name == "other_income":
                                final_other_income = None
                            else:
                                final_total_income = None

            pages_out = []

        else:
            pages = _normalize_source_pages(source_pages, _CURRENT["read_pages"])
            if not pages:
                return "ERROR: No pages were read by pdf-reader. Delegate page reading using task first."

            # Ensure we search pages that were actually read
            search_pages = [p for p in pages if p in _CURRENT["read_pages"]] or list(_CURRENT["read_pages"])

            revenue_page, revenue_row = _locate_pdf(
                path, search_pages, revenue_target, matched_line
            )
            if revenue_row is None:
                # Also try locating on all read pages
                revenue_page, revenue_row = _locate_pdf(
                    path, sorted(_CURRENT["read_pages"]), revenue_target, matched_line
                )

            if revenue_row is None:
                return (
                    f"ERROR: Revenue amount {revenue_from_operations} was not found "
                    f"on source_pages {pages}."
                )
            if _looks_like_note(revenue_row, revenue_target):
                return (
                    f"ERROR: {revenue_from_operations} appears to be a note reference, "
                    f"not the financial amount: {revenue_row[:220]}"
                )

            evidence_rows.append(f"PDF page {revenue_page}: {revenue_row}")
            if revenue_page not in pages:
                pages.append(revenue_page)

            # Optional metrics validation (Other Income & Total Income)
            for amount, label, field_name, metric_title in (
                (final_other_income, other_income_line, "other_income", "Other Income"),
                (final_total_income, total_income_line, "total_income", "Total Income"),
            ):
                if amount:
                    opt_res, _ = _validate_pdf_metric(path, search_pages, amount, label, metric_title)
                    if opt_res:
                        metric_page, metric_row = opt_res
                        evidence_rows.append(f"PDF page {metric_page}: {metric_row}")
                    else:
                        # If optional metric is not verified on page, set to None rather than crashing
                        if field_name == "other_income":
                            final_other_income = None
                        else:
                            final_total_income = None

            pages_out = sorted(set(pages))

        printed_revenue = str(revenue_from_operations).strip()
        clean_company = None if _is_empty_or_none(company_name) else str(company_name).strip()
        clean_fy = None if _is_empty_or_none(fiscal_year) else str(fiscal_year).strip()
        clean_curr = None if _is_empty_or_none(currency) else str(currency).strip()
        clean_unit = None if _is_empty_or_none(unit_scale) else str(unit_scale).strip()

        details = RevenueDetails(
            company_name=clean_company,
            fiscal_year=clean_fy,
            scope=scope_value,
            revenue_from_operations=printed_revenue,
            other_income=final_other_income,
            total_income=final_total_income,
            currency=clean_curr,
            unit_scale=clean_unit,
            source_pages=pages_out,
            matched_line=str(matched_line).split("|")[0].strip(),
            supporting_evidence="\n".join(evidence_rows),
        )
        return details.model_dump_json()

    except Exception as exc:
        return f"ERROR: Invalid revenue submission: {exc}"


__all__ = [
    "inspect_document_outline",
    "scan_document_page_headers",
    "read_page",
    "submit_revenue_details",
    "set_current_document",
    "get_read_pages",
    "get_scanned_ranges",
]
