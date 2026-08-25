# mcp-worker-dispatcher

Two small Python tools for routing Claude Code requests to local llama.cpp
models behind the [ai-tools router](../router):

1. **`router.py`** — a lightweight, OpenAI-compatible HTTP router. One endpoint
   Claude Code points at; it reads the `model` field from each request and
   proxies to the matching llama.cpp backend, streaming responses back
   transparently.

2. **`server.py`** — an [MCP](https://modelcontextprotocol.io) server (stdio
   transport) that exposes the router as tools Claude Code can call directly
   (`dispatch_task`, `dispatch_tasks_parallel`, `list_models`).

Pick whichever fits your workflow — or use both: point Claude Code at the HTTP
router for normal completions, and add the MCP server when you want to fan
tasks out to multiple backends in parallel from inside a conversation.

---

## 1. HTTP router (`router.py`)

### Endpoints

| Method | Path | Behaviour |
|---|---|---|
| `GET` | `/v1/models` | List configured models and their backend URLs. |
| `POST` | `/v1/chat/completions` | Proxy to the backend mapped to the request's `model` field. |
| `POST` | `/v1/messages` | Same, Anthropic-style path. |
| *anything else* | — | `404`. |

Unknown model names return a JSON `404` listing the valid names (unless
`default_model` is set, in which case the request falls back to it). Backend
connection errors return `502`; timeouts return `504`.

### Setup

```bash
cd ~/projects/ai-tools/mcp-worker-dispatcher

# With uv (recommended — python3-venv/ensurepip is often missing on Ubuntu):
uv venv .venv --python 3.12
uv pip install -r requirements.txt --python .venv/bin/python

# Or with pip, if python3-venv is installed:
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### Config

The model-to-backend mapping lives in a YAML file. The default lookup order:

1. `--config /path/to/config.yaml` (CLI flag)
2. `$ROUTER_CONFIG` (env var)
3. `~/ai-tools/router/config.yaml`

Copy the sample and edit it:

```bash
cp config.yaml ~/ai-tools/router/config.yaml
```

Sample (`config.yaml` in this directory):

```yaml
host: 0.0.0.0
port: 8090
request_timeout_s: 120

models:
  # 27B orchestrator model — decomposition / synthesis.
  orchestrator: "http://evo-x2:8080"
  # 35B worker model — executing individual subtasks.
  worker: "http://evo-x2:8081"

# Fall back to this model on unknown names (null = 404 instead).
default_model: null
```

### Run

```bash
.venv/bin/python router.py
# or with overrides:
.venv/bin/python router.py --port 9090 --config /path/to/config.yaml -v
```

Logs go to stderr and show every routing decision:

```
15:34:20  INFO  router  model=orchestrator  -> http://evo-x2:8080/v1/chat/completions  (91 bytes)
15:34:20  INFO  router  model=worker        -> http://evo-x2:8081/v1/chat/completions  (84 bytes)
```

### Point Claude Code at it

In `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8090",
    "ANTHROPIC_MODEL": "orchestrator"
  }
}
```

Claude Code sends its requests to `ANTHROPIC_BASE_URL`; the router forwards
them based on the `model` field. (The router proxies the request body
unchanged, so whatever shape Claude Code sends is what the llama.cpp backend
receives — make sure the backends speak the same protocol, e.g. via an
llama.cpp build with the Anthropic-compatible `/v1/messages` endpoint.)

---

## 2. MCP server (`server.py`)

### Tools

| Tool | Description |
|---|---|
| `dispatch_task(instruction, context, model="worker")` | Send one task to one model through the router. Returns the response text. |
| `dispatch_tasks_parallel(tasks)` | Send many tasks **concurrently** (asyncio). `tasks` is a list of `{"instruction", "context", "model"}` dicts. Returns results in input order; each result is `{"ok": true, "model", "response"}` or `{"ok": false, "model", "error"}`. |
| `list_models()` | Query the router's `/v1/models`. Returns `{"router_url", "models": {name: backend_url}}`. |

Error handling: a failed task in a parallel batch returns an error dict for
that task — the rest of the batch still completes. A failed single-task call
or router connection error is returned to the client as an MCP error result
(`isError`), not a server crash.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ROUTER_URL` | `http://localhost:8090` | Base URL of the HTTP router. |
| `ROUTER_TIMEOUT_S` | `120` | Per-request HTTP timeout in seconds. |

### Adding it to Claude Code

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

- Use the **absolute** path to the venv's `python` and `server.py` — Claude
  Code starts the server with no working directory of its own.
- If the router is not on `localhost:8090`, set `ROUTER_URL` accordingly
  (e.g. `http://192.168.1.20:8090` or another port).
- To scope it to a single project instead, put the same `mcpServers` block in
  that project's `.mcp.json`.

After editing the config, restart Claude Code (or run `/mcp` in an existing
session to re-check status). You should see `worker-dispatcher` listed with its
three tools.

### Example usage (standalone, no Claude Code)

Drive the MCP server directly to verify it works:

```bash
cd ~/projects/ai-tools/mcp-worker-dispatcher
.venv/bin/python - <<'EOF'
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command=".venv/bin/python",
        args=["server.py"],
        cwd=".",
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()

            print([t.name for t in (await session.list_tools()).tools])

            res = await session.call_tool("dispatch_task", {
                "instruction": "Summarize this in one sentence: "
                               "the router forwards requests by model name.",
                "context": "You are a concise summarizer.",
                "model": "worker",
            })
            print(res.content[0].text)

            # A failed task (bad model) returns an error dict without
            # killing the batch.
            res = await session.call_tool("dispatch_tasks_parallel", {
                "tasks": [
                    {"instruction": "Say OK.", "model": "worker"},
                    {"instruction": "This should fail.", "model": "no-such-model"},
                ],
            })
            print(res.content[0].text)

            res = await session.call_tool("list_models", {})
            print(res.content[0].text)

asyncio.run(main())
EOF
```

### Verify in Claude Code

Run `/mcp` and confirm `worker-dispatcher` is connected, then try:

> Use the worker-dispatcher tools to list the available models.

or

> Dispatch the task "What is 2+2?" to the worker model and tell me the answer.

---

## Files

- `router.py` — the HTTP router (single file; httpx + PyYAML + uvicorn)
- `server.py` — the MCP server (single file; mcp SDK + httpx)
- `config.yaml` — sample router config
- `requirements.txt` — `mcp`, `httpx`, `PyYAML`, `uvicorn`
