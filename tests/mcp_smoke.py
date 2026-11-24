"""Start the MCP server exactly as Claude Desktop would (a subprocess over stdio) and call its tools.

Run from the project folder:  python -m tests.mcp_smoke [user]
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def main(user: str = "maria") -> dict:
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server" / "server.py")],
                                   env={"DATA_AGENT_USER": user}, cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            who = json.loads(text_of(await session.call_tool("whoami", {})))
            ok = json.loads(text_of(await session.call_tool(
                "run_query", {"sql": "SELECT region, COUNT(*) AS n FROM orders GROUP BY region"})))
            bad = json.loads(text_of(await session.call_tool("run_query", {"sql": "DELETE FROM orders"})))
            return {"server": init.server_info.name, "tools": names, "user": who["user"],
                    "scope": who["data_scope"], "orders_by_region": ok["rows"], "delete": bad["error_code"],
                    "instructions_sent": bool(init.instructions)}


if __name__ == "__main__":
    out = asyncio.run(main(*(sys.argv[1:2] or ["maria"])))
    print(f"Server        {out['server']}  (started over stdio, like Claude Desktop)")
    print(f"Tools         {', '.join(out['tools'])}")
    print(f"Signed in as  {out['user']}, {out['scope']}")
    print(f"run_query     orders by region -> {out['orders_by_region']}")
    print(f"run_query     DELETE FROM orders -> {out['delete']}")
