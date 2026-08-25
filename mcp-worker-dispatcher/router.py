#!/usr/bin/env python3
"""Lightweight model router: OpenAI-compatible proxy in front of llama.cpp backends.

Sits between Claude Code and one or more local llama.cpp servers. Reads the
``model`` field from the request body, looks it up in a YAML config, and
proxies the entire request to the matching backend — streaming responses
back transparently byte-for-byte.

Endpoints handled (anything else gets 404):
  GET  /v1/models
  POST /v1/chat/completions
  POST /v1/messages

Config (YAML), default location ~/ai-tools/router/config.yaml, overridable
via CLI flag or env var:

    host: 0.0.0.0
    port: 8090
    request_timeout_s: 120
    read_chunk_size: 4096
    models:
      orchestrator: "http://localhost:8080"
      worker: "http://localhost:8081"
    default_model: null   # or a model name to fall back to on unknown models

Usage:
    python router.py
    python router.py --config /path/to/config.yaml
    ROUTER_CONFIG=/path/to/config.yaml python router.py
    python router.py --port 9090            # override port from CLI

Dependencies: httpx, PyYAML, uvicorn.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
import yaml

log = logging.getLogger("router")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def default_config_paths() -> list[Path]:
    env = os.environ.get("ROUTER_CONFIG")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / "ai-tools" / "router" / "config.yaml")
    return candidates


def resolve_config_path(cli_path: str | None) -> Path:
    candidates = [Path(cli_path)] if cli_path else default_config_paths()
    for c in candidates:
        if c.is_file():
            return c
    raise SystemExit(
        "config file not found. Tried:\n  " + "\n  ".join(str(c) for c in candidates)
        + "\nPass --config /path/to/config.yaml or set ROUTER_CONFIG."
    )


def load_config(path: Path) -> dict:
    """Load and normalize the YAML config."""
    raw = yaml.safe_load(path.read_text()) or {}

    models = raw.get("models") or {}
    if not isinstance(models, dict) or not models:
        raise ValueError(f"config {path} must define a non-empty 'models' mapping")

    # Normalize: strip trailing slashes, coerce to str.
    models = {str(k): str(v).rstrip("/") for k, v in models.items()
              if isinstance(v, (str, int)) and str(v).strip()}
    if not models:
        raise ValueError(f"config {path} 'models' has no valid entries")

    default_model = raw.get("default_model")
    if default_model is not None:
        default_model = str(default_model)
        if default_model not in models:
            log.warning("default_model %r not in models; ignoring", default_model)
            default_model = None

    return {
        "host": str(raw.get("host", "0.0.0.0")),
        "port": int(raw.get("port", 8090)),
        "request_timeout_s": float(raw.get("request_timeout_s", 120)),
        "read_chunk_size": int(raw.get("read_chunk_size", 4096)),
        "models": models,
        "default_model": default_model,
    }


# ---------------------------------------------------------------------------
# Router state
# ---------------------------------------------------------------------------

class Router:
    def __init__(self, cfg: dict):
        self.models: dict[str, str] = cfg["models"]
        self.default_model: str | None = cfg["default_model"]
        self.timeout = httpx.Timeout(cfg["request_timeout_s"], connect=10.0)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "Router":
        # One shared client: httpx pools connections per backend host.
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            http1=True,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        return self

    async def __aexit__(self, *exc):
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, "Router not started"
        return self._client

    def resolve_backend(self, model: str) -> str | None:
        if model in self.models:
            return self.models[model]
        if self.default_model and self.default_model in self.models:
            return self.models[self.default_model]
        return None


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------

PROXIED_PATHS = {"/v1/chat/completions", "/v1/messages"}
# Hop-by-hop headers that must not be forwarded (RFC 7230 §6.1).
HOP_HEADERS = {
    "host", "content-length", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
}


def _json_bytes(obj) -> bytes:
    return json.dumps(obj).encode()


async def handle_request(router: Router, method: str, path: str,
                         headers: list[tuple[str, str]], body: bytes):
    """Returns (status, resp_headers, payload).

    payload is either a bytes object (send once) or an async iterator of
    bytes (stream chunk by chunk, then close).
    """

    # ---- /v1/models -------------------------------------------------------
    if method == "GET" and path == "/v1/models":
        data = {"data": [{"id": name, "object": "model", "url": url}
                         for name, url in sorted(router.models.items())]}
        return 200, [("content-type", "application/json")], _json_bytes(data)

    # ---- 404 for everything else not proxied -----------------------------
    if method != "POST" or path not in PROXIED_PATHS:
        return 404, [("content-type", "application/json")], \
            _json_bytes({"error": f"not found: {method} {path}"})

    # ---- parse model from body --------------------------------------------
    try:
        model = json.loads(body).get("model", "")
    except (ValueError, AttributeError):
        return 400, [("content-type", "application/json")], \
            _json_bytes({"error": "malformed JSON body"})

    backend = router.resolve_backend(model)
    if backend is None:
        known = ", ".join(router.models) or "(none)"
        return 404, [("content-type", "application/json")], \
            _json_bytes({"error": f"unknown model {model!r}. Known models: {known}"})

    log.info("model=%-20s -> %s%s  (%d bytes)", model, backend, path, len(body))

    fwd_headers = [(k, v) for k, v in headers if k not in HOP_HEADERS]
    fwd_headers.append(("content-type", "application/json"))

    try:
        req = router.client.build_request("POST", f"{backend}{path}",
                                          headers=fwd_headers, content=body)
        resp = await router.client.send(req, stream=True)
    except httpx.ConnectError as e:
        log.error("backend %s unreachable: %s", backend, e)
        return 502, [("content-type", "application/json")], \
            _json_bytes({"error": f"backend {backend} unreachable: {e}"})
    except httpx.TimeoutException as e:
        log.error("backend %s timeout: %s", backend, e)
        return 504, [("content-type", "application/json")], \
            _json_bytes({"error": f"backend {backend} timed out: {e}"})

    resp_headers = [(k, v) for k, v in resp.headers.items() if k not in HOP_HEADERS]

    if resp.status_code != 200:
        body_out = await resp.aread()
        await resp.aclose()
        log.warning("model=%s backend=%s returned %d", model, backend, resp.status_code)
        return resp.status_code, resp_headers, body_out

    # 200: stream the body back transparently.
    async def stream():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return 200, resp_headers, stream()


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------

def make_asgi_app(router: Router):
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope["method"]
        headers = [(k.decode("latin-1").lower(), v.decode("latin-1"))
                   for k, v in scope.get("headers", [])]

        # Read the full request body.
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        try:
            status, resp_headers, payload = await handle_request(
                router, method, path, headers, body)
        except Exception:
            log.exception("unhandled error for %s %s", method, path)
            status, resp_headers, payload = 500, \
                [("content-type", "application/json")], \
                _json_bytes({"error": "internal server error"})

        await send({
            "type": "http.response.start",
            "status": status,
            "headers": resp_headers,
        })

        if isinstance(payload, (bytes, bytearray)):
            await send({"type": "http.response.body",
                        "body": bytes(payload), "more_body": False})
        else:
            async for chunk in payload:
                await send({"type": "http.response.body",
                            "body": chunk, "more_body": True})
            await send({"type": "http.response.body",
                        "body": b"", "more_body": False})

    return app


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OpenAI-compatible model router for llama.cpp backends")
    p.add_argument("--config", type=str, default=None,
                   help="Path to config.yaml (default: $ROUTER_CONFIG or "
                        "~/ai-tools/router/config.yaml)")
    p.add_argument("--host", type=str, default=None,
                   help="Override host from config")
    p.add_argument("--port", type=int, default=None,
                   help="Override port from config")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Debug logging")
    return p.parse_args()


async def main_async(cfg: dict, host: str, port: int, verbose: bool) -> None:
    import uvicorn

    log.info("router starting on %s:%d", host, port)
    log.info("models: %s", ", ".join(f"{k}->{v}" for k, v in cfg["models"].items()))
    if cfg["default_model"]:
        log.info("default_model: %s", cfg["default_model"])

    async with Router(cfg) as router:
        app = make_asgi_app(router)
        server = uvicorn.Server(uvicorn.Config(
            app, host=host, port=port,
            log_level="debug" if verbose else "info", access_log=False))
        await server.serve()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    cfg_path = resolve_config_path(args.config)
    log.info("loading config from %s", cfg_path)
    cfg = load_config(cfg_path)

    host = args.host or cfg["host"]
    port = args.port or cfg["port"]

    try:
        asyncio.run(main_async(cfg, host, port, args.verbose))
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
