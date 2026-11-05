"""Deep Revenue Agent with a dedicated PDF-reading subagent."""

import time
import uuid
from pathlib import Path
from typing import Optional

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain_core.messages import ToolMessage
from langchain_ollama import ChatOllama
from langgraph.types import Command
from langgraph.checkpoint.memory import InMemorySaver

from config import MODEL_NAME, OLLAMA_BASE_URL, MODEL_TEMPERATURE, REPORTS_DIR
from .schemas import RevenueDetails
from .prompts import (
    DEEP_AGENT_SYSTEM_PROMPT,
    PDF_READER_SUBAGENT_PROMPT,
)
from .tools import (
    inspect_document_outline,
    scan_document_page_headers,
    read_page,
    submit_revenue_details,
    set_current_document,
    get_read_pages,
    get_scanned_ranges,
)
from tools.pdf_loader import get_document_outline


FILE_BUDGET_SECONDS = 30 * 60
MAX_ATTEMPTS = 3


class DeepRevenueAgent:
    """Find exact reported revenue in one selected PDF/DOCX annual report."""

    def __init__(
        self,
        model_name=MODEL_NAME,
        base_url=OLLAMA_BASE_URL,
        temperature=MODEL_TEMPERATURE,
        hitl=False,
    ):
        root = Path(__file__).resolve().parent.parent
        skill_path = root / "skills" / "revenue-agent" / "SKILL.md"
        if not skill_path.is_file():
            raise FileNotFoundError(f"Skill missing: {skill_path}")

        skill = skill_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        if not skill.startswith("---\n"):
            raise ValueError("SKILL.md requires YAML frontmatter.")
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
            # Main agent navigates and submits. It delegates reading to pdf-reader.
            tools=[
                inspect_document_outline,
                scan_document_page_headers,
                submit_revenue_details,
            ],
            subagents=[
                {
                    "name": "pdf-reader",
                    "description": (
                        "Dedicated PDF reader subagent. Reads candidate financial statement pages "
                        "(using read_page). Extracts revenue from operations, other income, "
                        "total income, statement scope, fiscal year, currency, unit scale, exact row labels "
                        "and verbatim evidence. Call this using task(subagent_type='pdf-reader', description=...)."
                    ),
                    "model": llm,
                    "tools": [read_page],
                    "system_prompt": PDF_READER_SUBAGENT_PROMPT,
                }
            ],
            system_prompt=DEEP_AGENT_SYSTEM_PROMPT,
            backend=StateBackend(),
            **({"interrupt_on": {"submit_revenue_details": True}} if hitl else {}),
            checkpointer=InMemorySaver(),
        )

    @staticmethod
    def _text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                item if isinstance(item, str) else str(item.get("text", ""))
                for item in content
                if isinstance(item, (str, dict))
            )
        return str(content or "")

    def _submission(self, messages) -> Optional[RevenueDetails]:
        """Return the latest successful, tool-verified submission."""
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and message.name == "submit_revenue_details":
                try:
                    details = RevenueDetails.model_validate_json(
                        self._text(message.content).strip()
                    )
                    if details.revenue_from_operations and details.matched_line:
                        return details
                except Exception:
                    continue
        return None

    def _last_error(self, messages) -> str:
        for message in reversed(messages):
            if isinstance(message, ToolMessage) and message.name == "submit_revenue_details":
                content = self._text(message.content).strip()
                if content.startswith("ERROR"):
                    return content[:400]
        return ""

    @staticmethod
    def _calls(messages, name):
        calls = []
        for message in messages:
            for call in getattr(message, "tool_calls", None) or []:
                if call.get("name") == name:
                    calls.append(call.get("args") or {})
        return calls

    def _scanned(self, messages) -> list:
        # Keep scan history across retries; tools.py owns the selected-run state.
        return get_scanned_ranges()

    def _pages_read(self, messages) -> list:
        # Subagent tool calls are not always present in the parent message list.
        # The tool module records actual page reads in the shared selected-run state.
        return get_read_pages()

    @staticmethod
    def _unscanned_sections(path, scanned) -> str:
        try:
            data = get_document_outline(str(path))
        except Exception:
            return ""

        items = data.get("outline", [])
        starts = sorted({item["page"] for item in items})
        lines = []

        for item in items:
            later = [page for page in starts if page > item["page"]]
            end = later[0] - 1 if later else data["total_pages"]

            if end - item["page"] < 8:
                continue
            if any(start <= end and finish >= item["page"] for start, finish in scanned):
                continue

            lines.append(f"pages {item['page']}-{end}: {item['title'][:60]}")

        return "; ".join(lines[:8])

    def _run(self, messages, deadline):
        config = {
            "recursion_limit": 100,
            "configurable": {"thread_id": str(uuid.uuid4())},
        }
        input_value = {"messages": messages}
        final_state = None
        seen = 0

        while True:
            for chunk in self.agent.stream(
                input_value,
                config=config,
                stream_mode="values",
            ):
                current_messages = chunk.get("messages")
                if current_messages is None:
                    continue

                final_state = chunk
                for message in current_messages[seen:]:
                    calls = getattr(message, "tool_calls", None)
                    if calls:
                        for call in calls:
                            print(
                                f"  -> {call.get('name')} "
                                f"{str(call.get('args'))[:180]}",
                                flush=True,
                            )
                    elif isinstance(message, ToolMessage):
                        print(
                            f"  <- {message.name}: "
                            f"{self._text(message.content)[:140]!r}",
                            flush=True,
                        )

                seen = max(seen, len(current_messages))
                if time.time() > deadline and self._submission(current_messages) is None:
                    raise TimeoutError("Revenue agent exceeded the document time budget")

            state = self.agent.get_state(config)
            pending = [item for task in state.tasks for item in task.interrupts]
            if not pending:
                break

            interrupt_value = pending[0].value
            requests = (
                interrupt_value.get("action_requests", [])
                if isinstance(interrupt_value, dict)
                else []
            )

            print("\n" + "-" * 50 + "\n[HITL] Approval required before submission")
            for request in requests:
                args = request.get("args", {})
                print(f"Row label : {args.get('matched_line')}")
                print(
                    f"Revenue   : {args.get('revenue_from_operations')} "
                    f"({args.get('currency')}, {args.get('unit_scale')})"
                )
                print(
                    f"Scope     : {args.get('scope')} | "
                    f"Year: {args.get('fiscal_year')} | "
                    f"Pages: {args.get('source_pages')}"
                )
                if args.get("other_income") is not None:
                    print(f"Other income: {args.get('other_income')}")
                if args.get("total_income") is not None:
                    print(f"Total income: {args.get('total_income')}")

            if not requests:
                print(interrupt_value)

            pause_started = time.time()
            while True:
                answer = input("Approve? (y/n): ").strip().lower()
                if answer in {"y", "yes", "n", "no"}:
                    break
                print("Please enter y or n.")

            if answer in {"y", "yes"}:
                decisions = [{"type": "approve"}] * max(1, len(requests))
            else:
                reason = input("What is incorrect? ").strip()
                decisions = [
                    {
                        "type": "reject",
                        "message": (
                            f"Human rejected: {reason}. Correct the issue; "
                            "do not submit the same rejected value again."
                        ),
                    }
                ] * max(1, len(requests))

            deadline += time.time() - pause_started
            input_value = Command(resume={"decisions": decisions})

        return final_state or {"messages": []}

    def extract_revenue(self, document_name) -> RevenueDetails:
        if not str(document_name).strip():
            raise ValueError("Document filename is required")

        supplied_text = str(document_name).strip()
        supplied = Path(supplied_text)
        requested_name = supplied.name

        if supplied_text != requested_name and not supplied.is_absolute():
            raise ValueError(
                "Use a report filename or an allowed absolute path inside data/reports."
            )
        if Path(requested_name).suffix.lower() not in {".pdf", ".docx"}:
            raise ValueError("Only PDF and DOCX reports are allowed")

        report_root = REPORTS_DIR.resolve()
        if supplied.is_absolute():
            path = supplied.resolve()
            if path.parent != report_root or not path.is_file():
                raise FileNotFoundError("Document is not an allowed uploaded report")
        else:
            path = (report_root / requested_name).resolve()
            if path.parent != report_root:
                raise PermissionError("Access denied: report must be inside data/reports")
            if not path.is_file():
                raise FileNotFoundError(f"Report not found: {requested_name}")

        is_pdf = path.suffix.lower() == ".pdf"
        set_current_document(path)

        started = time.time()
        deadline = started + FILE_BUDGET_SECONDS
        print(f"[*] Starting Deep Revenue Agent: {path}", flush=True)

        task = (
            f"Extract and verify the latest fiscal-year revenue for report: '{requested_name}'.\n\n"
            f'<skill name="revenue-agent">\n{self.skill_body}\n</skill>\n\n'
            "Follow the workflow step-by-step:\n"
            "1. Call `inspect_document_outline()` to view bookmarks/TOC and locate the Financial Statements section.\n"
            "2. Call `scan_document_page_headers(start_page, end_page)` to locate the exact page containing the "
            "Consolidated Statement of Profit and Loss (or Income Statement).\n"
            "3. Immediately call `task(subagent_type='pdf-reader', description='Read page X...')` to have the subagent read the page and extract the financial figures.\n"
            "4. Review the subagent's extracted values and call `submit_revenue_details(...)` to submit the verified result."
        )

        notes = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            output = self._run(
                [{"role": "user", "content": task + notes}],
                deadline,
            )

            messages = output.get("messages", [])
            details = self._submission(messages)

            if details is not None:
                if not is_pdf:
                    details.source_pages = []

                print(
                    f"[*] Done in {int(time.time() - started)} seconds "
                    f"(attempt {attempt}).",
                    flush=True,
                )
                return details

            scanned = self._scanned(messages)
            read = self._pages_read(messages)
            error = self._last_error(messages) or "No successful submit_revenue_details call."

            print(
                f"[*] Attempt {attempt}/{MAX_ATTEMPTS} failed: {error}",
                flush=True,
            )

            if attempt == MAX_ATTEMPTS or time.time() > deadline:
                raise RuntimeError(
                    f"Revenue not verified. Last problem: {error} "
                    f"Page ranges scanned: {scanned}. Pages read: {read}."
                )

            unscanned_hint = self._unscanned_sections(path, scanned) if is_pdf else ""
            notes = (
                "\n\nA PREVIOUS ATTEMPT DID NOT PRODUCE A VERIFIED SUBMISSION.\n"
                f"Page ranges scanned in that attempt: {scanned or 'none'}.\n"
                f"Pages actually read so far: {read or 'none'}.\n"
                f"Last submission problem: {error}\n"
                + (
                    f"Bookmark sections not scanned in that attempt: {unscanned_hint}\n"
                    if unscanned_hint else ""
                )
                + "Do not repeat an already scanned range. Continue with an unscanned "
                "financial-statement candidate. Delegate page reading to pdf-reader, "
                "then review the evidence and submit."
            )

        raise RuntimeError("Revenue extraction ended without a verified result.")
