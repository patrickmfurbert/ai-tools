# Router Setup

The [`router/`](../router) is a single-file Python HTTP proxy that lets every
client on the laptop talk to one URL (`:8090`) and pick a backend by the
`model` field. Full API docs live in
[`router/README.md`](../router/README.md); this chapter covers *our*
deployment: config layout, the GBNF bug workarounds, and how to add models.

## Where things live

| File | Where | Role |
|---|---|---|
| `router.py` | `~/projects/ai-tools/router/router.py` (this repo, runs on the **laptop**) | The router itself (stdlib Python, single file) |
| `~/ai-tools/router/config.yaml` | runtime config (path resolution: `--config` flag → `ROUTER_CONFIG` env → `~/ai-tools/router/config.yaml`) |
| `~/ai-tools/router/responses.log` | debug log: first 4 KB of every response body, appended per request |

The router runs on the **laptop**, not the EVO-X2 — the backends are reached
through the mesh.

## Our config

`~/ai-tools/router/config.yaml`:

```yaml
host: 0.0.0.0
port: 8090
request_timeout_s: 300          # 300 s: deep-thinking first tokens can be slow
read_chunk_size: 4096
models:
  # Port 8080 — worker (fast execution)
  worker: "http://evo-x2:8080"
  # Port 8081 — orchestrator (main Qwen Code model)
  orchestrator: "http://evo-x2:8081"
  # Port 8082 — strategist (deep planning)
  strategist: "http://evo-x2:8082"
default_model: orchestrator
```

- URLs are **base URLs** — the router appends `/v1/messages` or
  `/v1/chat/completions` verbatim, so llama.cpp sees the path the client sent.
- `evo-x2` resolves via the mesh's DNS; `request_timeout_s` only bounds
  connect + first byte (streams keep flowing past it).
- `default_model: orchestrator` means any unknown model name lands on the
  orchestrator instead of 404-ing.

## Dual-format support: Anthropic *and* OpenAI

The router proxies exactly two endpoints and they carry tool definitions
differently:

| | OpenAI style | Anthropic style |
|---|---|---|
| Endpoint | `POST /v1/chat/completions` | `POST /v1/messages` |
| Tool schema key | `tools[].function.parameters` | `input_schema` |

The router understands **both formats natively** — `get_tool_schema()` in
`router.py` reads `fn.parameters` (OpenAI) *or* `input_schema`
(Anthropic-shaped), and the sanitizers below handle both shapes
(`t.function.parameters` vs `t.input_schema`). That's what lets Claude Code
(Anthropic-format) and Qwen Code / OpenAI SDKs (OpenAI-format) share one
router, and why the whole request — headers, streaming SSE included — is
proxied verbatim otherwise.

## GBNF bug fixes (the reason the router mangles your tools)

llama.cpp converts each tool's JSON schema into a **GBNF grammar** at request
time. Two llama.cpp grammar-compiler bugs bit us with Claude Code's tool
set; the router works around both on the way through.

### 1. `zoom_image` — permutation explosion

llama.cpp's GBNF compiler expands tools with many **optional** parameters
into all-permutation rule sets. Claude Code's `zoom_image` tool has 5
optional params (`file_path`, `x1`, `y1`, `x2`, `y2`) → 5! = **120
permutation rules**, and the grammar compile blows up (hang / OOM / failure)
on the model server.

Fix: the tool is blocked outright (`BLOCKED_TOOLS` in `router.py`) — stripped
from every request before it reaches a backend:

```python
BLOCKED_TOOLS: frozenset[str] = frozenset({"zoom_image"})
```

The router logs `blocked tools: zoom_image` when it strips it. If you
genuinely need image zoom, add the tool to your client's exclusion list
instead of sending it through this router.

### 2. `maxLength` dangling rules

The schema→GBNF converter emits `maxLength` string constraints as references
to helper rules; depending on schema shape those references end up
**dangling** — the emitted grammar references a rule the converter never
emitted, and compilation fails with an undefined-rule error. Rather than fix
the converter, the router recursively strips every `maxLength` key from every
tool schema before forwarding:

```python
def strip_max_length(schema):
    if isinstance(schema, dict):
        return {k: strip_max_length(v) for k, v in schema.items() if k != "maxLength"}
    ...
```

Consequence: a model may emit a tool argument longer than the schema's
`maxLength` — llama-side validation is gone; clients tolerate it fine.

Both fixes are visible in the router log: `blocked tools: ...` and
`stripped maxLength constraints from tool schemas`.

## Running it

```bash
# foreground
python3 ~/projects/ai-tools/router/router.py -v

# or in tmux (what strategist()/worker() auto-start):
tmux new-session -d -s ai-router 'python3 ~/projects/ai-tools/router/router.py -v'

# health check
curl -s http://localhost:8090/health      # → ok
```

Startup log prints the routing table:

```
router listening on 0.0.0.0:8090  (config=/home/pastrycak3s/ai-tools/router/config.yaml)
  worker                           -> http://evo-x2:8080
  orchestrator                     -> http://evo-x2:8081
  strategist                       -> http://evo-x2:8082
  default model: orchestrator
```

## Adding a model

1. Start a backend (on the EVO-X2) that listens on a free port — copy one of
   the [scripts/](scripts/) and change `-m`/`--port`.
2. Add a name→URL line to `config.yaml`:

   ```yaml
   models:
     ...
     coder: "http://evo-x2:8083"
   ```

3. Restart the router (`tmux kill-session -t ai-router` then start it again —
   or let `strategist` restart it). No code change needed.
4. Use the name: `curl localhost:8090/v1/chat/completions -d '{"model": "coder", ...}'`,
   or add a matching `strategist()`-style shell function
   ([client-setup.md](client-setup.md)).

The MCP dispatcher (`mcp-worker-dispatcher/`, see
[its README](../mcp-worker-dispatcher/README.md)) picks new names up
automatically via `list_models()`, which reads this same config file.
