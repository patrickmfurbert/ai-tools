# ai-tools router

A lightweight HTTP router that sits between an LLM client (such as Claude
Code) and multiple llama.cpp backend servers. It reads the `model` field
from the request body and forwards the whole request — headers, body,
streaming included — to the matching backend, then streams the response
back to the caller unchanged.

## Why

llama.cpp's `llama-server` exposes one model per instance. If you want a
27B orchestrator on one GPU and a 35B worker on another, you run two
servers on two ports. But your client (Claude Code, an SDK, a script) only
wants to talk to one URL and pick a model by name. The router is that one
URL.

```
            ┌──────────────┐
  client ──►│  ai-tools    │──►  <server_url>  (larger model)
  (Claude   │  router      │
   Code)    │  :8090       │──►  <server_url>  (smaller/faster model)
            └──────────────┘
```

## Quick start

```bash
# 1. Edit the config to point at your backends.
$EDITOR ~/ai-tools/router/config.yaml

# 2. Run the router.
python3 ~/ai-tools/router/router.py

# 3. Point your client at http://localhost:8090 and use the model names
#    from the config in the `model` field of each request.
```

## Configuration

The config is YAML by default (TOML works too). The path is resolved in
this order:

1. `--config /path/to/file.yaml` (CLI flag)
2. `ROUTER_CONFIG=/path/to/file.yaml` (environment variable)
3. `~/ai-tools/router/config.yaml` (default)

A complete example is in [`config.yaml`](config.yaml). The important
section is `models` — replace each `<server_url>` with the address of a
llama.cpp backend:

```yaml
models:
  # a larger model for decomposition / synthesis
  orchestrator: "http://localhost:8080"
  # a smaller, faster model for executing individual subtasks
  worker: "http://192.168.1.20:8081"

# Optional fallback for unknown model names. Omit or set to null to 404.
default_model: orchestrator
```

The URL is the base of the llama.cpp server. The router appends the
request path (`/v1/messages` or `/v1/chat/completions`) verbatim, so the
backend sees the same path your client sent.

### Other config keys

| Key | Default | Meaning |
|-----|---------|---------|
| `host` | `0.0.0.0` | Bind address for the router. |
| `port` | `8090` | Bind port. Overridable via `--port`. |
| `request_timeout_s` | `120` | Seconds to wait for the backend's first byte. Streaming responses keep flowing past this; it only bounds connect + first-token time. |
| `read_chunk_size` | `4096` | Bytes read from the backend per iteration while streaming. |
| `models` | — | Map of model name → backend base URL. Required. |
| `default_model` | null | Fallback model for unknown names. Must be a key in `models`. |

## Endpoints

The router proxies exactly two paths; everything else returns 404:

- `POST /v1/messages` (Anthropic-style)
- `POST /v1/chat/completions` (OpenAI-style)

Plus:

- `GET /health` → `ok` (for load balancers / uptime checks)

Requests that lack a `model` field, or whose model matches no configured
entry (and no `default_model`), get a JSON error with the list of known
models.

## Streaming

If the backend responds with `content-type: text/event-stream` (or any
`transfer-encoding`), the router streams chunks through to the client as
they arrive and closes the connection when the stream ends. Non-streaming
responses are buffered so the router can set `content-length`.

This is what makes it transparent to Claude Code: the client doesn't know
(or care) that a different backend answered.

## Logging

Every request logs two lines — the route decision and the completion:

```
13:24:44  INFO   router  [1] POST /v1/messages  model=orchestrator  ->  http://localhost:8080  (93 bytes)
13:24:44  INFO   router  [1] /v1/messages done  status=200  body=172 bytes  elapsed=70ms
```

The `[N]` is a per-process request id you can use to correlate the two
lines. Run with `-v` for debug output (including inbound HTTP details).

## Error handling

| Condition | HTTP | Body |
|-----------|------|------|
| Unknown path | 404 | `{"error": "path ... is not proxied; supported: [...]"}` |
| Missing / bad `model` field | 400 | `{"error": "request body must include a string 'model' field"}` |
| Unknown model, no default | 404 | `{"error": "no backend configured for model ...; known models: ..."}` |
| Backend unreachable | 502 | `{"error": "upstream unreachable: ...", "backend": "..."}` |
| Backend returns non-2xx | passthrough | The backend's status and body are forwarded unchanged |
| Client disconnects mid-stream | — | Logged as a warning; the upstream connection is closed |

## Running in production

The router uses Python's `ThreadingHTTPServer` — one thread per
connection, no async. For a single-user workstation routing a few
concurrent requests this is fine. For higher concurrency, put it behind
`systemd` (or run it under a process supervisor) and consider a reverse
proxy in front.

A minimal systemd unit:

```ini
[Unit]
Description=ai-tools LLM router
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/USER/ai-tools/router/router.py
Restart=on-failure
RestartSec=2
# Optionally: Environment=ROUTER_CONFIG=/etc/ai-tools/router.yaml

[Install]
WantedBy=multi-user.target
```

## Dependencies

- Python 3.10+ (uses `tomllib` for TOML configs; stdlib `urllib` for
  upstream requests)
- PyYAML — only needed for YAML configs. For TOML-only setups the router
  runs on stdlib alone.

```bash
pip install pyyaml   # if you use YAML configs (the default)
```

## File layout

```
router/
  router.py        # the router (single file, no package)
  config.yaml      # sample config (copied to ~/ai-tools/router/config.yaml)
  README.md        # this file
```
