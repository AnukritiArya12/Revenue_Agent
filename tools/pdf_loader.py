"""PyMuPDF document loader extracting tables, text, outlines, and multi-page tables."""

import re
from collections import Counter
from pathlib import Path
from typing import List, Dict, Any, Optional
import pymupdf

from config import REPORTS_DIR, MAX_FILE_SIZE_MB


def _resolve_document_path(filename: str) -> Path:
    """Allow only PDF/DOCX reports directly inside REPORTS_DIR."""
    root = REPORTS_DIR.resolve()
    supplied = Path(filename)
    candidate = supplied.resolve() if supplied.is_absolute() else (root / supplied).resolve()
    if candidate.parent != root:
        raise PermissionError("Access denied: document must be inside data/reports")
    if candidate.suffix.lower() not in {".pdf", ".docx"}:
        raise PermissionError("Only PDF and DOCX reports are allowed")
    if not candidate.is_file():
        raise FileNotFoundError(f"Report not found: {candidate.name}")
    return candidate


def _detect_synthetic_outline(doc: pymupdf.Document) -> List[Dict[str, Any]]:
    """
    For PDFs without digital bookmarks, scan page titles and structural signatures
    across the document to detect major financial statement sections.
    """
    total = len(doc)
    detected = []
    seen = set()

    for pno in range(total):
        txt = doc[pno].get_text("text")
        lines = [l.strip() for l in txt.splitlines() if l.strip()][:6]
        if not lines:
            continue

        header_text = " ".join(lines).lower()
        is_cons = any(c in header_text for c in ["consolidated", "group"])

        # 1. Primary Statement Heading Match
        if any(term in header_text for term in [
            "statement of profit and loss", "statements of operations",
            "statement of operations", "income statement", "statements of income",
            "statement of earnings"
        ]):
            key = "cons_pnl" if is_cons else "pnl"
            if key not in seen:
                seen.add(key)
                title = "Consolidated Statement of Profit and Loss" if is_cons else "Statement of Profit and Loss"
                detected.append({"level": 1, "title": title, "page": pno + 1})
            continue

        # 2. Section Headers
        if any(term in header_text for term in ["consolidated financial statements", "consolidated balance sheet"]):
            if "cons_sec" not in seen and not any(k in header_text[:50] for k in ["notes", "refer", "annexure"]):
                seen.add("cons_sec")
                detected.append({"level": 1, "title": "Consolidated Financial Statements", "page": pno + 1})
            continue

        if any(term in header_text for term in ["standalone financial statements", "standalone balance sheet"]):
            if "stand_sec" not in seen and not any(k in header_text[:50] for k in ["notes", "refer", "annexure"]):
                seen.add("stand_sec")
                detected.append({"level": 1, "title": "Standalone Financial Statements", "page": pno + 1})
            continue

        # 3. Structural Financial Signature Fallback (at least 3 key financial lines together)
        lower_full = txt.lower()
        has_rev = any(w in lower_full for w in [
            "revenue from operation", "revenue from operations", "net sales",
            "total revenue", "total net sales", "total revenues", "revenues"
        ])
        has_prof = any(w in lower_full for w in [
            "profit before tax", "profit for the year", "operating profit",
            "operating income", "net income", "income before income tax"
        ])
        has_tax = any(w in lower_full for w in [
            "tax expense", "provision for income taxes", "income taxes",
            "income tax expense", "taxation"
        ])
        has_eps = any(w in lower_full for w in ["earnings per share", "eps", "per share"])

        score = sum([has_rev, has_prof, has_tax, has_eps])
        if score >= 3:
            cons_sig = any(w in lower_full[:400] for w in ["consolidated", "group"])
            key = "sig_cons" if cons_sig else "sig_pnl"
            if key not in seen:
                seen.add(key)
                title = "Consolidated Financial Statements (P&L / Operations)" if cons_sig else "Financial Statements (P&L / Operations)"
                detected.append({"level": 1, "title": title, "page": pno + 1})

    return sorted(detected, key=lambda x: x["page"])


def get_document_outline(filename: str) -> Dict[str, Any]:
    """
    Extracts total pages and bookmarks / Table of Contents (TOC).
    Handles documents with or without digital bookmarks.
    """
    doc_path = _resolve_document_path(filename)
    if doc_path.suffix.lower() != ".pdf":
        return {
            "filename": doc_path.name,
            "total_pages": 1,
            "outline": [],
            "has_bookmarks": False,
            "format": "docx/text",
        }

    with pymupdf.open(doc_path) as doc:
        total_pages = len(doc)
        toc = doc.get_toc()  # [[lvl, title, page], ...]

        outline_items = []
        if toc:
            for item in toc:
                lvl, title, page = item[0], str(item[1]).strip(), int(item[2])
                if title:
                    outline_items.append({
                        "level": lvl,
                        "title": title,
                        "page": page,
                    })

        # If digital bookmarks are missing or sparse, generate synthetic outline
        if len(outline_items) < 3:
            synthetic = _detect_synthetic_outline(doc)
            if synthetic:
                outline_items = synthetic

        # Provide preview of initial pages if still no outline
        printed_toc_preview = ""
        if len(outline_items) < 3:
            previews = []
            preview_limit = min(15, total_pages)
            for i in range(preview_limit):
                txt = doc[i].get_text("text").strip()
                if txt:
                    previews.append(f"--- Page {i+1} ---\n{txt[:1500]}")
            printed_toc_preview = "\n\n".join(previews)

        return {
            "filename": doc_path.name,
            "total_pages": total_pages,
            "outline": outline_items,
            "has_bookmarks": len(outline_items) >= 1,
            "printed_toc_preview": printed_toc_preview,
        }


def extract_page_content_and_tables(filename: str, page_number: int) -> Dict[str, Any]:
    """
    Extracts BOTH structured Markdown tables AND raw text from a specific PDF page.
    Preserves multi-year column headers and unit scale metadata.
    """
    doc_path = _resolve_document_path(filename)
    with pymupdf.open(doc_path) as doc:
        total_pages = len(doc)
        if page_number < 1 or page_number > total_pages:
            raise ValueError(f"Page number {page_number} is out of bounds (1 to {total_pages}).")

        page = doc[page_number - 1]
        raw_text = page.get_text("text", sort=True)

        extracted_tables = []
        try:
            tabs = page.find_tables()
            for tab in tabs:
                df = tab.extract()
                if not df:
                    continue

                table_lines = []
                for row in df:
                    cleaned_row = [str(cell).replace("\n", " ").strip() if cell is not None else "" for cell in row]
                    table_lines.append("| " + " | ".join(cleaned_row) + " |")

                if len(table_lines) > 1:
                    header = table_lines[0]
                    col_count = len(df[0])
                    separator = "| " + " | ".join(["---"] * col_count) + " |"
                    markdown_table = "\n".join([header, separator] + table_lines[1:])
                    extracted_tables.append(markdown_table)
        except Exception:
            pass

        return {
            "page_number": page_number,
            "total_pages": total_pages,
            "text": raw_text,
            "tables": extracted_tables,
            "has_tables": len(extracted_tables) > 0,
        }


def format_consecutive_pages(filename: str, start_page: int, num_pages: int = 2) -> str:
    """
    Reads consecutive pages to handle multi-page tables (e.g. continuing P&L statement
    or notes spanning across page boundaries).
    """
    doc_path = _resolve_document_path(filename)
    with pymupdf.open(doc_path) as doc:
        total_pages = len(doc)
        start = max(1, min(start_page, total_pages))
        end = min(total_pages, start + num_pages - 1)

    sections = []
    for pno in range(start, end + 1):
        data = extract_page_content_and_tables(filename, pno)
        page_header = f"=== Page {data['page_number']} of {data['total_pages']} ==="
        parts = [page_header]

        if data["tables"]:
            parts.append("\n[Structured Financial Tables Extracted via PyMuPDF]:")
            for i, tbl in enumerate(data["tables"], start=1):
                parts.append(f"\n--- Table {i} ---\n{tbl}")

        parts.append("\n[Page Text]:\n" + data["text"])
        sections.append("\n".join(parts))

    return "\n\n" + ("=" * 40) + "\n\n".join(sections)
