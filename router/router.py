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
    # Quiet down urllib so we control the noise ourselves.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


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
    source_path: Path | None = None

    def resolve_backend(self, model: str) -> str | None:
        """Return the backend URL for ``model``, falling back to the default."""
        if model in self.models:
            return self.models[model]
        if self.default_model is not None and self.default_model in self.models:
            return self.models[self.default_model]
        return None


def load_config(path: Path) -> RouterConfig:
    """Parse a YAML or TOML config file into a RouterConfig.

    The file extension decides the parser; YAML is the default. Unknown
    extensions raise ConfigError so a typo'd path doesn't silently fall
    through.
    """
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
    """Apply the CLI > env > default resolution order for the config path."""
    if flag:
        return Path(flag).expanduser()
    env = os.environ.get("ROUTER_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / "ai-tools" / "router" / "config.yaml"


# ---------------------------------------------------------------------------
# Upstream proxying
# ---------------------------------------------------------------------------

# Headers that must not be forwarded to the backend as-is. Connection
# management, hop-by-hop, and our own routing metadata are all stripped.
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
    """Open the backend request and return a readable response object.

    The returned object is the raw ``http.client.HTTPResponse`` wrapped by
    urllib. Callers must read it to exhaustion and close it — the response
    is not buffered so streaming works.
    """
    url = backend_url + path_with_query
    req_headers = {
        k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP
    }
    # Make sure we forward content-length for non-streaming correctness and
    # that we do NOT set an Expect: 100-continue we can't honor.
    if body:
        req_headers["content-length"] = str(len(body))
    req_headers.pop("expect", None)

    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        # HTTPError is still a readable response; surface status + body.
        err_body = exc.read()
        raise UpstreamError(exc.code, err_body, str(exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError(0, b"", str(exc.reason)) from exc
    return resp


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class RouterHandler(BaseHTTPRequestHandler):
    """Handles a single client connection and proxies it upstream."""

    # Bound class-level references set by the server factory below.
    config: RouterConfig = None  # type: ignore[assignment]
    request_counter: int = 0
    _counter_lock = threading.Lock()

    server_version = "ai-tools-router/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # The default BaseHTTPRequestHandler logger is too noisy; we do our
        # own structured logging in handle_request().
        log.debug("http: " + fmt, *args)

    def _next_id(self) -> int:
        with self._counter_lock:
            RouterHandler.request_counter += 1
            return RouterHandler.request_counter

    def _read_request_body(self) -> bytes:
        """Read the request body honoring content-length. No chunked support
        on the inbound side — Claude Code sends content-length."""
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

    # -- request entrypoints -------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        # Only /health is allowed; anything else is a 404.
        if self.path == "/health":
            self._send_response(200, b"ok", "text/plain; charset=utf-8")
            return
        self._send_json_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        self.handle_request("POST")

    # -- core ----------------------------------------------------------------

    def handle_request(self, method: str) -> None:
        req_id = self._next_id()
        started = time.monotonic()

        # Only two endpoints are supported.
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

        log.info(
            "[%d] %s %s  model=%s  ->  %s  (%d bytes)",
            req_id,
            method,
            path,
            model,
            backend,
            len(body),
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
                self._send_response(exc.status, exc.body, self._content_type_from_headers())
            return

        status = resp.status
        reason = resp.reason or ""
        ctype = resp.headers.get("content-type", "application/json")

        # Stream the body back to the client. For streaming responses the
        # content-type is text/event-stream and there is no content-length,
        # so we must NOT set content-length ourselves — we set
        # transfer-encoding: chunked (HTTP/1.1 default) and write chunks as
        # they arrive. For non-streaming responses we buffer the body so we
        # can set content-length (keeps keep-alive simple).
        is_streaming = ctype.startswith("text/event-stream") or (
            "transfer-encoding" in {k.lower() for k in resp.headers}
        )

        if is_streaming:
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("cache-control", "no-cache, no-transform")
            # No content-length: the client reads until the connection
            # closes. We can't use HTTP/1.1 keep-alive without a length, so
            # we close the connection at end of stream.
            self.send_header("connection", "close")
            self.close_connection = True
            self.end_headers()

            bytes_sent = 0
            try:
                while True:
                    chunk = resp.read(self.config.read_chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    bytes_sent += len(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError) as exc:
                log.warning(
                    "[%d] client disconnected mid-stream (%s); sent %d bytes",
                    req_id,
                    type(exc).__name__,
                    bytes_sent,
                )
            finally:
                resp.close()
            elapsed_ms = (time.monotonic() - started) * 1000
            log.info(
                "[%d] %s done  status=%d  streamed=%d bytes  elapsed=%.0fms",
                req_id,
                path,
                status,
                bytes_sent,
                elapsed_ms,
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

        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(full_body)))
        self.end_headers()
        if full_body:
            self.wfile.write(full_body)

        elapsed_ms = (time.monotonic() - started) * 1000
        log.info(
            "[%d] %s done  status=%d  body=%d bytes  elapsed=%.0fms",
            req_id,
            path,
            status,
            len(full_body),
            elapsed_ms,
        )

    def _content_type_from_headers(self) -> str:
        # Best-effort content type for error responses forwarded from upstream.
        return "application/json"


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def make_server(config: RouterConfig) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer with the config bound to the handler."""
    RouterHandler.config = config
    server = ThreadingHTTPServer((config.host, config.port), RouterHandler)
    server.daemon_threads = True
    return server


def serve(config: RouterConfig) -> int:
    """Run the router until interrupted. Returns a process exit code."""
    server = make_server(config)
    log.info(
        "router listening on %s:%d  (config=%s)",
        config.host,
        config.port,
        config.source_path,
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
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
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

    return serve(config)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
