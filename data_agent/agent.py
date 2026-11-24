"""API agent: Claude + the same five tools, plus a final_answer tool so every answer is structured
(outcome, value, caveats, SQL). Used by the CLI chat and by the answer-accuracy eval.

Needs ANTHROPIC_API_KEY. The model is AGENT_MODEL (default claude-sonnet-5-5).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from data_agent.tools import INSTRUCTIONS, Toolbox

MAX_STEPS = 10
MAX_TOOL_RESULT_CHARS = 20_000

FINAL_RULE = """
Finish EVERY reply by calling final_answer exactly once:
- outcome "answer": you answered from query results. Put the single headline number (if there is one) in value.
- outcome "clarify": the question is ambiguous; answer holds your one clarifying question.
- outcome "cannot_answer": the data needed doesn't exist in the warehouse.
- outcome "refused": the user asked for data outside their access (another region, a hidden column).
List data-quality caveats that affect the answer in caveats, and every SQL statement you relied on in sql.
"""

TOOLS = [
    {"name": "whoami", "description": "Current user's role, region scope and hidden columns.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_tables", "description": "Tables and columns this user can query.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_metric_definitions",
     "description": "Agreed metric definitions, the as_of date, and what data is not available.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "run_query", "description": "Run ONE read-only SQLite SELECT. Limited to the user's access, 200 rows.",
     "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "get_data_quality_notes", "description": "Known data conflicts and gaps, optionally by issue_type.",
     "input_schema": {"type": "object", "properties": {"issue_type": {"type": "string"},
                                                       "limit": {"type": "integer"}}}},
    {"name": "final_answer", "description": "Deliver the final answer to the user. Call exactly once, last.",
     "input_schema": {"type": "object", "properties": {
         "outcome": {"type": "string", "enum": ["answer", "clarify", "cannot_answer", "refused"]},
         "answer": {"type": "string", "description": "What the user reads. Plain language, include the SQL used."},
         "value": {"type": ["number", "string", "null"], "description": "Headline value, e.g. 1234567.89"},
         "caveats": {"type": "array", "items": {"type": "string"}},
         "sql": {"type": "array", "items": {"type": "string"}}},
         "required": ["outcome", "answer"]}},
]


@dataclass
class AgentResult:
    outcome: str
    answer: str
    value: float | str | None = None
    caveats: list[str] = field(default_factory=list)
    sql: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class Agent:
    def __init__(self, user_id: str, client=None, model: str | None = None, **session_kw):
        self.toolbox = Toolbox(user_id, **session_kw)
        if client is None:
            import anthropic
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError("Set ANTHROPIC_API_KEY (in .env or your shell) to use the API agent. "
                                   "No key? Use Claude Desktop with the MCP server instead.")
            client = anthropic.Anthropic()
        self.client = client
        self.model = model or os.getenv("AGENT_MODEL", "claude-sonnet-5-5")
        self.messages: list[dict] = []  # kept across ask() calls, so clarifying questions work in chat

    def _call_tool(self, name: str, args: dict) -> dict:
        tb = self.toolbox
        if name == "run_query":
            return tb.run_query(args.get("sql", ""))
        if name == "get_data_quality_notes":
            return tb.get_data_quality_notes(args.get("issue_type"), args.get("limit", 20))
        if name in ("whoami", "list_tables", "get_metric_definitions"):
            return getattr(tb, name)()
        return {"error": f"Unknown tool {name}"}

    def ask(self, question: str) -> AgentResult:
        self.messages.append({"role": "user", "content": question})
        result = AgentResult(outcome="error", answer="The agent stopped without answering.")
        for step in range(1, MAX_STEPS + 1):
            resp = self.client.messages.create(model=self.model, max_tokens=2000, system=INSTRUCTIONS + FINAL_RULE,
                                               tools=TOOLS, messages=self.messages)
            result.steps = step
            usage = getattr(resp, "usage", None)
            result.input_tokens += getattr(usage, "input_tokens", 0) or 0
            result.output_tokens += getattr(usage, "output_tokens", 0) or 0
            self.messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:  # model answered in plain text without final_answer
                text = "".join(getattr(b, "text", "") for b in resp.content)
                result.outcome, result.answer = "answer", text
                return result
            results = []
            for tu in uses:
                if tu.name == "final_answer":
                    a = tu.input
                    result.outcome, result.answer = a.get("outcome", "answer"), a.get("answer", "")
                    result.value, result.caveats, result.sql = a.get("value"), a.get("caveats") or [], a.get("sql") or []
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "delivered"})
                    self.messages.append({"role": "user", "content": results})
                    return result
                out = self._call_tool(tu.name, tu.input or {})
                result.tool_calls.append({"tool": tu.name, "input": tu.input,
                                          "error_code": out.get("error_code") if isinstance(out, dict) else None})
                text = json.dumps(out, default=str)[:MAX_TOOL_RESULT_CHARS]
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": text})
            self.messages.append({"role": "user", "content": results})
        return result
