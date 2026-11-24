"""MCP server: lets Claude Desktop (or any MCP client) query the warehouse as one specific user.

Run by Claude Desktop, configured in claude_desktop_config.json (see docs/claude_desktop_setup.md):
    python -m mcp_server.server        with env DATA_AGENT_USER=maria | dev | priya | sam

The user is fixed when the server starts, the way a real deployment would take it from the
person's login. Nothing the model sends can change it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so `data_agent` imports when launched by path

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

from data_agent.tools import INSTRUCTIONS, Toolbox  # noqa: E402

USER = os.getenv("DATA_AGENT_USER", "maria")
toolbox = Toolbox(USER)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

server = MCPServer(name=f"harborline-data ({USER})", instructions=INSTRUCTIONS)


@server.tool(annotations=READ_ONLY)
def whoami() -> dict:
    """Who the current user is, their role, which regions they can see, and which columns are hidden from them."""
    return toolbox.whoami()


@server.tool(annotations=READ_ONLY)
def list_tables() -> dict:
    """Tables and columns this user can query, with a description of each table."""
    return toolbox.list_tables()


@server.tool(annotations=READ_ONLY)
def get_metric_definitions() -> dict:
    """Agreed business definitions (revenue, active customer, resolution time...), the as_of date that
    'this year' / 'last month' are relative to, and what data is NOT available. Read before computing metrics."""
    return toolbox.get_metric_definitions()


@server.tool(annotations=READ_ONLY)
def run_query(sql: str) -> dict:
    """Run ONE read-only SQLite SELECT against the warehouse tables (customers, prospects, reps, products,
    orders, order_lines, tickets, data_quality). Results are limited to this user's access and to 200 rows;
    aggregate in SQL. Returns columns, rows, and an error_code/message if the query was refused."""
    return toolbox.run_query(sql)


@server.tool(annotations=READ_ONLY)
def get_data_quality_notes(issue_type: str | None = None, limit: int = 20) -> dict:
    """Known data problems found while combining the ERP, CRM and support system (region conflicts,
    duplicates merged, unmatched tickets, missing values) and how each was resolved. Optionally filter by
    issue_type, e.g. REGION_CONFLICT."""
    return toolbox.get_data_quality_notes(issue_type, limit)


if __name__ == "__main__":
    server.run()  # stdio, which is what Claude Desktop uses
