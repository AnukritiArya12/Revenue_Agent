"""Deep Revenue Agent: navigates a report like a human, using deepagents + Ollama."""

import time
from pathlib import Path
import inspect
from typing import Optional
import uuid

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import StateBackend
from deepagents.backends.utils import create_file_data
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.messages import ToolMessage
from langchain_ollama import ChatOllama
from langgraph.types import Command
from langgraph.checkpoint.memory import InMemorySaver

from config import MODEL_NAME, OLLAMA_BASE_URL, MODEL_TEMPERATURE, REPORTS_DIR
from agent.schemas import RevenueDetails
from agent.prompts import DEEP_AGENT_SYSTEM_PROMPT
from agent.tools import (
    inspect_document_outline,
    scan_document_page_headers,
    read_page,
    submit_revenue_details,
    set_current_document,
)
from tools.pdf_loader import get_document_outline


FILE_BUDGET_SECONDS = 30 * 60  # hard time budget per document
MAX_ATTEMPTS = 3

FILESYSTEM_PERMISSIONS = [
    # Read virtual navigation files.
    FilesystemPermission(
        operations=["read"],
        paths=["/navigation/**"],
        mode="allow",
    ),

    # Block all other reads and all file modifications.
    FilesystemPermission(
        operations=["read", "write"],
        paths=["/**"],
        mode="deny",
    ),
]


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

        # Include the skill instructions at the start of every attempt.
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

        # Fail early rather than silently running an incompatible middleware API.
        if "tools" not in inspect.signature(FilesystemMiddleware).parameters:
            raise RuntimeError(
                "Installed DeepAgents FilesystemMiddleware lacks the tools allowlist. "
                "Check that deepagents==0.7.32 is installed."
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
            middleware=[
                FilesystemMiddleware(tools=["read_file", "glob", "grep"])
            ],
            permissions=FILESYSTEM_PERMISSIONS,
            interrupt_on={
                "submit_revenue_details": True
            },
            checkpointer=InMemorySaver(),
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

    # ---------- agent run with HITL ----------

    def _run(self, messages, deadline, files=None):
        """Run the agent to graph completion and collect the final state."""

        seen = 0

        config = {
            "recursion_limit": 40,
            "configurable": {
                "thread_id": str(uuid.uuid4())
            }
        }

        try:
            result = self.agent.invoke(
                {
                    "messages": messages,
                    "files": files or {}
                },
                config=config,
            )

            while True:
                msgs = result.get("messages", [])

                for msg in msgs[seen:]:
                    calls = getattr(msg, "tool_calls", None)

                    if calls:
                        for c in calls:
                            print(
                                f"  -> {c.get('name')} "
                                f"{str(c.get('args'))[:150]}",
                                flush=True
                            )

                    elif isinstance(msg, ToolMessage):
                        print(
                            f"  <- {msg.name}: "
                            f"{self._text(msg.content)[:110]!r}",
                            flush=True,
                        )

                seen = len(msgs)

                # Check whether the graph is waiting for human approval.
                interrupts = result.get("__interrupt__", [])

                if not interrupts:
                    break

                for item in interrupts:
                    print("\n[HITL] Approval required:")
                    print(item.value)

                decision = input(
                    "\nApprove submit_revenue_details? (yes/no): "
                ).strip().lower()

                if decision in ("yes", "y"):
                    resume_value = {
                        "decisions": [
                            {"type": "approve"}
                        ]
                    }

                elif decision in ("no", "n"):
                    resume_value = {
                        "decisions": [
                            {
                                "type": "reject",
                                "message": "Human rejected this submission."
                            }
                        ]
                    }

                else:
                    print("Please enter yes or no.")
                    continue

                # Resume the same graph execution using the same thread_id.
                result = self.agent.invoke(
                    Command(resume=resume_value),
                    config=config,
                )

                if time.time() > deadline:
                    verified = self._submission(
                        result.get("messages", [])
                    )

                    if verified is None:
                        raise TimeoutError(
                            "Revenue agent exceeded the document time budget"
                        )

            return result

        except Exception as exc:
            print(
                f"[!] Agent step failed ({type(exc).__name__}): "
                f"{str(exc)[:200]}",
                flush=True
            )
            raise

    # ---------- main ----------

    def extract_revenue(self, document_name) -> RevenueDetails:
        # This CLI requires an exact report filename or an allowed absolute path.
        if not str(document_name).strip():
            raise ValueError("Document filename is required")

        requested = Path(str(document_name)).name

        if requested != str(document_name) and not Path(str(document_name)).is_absolute():
            raise ValueError(
                "Only report filenames or allowed absolute report paths are accepted"
            )

        if Path(requested).suffix.lower() not in {".pdf", ".docx"}:
            raise ValueError("Only PDF and DOCX reports are allowed")

        report_root = REPORTS_DIR.resolve()
        supplied = Path(str(document_name))

        if supplied.is_absolute():
            candidate = supplied.resolve()

            if candidate.parent != report_root or not candidate.is_file():
                raise FileNotFoundError(
                    "Document is not an allowed uploaded report"
                )

            path = candidate  # Already resolved: no glob call.

        else:
            # Exact filename: validate directly without an extra model call.
            path = (report_root / requested).resolve()

            if path.parent != report_root:
                raise PermissionError(
                    "Access denied: report must be inside data/reports"
                )

            if not path.is_file():
                raise FileNotFoundError(f"Report not found: {requested}")

        is_pdf = path.suffix.lower() == ".pdf"

        set_current_document(path)

        started = time.time()
        deadline = started + FILE_BUDGET_SECONDS

        print(f"[*] Starting Deep Revenue Agent: {path}", flush=True)

        task = (
            f"Document: {path}\n\n"
            f"<skill name=\"revenue-agent\">\n{self.skill_body}\n</skill>\n\n"
            "Follow the skill above and find the exact revenue of this document. "
            "Start with inspect_document_outline. Use built-in grep on /navigation/outline.txt "
            "when headings need locating; never grep report contents."
        )

        # Virtual navigation text only: no PDFs, credentials, or Python files.
        # The report itself remains accessible solely through existing document tools.
        outline = inspect_document_outline.invoke(
            {"document_name": str(path)}
        )

        navigation_files = {
            "/navigation/outline.txt": create_file_data(str(outline)[:16000]),
        }

        notes = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            output = self._run(
                [{"role": "user", "content": task + notes}],
                deadline,
                navigation_files
            )

            msgs = output.get("messages", [])
            details = self._submission(msgs)

            if details:
                if not is_pdf:
                    details.source_pages = []

                print(
                    f"[*] Done in {int(time.time() - started)} s "
                    f"(attempt {attempt}).",
                    flush=True
                )

                return details

            scanned = self._scanned(msgs)
            read = self._pages_read(msgs)
            error = self._last_error(msgs) or (
                "No successful submit_revenue_details call."
            )

            print(
                f"[*] Attempt {attempt}/{MAX_ATTEMPTS} failed: {error}",
                flush=True
            )

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
                + (
                    f"Bookmark sections not scanned yet: {hint}. "
                    if hint else ""
                )
                + "Do not repeat those scans. Table pages [T] in scanned ranges that "
                "you have not read are still candidates. Try a different range, "
                "then read the income statement page and submit."
            )