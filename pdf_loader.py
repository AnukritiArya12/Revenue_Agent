"""PyMuPDF document loader extracting tables, text, outlines, and multi-page tables."""

import re
from collections import Counter
from pathlib import Path
from typing import List, Dict, Any, Optional
import pymupdf

from config import REPORTS_DIR, MAX_FILE_SIZE_MB


def _resolve_document_path(filename: str) -> Path:
    """Resolve document path from filename or absolute path."""
    p = Path(filename)
    if p.is_file():
        return p.resolve()
    reports_p = (REPORTS_DIR / filename).resolve()
    if reports_p.is_file():
        return reports_p
    raise FileNotFoundError(f"Document not found: {filename}")


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

        # Handle missing TOC: provide preview of initial pages where printed index lives
        printed_toc_preview = ""
        if len(outline_items) < 3:
            previews = []
            preview_limit = min(10, total_pages)
            for i in range(preview_limit):
                txt = doc[i].get_text("text").strip()
                if txt:
                    # Capture first 500 chars of early index pages
                    previews.append(f"--- Page {i+1} ---\n{txt[:500]}")
            printed_toc_preview = "\n\n".join(previews)

        return {
            "filename": doc_path.name,
            "total_pages": total_pages,
            "outline": outline_items,
            "has_bookmarks": len(outline_items) >= 3,
            "printed_toc_preview": printed_toc_preview,
        }


def scan_page_headers(filename: str, start_page: int, end_page: int) -> str:
    """
    Scans the top titles/headers across a range of pages.
    Running headers/footers that repeat on many pages (e.g. company name, page
    numbers, report title) are removed, so the page's own heading is visible.
    """
    doc_path = _resolve_document_path(filename)
    with pymupdf.open(doc_path) as doc:
        total_pages = len(doc)
        start = max(1, min(start_page, total_pages))
        end = max(start, min(end_page, total_pages))

        page_lines = {}
        for pno in range(start, end + 1):
            txt = doc[pno - 1].get_text("text").strip()
            page_lines[pno] = [l.strip() for l in txt.splitlines() if l.strip()]

        # Lines repeating on many pages (digits ignored) are running headers.
        counts = Counter(
            re.sub(r"\d+", "", line)
            for lines in page_lines.values()
            for line in set(lines)
        )
        limit = max(3, len(page_lines) // 2)

        summaries = []
        for pno, lines in page_lines.items():
            own = [l for l in lines if counts[re.sub(r"\d+", "", l)] < limit]
            header_str = " | ".join(own[:4]) if own else "Empty Page"
            summaries.append(f"Page {pno}: {header_str[:160]}")

        return "\n".join(summaries)


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