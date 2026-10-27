"""Extract readable text and source locations from uploaded DOCX documents."""

from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

from config import MAX_FILE_SIZE_MB


WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_text(paragraph: ET.Element) -> str:
    """Read text, tabs, and line breaks from one Word paragraph."""

    parts = []
    for element in paragraph.iter():
        if element.tag == f"{WORD_NAMESPACE}t":
            parts.append(element.text or "")
        elif element.tag == f"{WORD_NAMESPACE}tab":
            parts.append("\t")
        elif element.tag == f"{WORD_NAMESPACE}br":
            parts.append("\n")
    return "".join(parts).strip()


def load_uploaded_docx(filename: str, content: bytes) -> dict:
    """Read an uploaded DOCX from memory and retain paragraph/table locations."""

    if Path(filename).suffix.lower() != ".docx":
        raise ValueError("Only DOCX files are supported")
    if not content:
        raise ValueError("Uploaded DOCX is empty")

    file_size_mb = len(content) / (1024 * 1024)
    if file_size_mb > MAX_FILE_SIZE_MB:
        raise ValueError(
            f"Document exceeds {MAX_FILE_SIZE_MB} MB limit"
        )

    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            document_xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as error:
        raise ValueError("Uploaded file is not a valid DOCX document") from error

    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError as error:
        raise ValueError("DOCX document contains invalid XML") from error
    body = root.find(f"{WORD_NAMESPACE}body")
    if body is None:
        raise ValueError("DOCX document has no readable body")

    sections = []
    paragraph_number = 0
    table_number = 0

    # Read top-level paragraphs and tables in their original document order.
    for block in body:
        if block.tag == f"{WORD_NAMESPACE}p":
            paragraph_number += 1
            text = _paragraph_text(block)
            if text:
                sections.append({
                    "location": f"Paragraph {paragraph_number}",
                    "text": text,
                })
        elif block.tag == f"{WORD_NAMESPACE}tbl":
            table_number += 1
            rows = block.findall(f"{WORD_NAMESPACE}tr")
            for row_number, row in enumerate(rows, start=1):
                cells = []
                for cell in row.findall(f"{WORD_NAMESPACE}tc"):
                    cell_paragraphs = [
                        _paragraph_text(paragraph)
                        for paragraph in cell.findall(f".//{WORD_NAMESPACE}p")
                    ]
                    cell_text = " ".join(text for text in cell_paragraphs if text)
                    cells.append(cell_text)

                row_text = " | ".join(cells).strip()
                if row_text:
                    sections.append({
                        "location": f"Table {table_number}, row {row_number}",
                        "text": row_text,
                    })

    return {
        "filename": Path(filename).name,
        "total_sections": len(sections),
        "sections": sections,
    }


def sections_to_text(sections: list[dict]) -> str:
    """Format DOCX sections with labels so answers can cite their locations."""

    return "\n\n".join(
        f"--- {section['location']} ---\n{section['text']}"
        for section in sections
    )
