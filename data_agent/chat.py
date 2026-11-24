"""Chat with the data agent in the terminal, as one of the demo users.

    python -m data_agent.chat --user maria
    python -m data_agent.chat --user maria --show-tools     # also print every tool call

Needs ANTHROPIC_API_KEY. Without one, use Claude Desktop + the MCP server (docs/claude_desktop_setup.md).
"""
from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from data_agent.access import ROOT

load_dotenv(ROOT / ".env")

from data_agent.agent import Agent  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", default="maria", help="maria (West manager), dev (East manager), priya (VP), sam (support)")
    p.add_argument("--show-tools", action="store_true")
    a = p.parse_args()
    agent = Agent(a.user)
    who = agent.toolbox.whoami()
    print(f"Signed in as {who['name']} ({who['role']}, {who['data_scope']}). Ask a question, or 'quit'.\n")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("quit", "exit", ""):
            break
        r = agent.ask(q)
        if a.show_tools:
            for c in r.tool_calls:
                flag = f"  [{c['error_code']}]" if c["error_code"] else ""
                print(f"  · {c['tool']} {json.dumps(c['input'])[:160]}{flag}")
        print(f"\n[{r.outcome}] {r.answer}")
        for c in r.caveats:
            print(f"  note: {c}")
        print(f"  ({r.steps} steps, {r.input_tokens + r.output_tokens:,} tokens)\n")


if __name__ == "__main__":
    main()
