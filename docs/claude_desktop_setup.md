# Use the Scoped SQL Agent in Claude Desktop (no API key)

Claude Desktop starts the MCP server on your Mac and uses its 5 tools to answer your questions. You choose which user it runs as.

## 1. Install the project (once)

In Terminal, from the `scoped-sql-agent` folder:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pwd
```
Copy the path `pwd` prints, e.g. `/Users/you/Documents/scoped-sql-agent`. You need it below.

## 2. Add the server to Claude Desktop

1. Open Claude Desktop → **Settings** → **Developer** → **Edit Config**. This opens `claude_desktop_config.json` (on a Mac it's in `~/Library/Application Support/Claude/`).
2. Paste the following, replacing `/Users/you/Documents/scoped-sql-agent` with your path in both places. If the file already has an `mcpServers` section, add the `harborline-maria` entry inside it.

```json
{
  "mcpServers": {
    "harborline-maria": {
      "command": "/Users/you/Documents/scoped-sql-agent/.venv/bin/python",
      "args": ["/Users/you/Documents/scoped-sql-agent/mcp_server/server.py"],
      "env": { "DATA_AGENT_USER": "maria" }
    }
  }
}
```

3. Save, then **quit Claude Desktop completely (Cmd+Q)** and reopen it. Closing the window isn't enough.
4. In a new chat, open the tools menu in the message box. You should see **harborline-maria** with 5 tools.

## 3. Try it as Maria (West regional manager)

- How many active customers do I have?
- What's my revenue so far this year, and how does it compare with the same period last year?
- Which of my customers haven't ordered in the last 6 months?
- What was the East region's revenue in 2026? *(should be refused: outside her access)*
- Show me contact emails for my top customers. *(refused: hidden column)*
- What was our gross margin? *(no cost data exists, so it should say so)*

## 4. Switch users

Change `"DATA_AGENT_USER"` to `dev`, `priya` or `sam` (and rename the entry to match), then restart Claude Desktop. Asking the same question as Maria and then as Priya shows the permissions working.

Run **one user at a time**. If two entries are enabled together, Claude can pick either user's tools. In a real deployment, the user would come from the person's login, not from a setting they can change.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Server not listed | Check the JSON is valid (no trailing commas) and both paths are absolute. Quit with Cmd+Q and reopen. |
| "Server disconnected" / failed to start | Run the same command in Terminal: `DATA_AGENT_USER=maria /your/path/.venv/bin/python /your/path/mcp_server/server.py`. It should sit waiting with no error (Ctrl+C to stop). If it prints `ModuleNotFoundError`, the packages weren't installed in that `.venv`. |
| "warehouse.db not found" | Run `python -m pipeline.build_warehouse` from the project folder. |
| Logs | Settings → Developer shows each server's log. Every query is also written to `logs/query_log.jsonl`. |
