#!/usr/bin/env python3
"""A lightweight HTTP router for llama.cpp backends.
Sits between an LLM client (e.g. Claude Code) and one or more llama.cpp
server instances. Reads the ``model`` field from the request body and
forwards the entire request — headers, body, streaming included — to the
matching backend, then streams the response back to the caller unchanged.
Only ``/v1/messages`` (Anthropic-style) and ``/v1/chat/completions``
(OpenAI-style) are handled. Everything else is rejected with a 404.
Usage:
    python router.py                          # port 8090, default config
    python router.py --port 9000 --config /tmp/c.yaml
    python router.py --rewrite-context-errors  # translate llama.cpp context-
                                               # overflow 400s into Anthropic shape
    ROUTER_CONFIG=/tmp/c.yaml python router.py
The config path resolution order is:
    1. --config CLI flag
    2. ROUTER_CONFIG environment variable
    3. ~/ai-tools/router/config.yaml
The router is intentionally small: stdlib HTTP server, stdlib json/http
client, PyYAML only for parsing YAML config (toml configs use the stdlib
tomllib). No web frameworks, no async, no dependency surprises.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("router")
RESPONSE_LOG_PATH = Path.home() / "ai-tools" / "router" / "responses.log"

def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(fmt)
    log.handlers.clear()
    log.addHandler(handler)
    log.setLevel(level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

def log_response_body(req_id: int, model: str, path: str, body: bytes) -> None:
    """Append response body to a debug log file for inspection."""
    try:
        RESPONSE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RESPONSE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"\n{'='*70}\n")
            f.write(f"[{req_id}] {time.strftime('%H:%M:%S')}  model={model}  path={path}\n")
            f.write(f"{'='*70}\n")
            f.write(body[:4000].decode("utf-8", errors="replace"))
            if len(body) > 4000:
                f.write(f"\n... ({len(body) - 4000} bytes truncated)")
            f.write("\n")
    except Exception as exc:
        log.warning("[%d] failed to write response log: %s", req_id, exc)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    """Raised when the config file is missing, unparseable, or invalid."""

@dataclass
class RouterConfig:
    """Resolved router configuration."""
    host: str
    port: int
    models: dict[str, str] = field(default_factory=dict)
    default_model: str | None = None
    request_timeout_s: float = 120.0
    read_chunk_size: int = 4096
    rewrite_context_errors: bool = False
    prompt_guards: dict[str, int] = field(default_factory=dict)
    source_path: Path | None = None

    def resolve_backend(self, model: str) -> str | None:
        if model in self.models:
            return self.models[model]
        if self.default_model is not None and self.default_model in self.models:
            return self.models[self.default_model]
        return None

def load_config(path: Path) -> RouterConfig:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    data: dict[str, Any]
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigError(
                "PyYAML is required for YAML configs. "
                "Install it with `pip install pyyaml` or use a .toml file."
            ) from exc
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    elif suffix == ".toml":
        import tomllib  # noqa: PLC0415
        try:
            data = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    else:
        raise ConfigError(
            f"unsupported config extension {suffix!r} for {path}; "
            "use .yaml, .yml, or .toml"
        )
    if not isinstance(data, dict):
        raise ConfigError(f"top level of {path} must be a mapping")
    models_raw = data.get("models", {})
    if not isinstance(models_raw, dict):
        raise ConfigError("'models' must be a mapping of model name -> backend URL")
    models: dict[str, str] = {}
    for name, url in models_raw.items():
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            if url == "<server_url>":
                raise ConfigError(
                    f"model {name!r} still has the placeholder '<server_url>' — "
                    "replace it with a real backend URL in the config"
                )
            raise ConfigError(
                f"model {name!r} maps to {url!r}; backend URLs must start with http:// or https://"
            )
        models[str(name)] = url.rstrip("/")
    host = data.get("host", "0.0.0.0")
    if not isinstance(host, str):
        raise ConfigError("'host' must be a string")
    port = data.get("port", 8090)
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("'port' must be an integer between 1 and 65535")
    default_model = data.get("default_model")
    if default_model is not None and default_model not in models:
        raise ConfigError(
            f"default_model {default_model!r} is not in the models mapping"
        )
    request_timeout_s = data.get("request_timeout_s", 120.0)
    if not isinstance(request_timeout_s, (int, float)) or request_timeout_s <= 0:
        raise ConfigError("'request_timeout_s' must be a positive number")
    read_chunk_size = data.get("read_chunk_size", 4096)
    if not isinstance(read_chunk_size, int) or read_chunk_size <= 0:
        raise ConfigError("'read_chunk_size' must be a positive integer")
    return RouterConfig(
        host=host,
        port=port,
        models=models,
        default_model=default_model,
        request_timeout_s=float(request_timeout_s),
        read_chunk_size=read_chunk_size,
        source_path=path,
    )

def resolve_config_path(flag: str | None) -> Path:
    if flag:
        return Path(flag).expanduser()
    env = os.environ.get("ROUTER_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / "ai-tools" / "router" / "config.yaml"

# ---------------------------------------------------------------------------
# Tool schema sanitisation
# ---------------------------------------------------------------------------
BLOCKED_TOOLS: frozenset[str] = frozenset({"zoom_image"})

def get_tool_schema(fn: dict) -> dict:
    """Get tool schema from either OpenAI (parameters) or Anthropic (input_schema) format."""
    return fn.get("parameters") or fn.get("input_schema") or {}

def strip_max_length(schema: Any) -> Any:
    """Recursively remove maxLength constraints from a JSON schema."""
    if isinstance(schema, dict):
        return {
            k: strip_max_length(v)
            for k, v in schema.items()
            if k != "maxLength"
        }
    if isinstance(schema, list):
        return [strip_max_length(v) for v in schema]
    return schema

def sanitise_tools(parsed: dict) -> tuple[dict, list[str], bool]:
    """Remove blocked tools and strip maxLength from all tool schemas.
    Handles both OpenAI format (parameters) and Anthropic format (input_schema).
    """
    tools = parsed.get("tools")
    if not tools:
        return parsed, [], False
    filtered = []
    removed = []
    schemas_modified = False
    for t in tools:
        fn = t.get("function", t)
        name = fn.get("name", "")
        if name in BLOCKED_TOOLS:
            removed.append(name)
            continue
        schema_key = "parameters" if "parameters" in fn else (
            "input_schema" if "input_schema" in fn else None
        )
        if schema_key:
            params = fn.get(schema_key)
            if params:
                cleaned = strip_max_length(params)
                if cleaned != params:
                    schemas_modified = True
                    t = dict(t)
                    if "function" in t:
                        t["function"] = dict(t["function"])
                        t["function"][schema_key] = cleaned
                    else:
                        t[schema_key] = cleaned
        filtered.append(t)
    if removed or schemas_modified:
        parsed = dict(parsed)
        parsed["tools"] = filtered
    return parsed, removed, schemas_modified

# ---------------------------------------------------------------------------
# Upstream proxying
# ---------------------------------------------------------------------------
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)

class UpstreamError(Exception):
    """Raised when the backend is unreachable or returns a non-2xx status."""
    def __init__(self, status: int, body: bytes, reason: str = ""):
        super().__init__(f"upstream returned HTTP {status}: {reason or body[:200]!r}")
        self.status = status
        self.body = body
        self.reason = reason

def open_upstream(
    backend_url: str,
    path_with_query: str,
    method: str,
    headers: dict[str, str],
    body: bytes,
    timeout_s: float,
) -> Any:
    url = backend_url + path_with_query
    req_headers = {
        k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP
    }
    if body:
        req_headers["content-length"] = str(len(body))
    req_headers.pop("expect", None)
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        err_body = exc.read()
        raise UpstreamError(exc.code, err_body, str(exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError(0, b"", str(exc.reason)) from exc
    return resp

# ---------------------------------------------------------------------------
# Context-overflow error translation (opt-in: --rewrite-context-errors)
# ---------------------------------------------------------------------------
def translate_context_error(body: bytes) -> bytes | None:
    """Translate a llama.cpp context-overflow 400 into the Anthropic error shape.

    llama.cpp answers an oversized prompt with::

        {"error": {"code": 400,
                   "message": "request (N tokens) exceeds the available context size (M tokens), try increasing it",
                   "type": "exceed_context_size_error",
                   "n_prompt_tokens": N, "n_ctx": M}}

    Clients like Claude Code only learn the real backend window from the
    Anthropic-shaped ``prompt is too long: N tokens > M maximum`` phrasing;
    with anything else they just report the error and the session dead-ends,
    because every retry — /compact included — resends the same oversized
    history. Rewritten bodies carry the real numbers so the client can shrink
    its assumed window and compact reactively. Returns None when the body is
    not that error, so the caller relays the upstream response untouched.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if not isinstance(err, dict) or err.get("type") != "exceed_context_size_error":
        return None
    n_prompt = err.get("n_prompt_tokens")
    n_ctx = err.get("n_ctx")
    if isinstance(n_prompt, int) and isinstance(n_ctx, int):
        message = f"prompt is too long: {n_prompt} tokens > {n_ctx} maximum"
    else:
        message = "prompt is too long: the prompt exceeds the backend context size"
    return json.dumps(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        }
    ).encode("utf-8")

# ---------------------------------------------------------------------------
# Prompt-size guard for silent-truncating backends (opt-in: --prompt-guard)
# ---------------------------------------------------------------------------
# Ollama never rejects an oversized prompt: every endpoint (native /api/chat,
# OpenAI /v1/chat/completions, Anthropic /v1/messages) answers HTTP 200 with
# the prompt silently truncated to fit — verified live on Ollama 0.33.2, even
# with truncate:false. There is no error to translate, so the client-facing
# signal must be produced here, before the backend sees the request. The 400
# carries the same Anthropic phrasing the translator emits so Claude Code
# learns the real window and compacts reactively instead of receiving an
# answer computed from a silently amputated context.
_SKIP_KEYS = frozenset({"data", "image", "image_url", "audio", "source", "video"})

def _harvest_text(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in _SKIP_KEYS:
                continue
            _harvest_text(val, out)
    elif isinstance(obj, list):
        for val in obj:
            _harvest_text(val, out)
    elif isinstance(obj, str):
        out.append(obj)

def estimate_prompt_tokens(parsed: dict) -> int:
    """Estimate request size in tokens as chars/3 over all text content.

    Deliberately generous: English runs ~4 chars/token under the Qwen
    tokenizer, so chars/3 over-counts ordinary text and the guard fires
    slightly early rather than after truncation has already happened. Tool
    schemas and system prompt are counted (they are part of the prompt);
    base64 blobs are skipped because chars/3 wildly miscounts them.
    """
    texts: list[str] = []
    _harvest_text(parsed, texts)
    return sum(len(t) for t in texts) // 3

def too_long_response(estimated_tokens: int, limit: int) -> bytes:
    return json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": f"prompt is too long: {estimated_tokens} tokens > {limit} maximum",
            },
        }
    ).encode("utf-8")

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class RouterHandler(BaseHTTPRequestHandler):
    """Handles a single client connection and proxies it upstream."""
    config: RouterConfig = None  # type: ignore[assignment]
    request_counter: int = 0
    _counter_lock = threading.Lock()
    server_version = "ai-tools-router/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("http: " + fmt, *args)

    def _next_id(self) -> int:
        with self._counter_lock:
            RouterHandler.request_counter += 1
            return RouterHandler.request_counter

    def _read_request_body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_response(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json_error(self, status: int, message: str, details: dict | None = None) -> None:
        payload: dict[str, Any] = {"error": message}
        if details:
            payload.update(details)
        self._send_response(status, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_response(200, b"ok", "text/plain; charset=utf-8")
            return
        self._send_json_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        self.handle_request("POST")

    def handle_request(self, method: str) -> None:
        req_id = self._next_id()
        started = time.monotonic()
        allowed_paths = {"/v1/messages", "/v1/chat/completions"}
        path = self.path.split("?", 1)[0]
        if path not in allowed_paths:
            self._send_json_error(
                404,
                f"path {path!r} is not proxied; supported: {sorted(allowed_paths)}",
            )
            return
        body = self._read_request_body()
        if not body:
            self._send_json_error(400, "empty request body")
            return
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json_error(400, f"request body is not valid JSON: {exc}")
            return
        if not isinstance(parsed, dict):
            self._send_json_error(400, "request body must be a JSON object")
            return
        model = parsed.get("model")
        if not model or not isinstance(model, str):
            self._send_json_error(400, "request body must include a string 'model' field")
            return
        backend = self.config.resolve_backend(model)
        if backend is None:
            known = ", ".join(sorted(self.config.models)) or "(none)"
            self._send_json_error(
                404,
                f"no backend configured for model {model!r}; known models: {known}",
            )
            return
        # Prompt guard: reject oversized prompts for backends that truncate
        # silently instead of erroring, so the client can compact instead.
        guard_limit = self.config.prompt_guards.get(model)
        if guard_limit:
            est = estimate_prompt_tokens(parsed)
            if est >= guard_limit:
                log.warning(
                    "[%d] prompt-guard: model=%s est=%d tokens >= limit=%d — "
                    "rejecting so the client compacts instead of truncating",
                    req_id, model, est, guard_limit,
                )
                self._send_response(
                    400, too_long_response(est, guard_limit), "application/json"
                )
                return
        # Log tool schema sizes — handles both OpenAI (parameters) and
        # Anthropic (input_schema) tool formats.
        tools = parsed.get("tools", [])
        tool_summary = []
        for t in tools:
            fn = t.get("function", t)
            name = fn.get("name", "?")
            schema = get_tool_schema(fn)
            schema_bytes = len(json.dumps(schema))
            tool_summary.append(f"{name}={schema_bytes}B")
        log.info(
            "[%d] tools=%d  total_body=%dB  schemas: %s",
            req_id, len(tools), len(body),
            ", ".join(tool_summary) if tool_summary else "(none)",
        )
        # Sanitise tool schemas for llama.cpp GBNF compatibility.
        parsed, removed, schemas_modified = sanitise_tools(parsed)
        if removed:
            log.info("[%d] blocked tools: %s", req_id, ", ".join(removed))
        if schemas_modified:
            log.info("[%d] stripped maxLength constraints from tool schemas", req_id)
        if removed or schemas_modified:
            body = json.dumps(parsed).encode("utf-8")
        log.info(
            "[%d] %s %s  model=%s  ->  %s  (%d bytes)",
            req_id, method, path, model, backend, len(body),
        )
        # Forward.
        try:
            resp = open_upstream(
                backend,
                self.path,
                method,
                dict(self.headers.items()),
                body,
                self.config.request_timeout_s,
            )
        except UpstreamError as exc:
            if exc.status == 0:
                log.error("[%d] upstream %s unreachable: %s", req_id, backend, exc.reason)
                self._send_json_error(
                    502, f"upstream unreachable: {exc.reason}", {"backend": backend}
                )
            else:
                log.warning(
                    "[%d] upstream %s returned HTTP %d", req_id, backend, exc.status
                )
                body_out = exc.body
                if self.config.rewrite_context_errors:
                    rewritten = translate_context_error(exc.body)
                    if rewritten is not None:
                        log.info(
                            "[%d] rewrote context-overflow error to Anthropic shape",
                            req_id,
                        )
                        body_out = rewritten
                self._send_response(exc.status, body_out, self._content_type_from_headers())
            return
        status = resp.status
        reason = resp.reason or ""
        ctype = resp.headers.get("content-type", "application/json")
        is_streaming = ctype.startswith("text/event-stream") or (
            "transfer-encoding" in {k.lower() for k in resp.headers}
        )
        if is_streaming:
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("cache-control", "no-cache, no-transform")
            self.send_header("connection", "close")
            self.close_connection = True
            self.end_headers()
            bytes_sent = 0
            response_sample = bytearray()
            try:
                while True:
                    chunk = resp.read(self.config.read_chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    bytes_sent += len(chunk)
                    # Capture first 4000 bytes for response log
                    if len(response_sample) < 4000:
                        response_sample.extend(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError) as exc:
                log.warning(
                    "[%d] client disconnected mid-stream (%s); sent %d bytes",
                    req_id, type(exc).__name__, bytes_sent,
                )
            finally:
                resp.close()
            # Log response sample
            log_response_body(req_id, model, path, bytes(response_sample))
            elapsed_ms = (time.monotonic() - started) * 1000
            log.info(
                "[%d] %s done  status=%d  streamed=%d bytes  elapsed=%.0fms",
                req_id, path, status, bytes_sent, elapsed_ms,
            )
            return
        # Non-streaming: buffer the whole body.
        try:
            full_body = resp.read()
            resp.close()
        except Exception as exc:  # noqa: BLE001
            log.error("[%d] error reading upstream body: %s", req_id, exc)
            self._send_json_error(502, f"error reading upstream body: {exc}")
            return
        # Log full response body
        log_response_body(req_id, model, path, full_body)
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(full_body)))
        self.end_headers()
        if full_body:
            self.wfile.write(full_body)
        elapsed_ms = (time.monotonic() - started) * 1000
        log.info(
            "[%d] %s done  status=%d  body=%d bytes  elapsed=%.0fms",
            req_id, path, status, len(full_body), elapsed_ms,
        )

    def _content_type_from_headers(self) -> str:
        return "application/json"

# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------
def make_server(config: RouterConfig) -> ThreadingHTTPServer:
    RouterHandler.config = config
    server = ThreadingHTTPServer((config.host, config.port), RouterHandler)
    server.daemon_threads = True
    return server

def serve(config: RouterConfig) -> int:
    server = make_server(config)
    log.info(
        "router listening on %s:%d  (config=%s)",
        config.host, config.port, config.source_path,
    )
    for model, backend in config.models.items():
        log.info("  %-32s -> %s", model, backend)
    if config.default_model:
        log.info("  default model: %s", config.default_model)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Route LLM API requests to llama.cpp backends by model name."
    )
    parser.add_argument(
        "--config",
        help="Path to the YAML/TOML config file (overrides ROUTER_CONFIG env var).",
    )
    parser.add_argument(
        "--host",
        help="Override the bind host from the config file.",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Override the bind port from the config file (default 8090).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--rewrite-context-errors",
        action="store_true",
        help=(
            "Translate a llama.cpp 'exceed_context_size_error' 400 into the "
            "Anthropic error shape ('prompt is too long: N tokens > M maximum'). "
            "Off by default: without it the upstream error is relayed verbatim. "
            "Claude Code only learns the real backend window from the Anthropic "
            "phrasing, so enabling this lets it shrink its assumed window and "
            "compact instead of dead-ending on long sessions."
        ),
    )
    parser.add_argument(
        "--prompt-guard",
        action="append",
        default=[],
        metavar="MODEL=LIMIT",
        help=(
            "Reject requests to MODEL whose estimated prompt size reaches "
            "LIMIT tokens (estimated as chars/3 over text content) with the "
            "Anthropic 'prompt is too long: N tokens > M maximum' 400, "
            "without forwarding. For backends that silently truncate oversized "
            "prompts (Ollama) instead of erroring — the client never learns "
            "from a silent amputation. Repeatable: --prompt-guard worker=16386. "
            "Off for models not listed."
        ),
    )
    return parser.parse_args(argv)

def main(argv: list[str]) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    config_path = resolve_config_path(args.config)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 2
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    config.rewrite_context_errors = args.rewrite_context_errors
    if config.rewrite_context_errors:
        log.info("context-overflow error rewriting: ON")
    for entry in args.prompt_guard:
        model, sep, limit = entry.rpartition("=")
        if not sep or not model or not limit.isdigit():
            log.error("bad --prompt-guard %r; expected MODEL=LIMIT", entry)
            return 2
        config.prompt_guards[model] = int(limit)
    if config.prompt_guards:
        guards = ", ".join(f"{m}<{lim}" for m, lim in sorted(config.prompt_guards.items()))
        log.info("prompt guards: ON (%s)", guards)
    return serve(config)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
