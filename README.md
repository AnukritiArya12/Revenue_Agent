# Deep Revenue Agent

A local financial document agent that navigates annual reports and extracts revenue figures with source evidence. It uses a DeepAgents tool-calling workflow, PyMuPDF for PDF tables and text, and a local Ollama model. Navigation is model-driven; the project does not use keyword-based retrieval, semantic search, hybrid retrieval, embeddings, or a vector database. A final exact-label/value check validates the cited source row after the agent has selected it.

## How it works

1. The agent checks the total pages, PDF bookmarks, and opening pages for the printed contents.
2. It uses the contents to locate the consolidated income statement, with standalone as fallback. If the physical PDF page is unclear, it reads short excerpts from bounded page ranges; it does not extract every page in full.
3. It reads the relevant statement in small page batches. Structured financial tables are shown first, with compact page context; if table extraction is unavailable, the reader falls back to page text.
4. The agent submits the report's own revenue label, latest period, scope, currency, scale, and source location. It can include distinct additional revenue measures separately. The submission tool checks cited amounts and labels against the source.

PDF page citations are taken from pages actually read. DOCX citations are retained as paragraph/table locations in the supporting evidence.

## Requirements

- Python dependencies from `requirements.txt`
- Ollama running locally with `qwen3.5:4b`

Set `OLLAMA_MODEL` or `OLLAMA_BASE_URL` in `.env` to override the defaults. The default Ollama endpoint is `http://localhost:11434`.

## Run from the command line

```bash
ollama run qwen3.5:4b
python run.py --document TCS_FY26.pdf
```

`--pdf` remains available as an alias. Pass a filename from `data/reports/` or a full path to a PDF or DOCX file.

## Run the web interface

```bash
streamlit run app.py
```

The interface can select a bundled report or upload a PDF/DOCX file up to 200 MB.

## Scope and limitations

The project currently has no evaluation benchmark, telemetry, or independent fiscal-year verifier. The submission check verifies the cited row and amount; the agent must confirm that the amount is under the latest-year column. The returned extraction should be reviewed against the cited source excerpt. Image-only scanned PDFs may not contain extractable text, and DOCX files use paragraph/table offsets rather than PDF page numbers.
