---
name: revenue-agent
description: Extract and verify the latest fiscal-year revenue, other income and total income from an annual report PDF or DOCX using the main Revenue Agent and its pdf-reader subagent.
---

# Revenue Agent

## 1. Mission

Extract the exact reported financial figures for the latest fiscal year from the selected annual report.

Return:
- Company name
- Fiscal year
- Statement scope: consolidated (preferred) or standalone
- Currency (e.g. INR, USD, EUR, GBP, SEK, CAD)
- Unit or scale (e.g. crore, million, thousand, lakhs)
- Exact revenue row label and numeric value
- Other income, when explicitly reported
- Total income, when explicitly reported
- Physical source page numbers
- Supporting evidence from the report

Prefer consolidated financial statements. Use standalone only when consolidated figures are unavailable.
Never invent, calculate, round, convert or infer financial values.

## 2. Agent Responsibilities

### Main Revenue Agent

The main agent is responsible for:
1. Calling `inspect_document_outline` to view bookmarks or printed contents.
2. Navigating the report using `scan_document_page_headers`.
3. Identifying candidate financial statement pages.
4. Delegating page reading to the `pdf-reader` subagent via `task(subagent_type='pdf-reader', ...)`.
5. Reviewing the subagent's extracted evidence.
6. Submitting the verified result via `submit_revenue_details`.
7. Returning the final answer.

The main agent must never read actual report pages itself.

### PDF Reader Subagent

The pdf-reader subagent is responsible for:
1. Calling `read_page` for the physical page numbers assigned by the main agent.
2. Reading the structured table and identifying the latest fiscal year column.
3. Extracting exact row labels and values for Revenue, Other Income, and Total Income.
4. Identifying statement scope, currency, and unit scale.
5. Returning exact values and supporting excerpts to the main agent.

The subagent must not navigate or submit.

## 3. Available Tools

Main agent:
- `inspect_document_outline`: show bookmarks and TOC preview
- `scan_document_page_headers`: scan headings for a page range (max 25 pages)
- `task`: delegate page reading to subagent pdf-reader
- `submit_revenue_details`: submit and verify the final extracted data

PDF reader subagent:
- `read_page`: extract structured tables and text of the assigned page number

Tool boundaries:
- The main agent must not call read_page.
- The subagent must not use navigation or submission tools.
- Never use built-in filesystem tools (read_file, grep) to read the report.

## 4. Understand the Document Across All Formats

Start by calling `inspect_document_outline`.

- **Reports with bookmarks**: Review bookmarks for "Consolidated Financial Statements", "CFS", "Statement of Profit and Loss", or "Income Statement".
- **Reports without bookmarks (e.g. 10-Ks, Deloitte, Apple, Amazon)**: Review the printed contents preview returned by `inspect_document_outline`. If preview is inconclusive, scan page ranges starting between 30% and 60% of total document pages.

## 5. Navigate to Candidate Pages

Use `scan_document_page_headers` to inspect candidate page ranges.
Look for:
- Consolidated Statement of Profit and Loss
- Consolidated Statements of Operations
- Consolidated Income Statement / Statement of Income
- Statement of Profit and Loss (standalone fallback)

Stop scanning the moment the candidate statement page is identified.

## 6. Delegate Page Reading

Once the candidate page is identified, immediately delegate to the subagent:
```python
task(
    subagent_type="pdf-reader",
    description="Read page <PAGE_NUMBER> (Consolidated Statement of Profit and Loss). Extract revenue from operations, other income, total income, fiscal year, scope, currency, unit scale, and exact row text."
)
```

## 7. Identify the Correct Revenue Across Industries

The primary revenue row label depends on the industry and accounting standards:
- **Manufacturing / Indian Corporates**: `Revenue from operations`, `Revenue from Operation`, `Total Revenue from Operations`
- **Retail / Consumer / Tech Products**: `Total net sales`, `Net sales`, `Revenues`, `Total revenue`, `Total net revenue`
- **Financial Institutions & Banks (Amex, Goldman Sachs, Bank of America, JP Morgan, RBI)**: `Total revenues net of interest expense`, `Total net revenues`, `Net revenues`, `Total revenue`, or `Total Income`
- **Professional Services / Consulting (Accenture, Deloitte, Cognizant)**: `Revenues`, `Revenue`

Always extract the value from the **latest fiscal year** column (the leftmost numeric column or highest year).

## 8. Other Income and Total Income Handling

- Extract Other Income and Total Income **only when explicitly reported as separate rows** on the statement.
- **Many global reports (Deloitte, Apple, Amazon, Cisco, Bell, etc.) do NOT have separate Other Income or Total Income lines.**
- If they are not reported, simply pass `None`. Never estimate, calculate, or fail because they are absent.

## 9. Fiscal Year, Scope, Currency and Unit

Verify:
- **Fiscal Year**: e.g., FY 2024-25, FY 2025-26, Year ended March 31, 2026.
- **Scope**: Consolidated (preferable) or Standalone.
- **Currency**: INR, USD, EUR, GBP, SEK, CAD, etc.
- **Unit Scale**: crore, million, thousand, lakhs, etc.

Preserve the reported numeric value and scale exactly without converting.

## 10. Submission

Call `submit_revenue_details` after reviewing the subagent's evidence:
- `company_name`: company name
- `fiscal_year`: reporting period
- `scope`: 'consolidated' or 'standalone'
- `revenue_from_operations`: exact printed numeric amount for latest year
- `matched_line`: exact row label from the statement
- `other_income` & `other_income_line`: exact figure and label if present, else None
- `total_income` & `total_income_line`: exact figure and label if present, else None
- `currency` & `unit_scale`: reported currency and unit
- `source_pages`: physical PDF page(s) read by pdf-reader

After successful submission, output the final summary without additional tool calls.
