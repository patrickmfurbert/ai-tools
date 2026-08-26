# mcp-worker-dispatcher

An [MCP](https://modelcontextprotocol.io) server (Python, stdio transport) that
lets Claude Code — or any MCP client — dispatch tasks to the local models
behind the [ai-tools router](../router).

The router is an OpenAI-compatible HTTP front-end for llama.cpp backends. This
server is a thin async client on top of it: it turns tool calls into
`/v1/chat/completions` requests and returns the text.

## Tools

| Tool | Description |
|---|---|
| `dispatch_task(instruction, context, model="worker")` | Send one task to one model. Returns the response text. |
| `dispatch_tasks_parallel(tasks)` | Send many tasks **concurrently** (asyncio). `tasks` is a list of `{"instruction", "context", "model"}` dicts. Returns results in input order; each result is `{"ok": true, "model", "response"}` or `{"ok": false, "model", "error"}`. |
| `list_models()` | Returns the model names the router is configured with (from `ROUTER_CONFIG`), or queries the router's `/v1/models` endpoint if one is available. |

Error handling: a single failed task in a parallel batch returns an error dict
for that task — the rest of the batch still completes. A failed single-task
call or a router connection error is returned to the client as an MCP error
result (`is_error`), not a server crash.

> **Note on `list_models`:** the current router in this repo only proxies
> `/v1/messages` and `/v1/chat/completions` — it does not implement
> `/v1/models`. This server therefore falls back to reading the router's
> `config.yaml` (the `models:` section maps name → backend URL, which is
> exactly what this tool reports). If your router does implement
> `/v1/models`, that endpoint is used automatically instead.

## Setup

`mcp-worker-dispatcher/` gets its own venv (created with `uv`; plain
`pip install -r requirements.txt` works too if you prefer):

```bash
cd ~/projects/ai-tools/mcp-worker-dispatcher
uv venv .venv
uv pip install -r requirements.txt --python .venv/bin/python
```

Make sure the router is running first (see `../router/README.md`):

```bash
cd ~/projects/ai-tools/router
python3 router.py        # listens on :8090 by default
```

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ROUTER_URL` | `http://localhost:8090` | Base URL of the router. |
| `ROUTER_TIMEOUT_S` | `120` | Per-request HTTP timeout in seconds. |
| `ROUTER_CONFIG` | `~/projects/ai-tools/router/config.yaml` | Router config file, used by `list_models()` as a fallback for `/v1/models`. |

## Example usage (standalone, no Claude Code)

Drive the server directly with an MCP client (save as `example_client.py`):

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command=".venv/bin/python",
        args=["server.py"],
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()

            print([t.name for t in (await session.list_tools()).tools])

            # Single task
            res = await session.call_tool("dispatch_task", {
                "instruction": "Say OK.",
                "context": "You are a concise assistant.",
                "model": "worker",
            })
            print(res.content[0].text)

            # Parallel batch — the bad model returns an error dict without
            # killing the rest of the batch.
            res = await session.call_tool("dispatch_tasks_parallel", {
                "tasks": [
                    {"instruction": "Say OK."},
                    {"instruction": "Say OK in haiku form.", "model": "worker"},
                    {"instruction": "This should fail.", "model": "no-such-model"},
                ],
            })
            print(res.content[0].text)

            # What models does the router know about?
            res = await session.call_tool("list_models", {})
            print(res.content[0].text)

asyncio.run(main())
```

Run it from the `mcp-worker-dispatcher/` directory:

```bash
.venv/bin/python example_client.py
```

## Adding it to Claude Code

Add to `~/.claude/settings.json` (user-level, available in every project):

```json
{
  "mcpServers": {
    "worker-dispatcher": {
      "command": "/home/pastrycak3s/projects/ai-tools/mcp-worker-dispatcher/.venv/bin/python",
      "args": ["/home/pastrycak3s/projects/ai-tools/mcp-worker-dispatcher/server.py"],
      "env": {
        "ROUTER_URL": "http://localhost:8090"
      }
    }
  }
}
```

Notes:

- Use **absolute** paths for `command` and `args` — Claude Code starts the
  server without a working directory of its own.
- If the router isn't on `localhost:8090`, set `ROUTER_URL` accordingly
  (e.g. `http://192.168.1.20:8090` or a different port).
- Restart Claude Code (or run `/mcp`) after editing the config to pick up
  the new server.
- Alternatively, `claude mcp add worker-dispatcher -- /abs/path/.venv/bin/python /abs/path/server.py`
  registers the same thing non-interactively.
