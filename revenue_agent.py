"""Deep Revenue Agent: navigates a report like a human, using deepagents + Ollama."""

import re
import time
from pathlib import Path
from typing import Optional

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain_core.messages import ToolMessage
from langchain_ollama import ChatOllama

from config import MODEL_NAME, OLLAMA_BASE_URL, MODEL_TEMPERATURE
from agent.schemas import RevenueDetails
from agent.prompts import DEEP_AGENT_SYSTEM_PROMPT
from agent.tools import (
    inspect_document_outline,
    scan_document_page_headers,
    read_page,
    submit_revenue_details,
    set_current_document,
)
from tools.pdf_loader import _resolve_document_path, get_document_outline

FILE_BUDGET_SECONDS = 20 * 60  # hard time budget per document
MAX_ATTEMPTS = 3


class DeepRevenueAgent:
    """Find the exact reported revenue in a PDF/DOCX annual report."""

    def __init__(
        self,
        model_name=MODEL_NAME,
        base_url=OLLAMA_BASE_URL,
        temperature=MODEL_TEMPERATURE,
    ):
        root = Path(__file__).resolve().parent.parent
        skill_path = root / "skills" / "revenue-agent" / "SKILL.md"
        if not skill_path.is_file():
            raise FileNotFoundError(f"Skill missing: {skill_path}")

        skill = skill_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        if not skill.startswith("---\n"):
            raise ValueError("SKILL.md requires YAML frontmatter.")
        # The skill body is given to the agent at the start of every attempt, so
        # it is always followed (the agent is not allowed to browse files).
        self.skill_body = skill.split("\n---\n", 1)[-1].strip()

        llm = ChatOllama(
            model=model_name,
            base_url=base_url,
            temperature=temperature,
            top_p=0.8,
            top_k=20,
            reasoning=False,
            num_ctx=16384,
            num_predict=1500,
            keep_alive="30m",
            client_kwargs={"timeout": 600},
        )

        self.agent = create_deep_agent(
            model=llm,
            tools=[
                inspect_document_outline,
                scan_document_page_headers,
                read_page,
                submit_revenue_details,
            ],
            system_prompt=DEEP_AGENT_SYSTEM_PROMPT,
            backend=StateBackend(),
        )

    # ---------- helpers ----------

    @staticmethod
    def _text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                i if isinstance(i, str) else str(i.get("text", ""))
                for i in content
                if isinstance(i, (str, dict))
            )
        return str(content or "")

    def _submission(self, messages) -> Optional[RevenueDetails]:
        """Latest successful submit_revenue_details result. The tool has already
        verified the amount and row label against the cited page."""
        for m in reversed(messages):
            if isinstance(m, ToolMessage) and m.name == "submit_revenue_details":
                try:
                    details = RevenueDetails.model_validate_json(
                        self._text(m.content).strip()
                    )
                    if details.revenue_from_operations and details.matched_line:
                        return details
                except Exception:
                    continue
        return None

    def _last_error(self, messages) -> str:
        for m in reversed(messages):
            if isinstance(m, ToolMessage) and m.name == "submit_revenue_details":
                text = self._text(m.content).strip()
                if text.startswith("ERROR"):
                    return text[:300]
        return ""

    @staticmethod
    def _calls(messages, name):
        out = []
        for m in messages:
            for c in getattr(m, "tool_calls", None) or []:
                if c.get("name") == name:
                    out.append(c.get("args") or {})
        return out

    def _scanned(self, messages) -> list:
        ranges = []
        for a in self._calls(messages, "scan_document_page_headers"):
            try:
                ranges.append((int(a["start_page"]), int(a["end_page"])))
            except Exception:
                pass
        return ranges

    def _pages_read(self, messages) -> list:
        pages = set()
        for a in self._calls(messages, "read_page"):
            try:
                pages.add(int(a["page_number"]))
            except Exception:
                pass
        return sorted(pages)

    @staticmethod
    def _unscanned_sections(path, scanned) -> str:
        """Bookmark sections the agent has not scanned yet (a hint for retries)."""
        try:
            data = get_document_outline(str(path))
        except Exception:
            return ""
        items = data.get("outline", [])
        starts = sorted({i["page"] for i in items})
        lines = []
        for item in items:
            later = [p for p in starts if p > item["page"]]
            end = later[0] - 1 if later else data["total_pages"]
            if end - item["page"] < 8:
                continue
            if any(a <= end and b >= item["page"] for a, b in scanned):
                continue
            lines.append(f"pages {item['page']}-{end}: {item['title'][:60]}")
        return "; ".join(lines[:8])

    def _run(self, messages, deadline):
        """Run the agent, printing each step. Never raises on agent errors."""
        final = None
        seen = 0
        try:
            for chunk in self.agent.stream(
                {"messages": messages},
                config={"recursion_limit": 40},
                stream_mode="values",
            ):
                final = chunk
                msgs = chunk.get("messages", [])
                for msg in msgs[seen:]:
                    calls = getattr(msg, "tool_calls", None)
                    if calls:
                        for c in calls:
                            print(f"  -> {c.get('name')} {str(c.get('args'))[:150]}", flush=True)
                    elif isinstance(msg, ToolMessage):
                        print(f"  <- {msg.name}: {self._text(msg.content)[:110]!r}", flush=True)
                seen = max(seen, len(msgs))

                if self._submission(msgs):
                    print("[*] Verified revenue submitted. Stopping agent.", flush=True)
                    break
                if time.time() > deadline:
                    print("[!] Time budget reached. Stopping agent.", flush=True)
                    break
        except Exception as exc:  # step limit, timeout, malformed tool call ...
            print(f"[!] Agent step failed ({type(exc).__name__}): {str(exc)[:200]}", flush=True)
        return final or {"messages": []}

    # ---------- main ----------

    def extract_revenue(self, document_name) -> RevenueDetails:
        path = _resolve_document_path(document_name)
        is_pdf = path.suffix.lower() == ".pdf"
        set_current_document(path)
        started = time.time()
        deadline = started + FILE_BUDGET_SECONDS

        print(f"[*] Starting Deep Revenue Agent: {path}", flush=True)

        task = (
            f"Document: {path}\n\n"
            f"<skill name=\"revenue-agent\">\n{self.skill_body}\n</skill>\n\n"
            "Follow the skill above and find the exact revenue of this document. "
            "Start with inspect_document_outline. Use only the four registered tools."
        )
        notes = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            output = self._run([{"role": "user", "content": task + notes}], deadline)
            msgs = output.get("messages", [])
            details = self._submission(msgs)

            if details:
                if not is_pdf:
                    details.source_pages = []
                print(f"[*] Done in {int(time.time() - started)} s (attempt {attempt}).", flush=True)
                return details

            scanned = self._scanned(msgs)
            read = self._pages_read(msgs)
            error = self._last_error(msgs) or "No successful submit_revenue_details call."
            print(f"[*] Attempt {attempt}/{MAX_ATTEMPTS} failed: {error}", flush=True)

            if attempt == MAX_ATTEMPTS or time.time() > deadline:
                raise RuntimeError(
                    f"Revenue not verified. Last problem: {error} "
                    f"Page ranges scanned: {scanned}. Pages read: {read}."
                )

            hint = self._unscanned_sections(path, scanned) if is_pdf else ""
            notes = (
                "\n\nA PREVIOUS ATTEMPT FAILED. "
                f"Page ranges already scanned: {scanned or 'none'}. "
                f"Pages already read: {read or 'none'}. "
                f"Last problem: {error} "
                + (f"Bookmark sections not scanned yet: {hint}. " if hint else "")
                + "Do not repeat those scans. Table pages [T] in scanned ranges that "
                "you have not read are still candidates. Try a different range, "
                "then read the income statement page and submit."
            )