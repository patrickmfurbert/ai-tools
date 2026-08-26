# mcp-worker-dispatcher

An MCP server that dispatches tasks to local LLMs through the **ai-tools router**
in `../router`. It is intentionally *not* a router — it picks a model and fires a
task; all model-name → backend-URL routing is done by the router, which is a
separate, standalone project.

```
Claude Code  →  mcp-worker-dispatcher/server.py (MCP, stdio)  →  ../router (HTTP :8090)  →  llama.cpp backends
                 dispatch_task /                                  model name → URL
                 dispatch_tasks_parallel
```

## What it exposes

Three MCP tools over stdio:

- `dispatch_task(instruction, context, model="worker")` — send one task to a
  model through the router; returns the response text.
- `dispatch_tasks_parallel(tasks)` — send many tasks concurrently; results
  preserve input order, each tagged with the model and an ok/error status.
- `list_models()` — list the model names configured on the router and the
  backend URL each one maps to.

Each task dict for the parallel tool:
`{"instruction": str, "context": str (optional), "model": str (optional, default "worker")}`.

The `model` field is what selects the backend — e.g. `worker` for the fast 35B,
`orchestrator` for the larger model. The router owns that mapping.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `ROUTER_URL` | `http://localhost:8090` | Base URL of the ai-tools router. |
| `ROUTER_TIMEOUT_S` | `120` | Per-request HTTP timeout in seconds. |

The router itself is configured in `../router/config.yaml` — there is no
separate config for the dispatcher.

## Running

```bash
cd mcp-worker-dispatcher
source .venv/bin/activate
python server.py            # speaks MCP over stdio; Claude Code launches it
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

- Use the **absolute** path to the venv's `python` and `server.py` — Claude
  Code starts the server with no working directory of its own.
- If the router is not on `localhost:8090`, set `ROUTER_URL` accordingly.
- To scope it to a single project instead, put the same entry in that
  project's `.mcp.json`.

## Dependencies

- `mcp` (official Python SDK)
- `httpx`

Both are in `requirements.txt`. The router's own dependencies live with the
router.

## File layout

```
mcp-worker-dispatcher/
  server.py        # the MCP dispatcher
  README.md
  requirements.txt
```
