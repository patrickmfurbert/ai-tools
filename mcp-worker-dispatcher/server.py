#!/usr/bin/env python3
"""MCP server that dispatches tasks to the ai-tools local model router.

Exposes three tools:
  - dispatch_task(instruction, context, model="worker") -> str
  - dispatch_tasks_parallel(tasks: list[dict]) -> list[dict]
  - list_models() -> dict

The router (see ../router) is an OpenAI-compatible HTTP front-end for
llama.cpp backends. This server is a thin async client on top of it: it
turns tool calls into /v1/chat/completions requests and returns the text.

Configuration:
  ROUTER_URL        Base URL of the router. Default: http://localhost:8090
  ROUTER_TIMEOUT_S  Per-request timeout in seconds. Default: 120
  ROUTER_CONFIG     Path to the router's config file. Used by list_models()
                    as a fallback when the router does not implement
                    /v1/models. Default: ~/projects/ai-tools/router/config.yaml

Transport: stdio (the MCP default). Run with:
  python server.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

log = logging.getLogger("mcp-worker-dispatcher")

ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8090").rstrip("/")
TIMEOUT_S = float(os.environ.get("ROUTER_TIMEOUT_S", "120"))
ROUTER_CONFIG = Path(os.environ.get(
    "ROUTER_CONFIG", "~/projects/ai-tools/router/config.yaml"
)).expanduser()

app = Server("mcp-worker-dispatcher")


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

async def _chat_completion(client: httpx.AsyncClient, instruction: str,
                           context: str, model: str) -> str:
    """Send one chat completion to the router and return the response text.

    Raises httpx.HTTPStatusError, httpx.RequestError, or ValueError on
    failure — callers decide how to represent the error.
    """
    messages = []
    if context:
        messages.append({"role": "system", "content": context})
    messages.append({"role": "user", "content": instruction})

    resp = await client.post(
        f"{ROUTER_URL}/v1/chat/completions",
        json={"model": model, "messages": messages, "stream": False},
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(
            f"unexpected response shape: {json.dumps(data)[:500]}"
        ) from e


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def dispatch_task(instruction: str, context: str, model: str) -> str:
    """Send a single task to the router; returns response text. Raises on error."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        return await _chat_completion(client, instruction, context, model)


async def _run_one(client: httpx.AsyncClient, task: dict) -> dict:
    """Run a single task dict; never raises.

    Success: {"ok": True, "model": str, "response": str}
    Failure: {"ok": False, "model": str, "error": str}
    """
    if not isinstance(task, dict) or "instruction" not in task:
        return {"ok": False, "model": "worker",
                "error": "task must be a dict with a required 'instruction' key"}

    model = task.get("model", "worker")
    try:
        text = await _chat_completion(client, task["instruction"],
                                      task.get("context", ""), model)
        return {"ok": True, "model": model, "response": text}
    except (httpx.HTTPError, ValueError, TypeError) as e:
        return {"ok": False, "model": model, "error": f"{type(e).__name__}: {e}"}


async def dispatch_tasks_parallel(tasks: list[dict]) -> list[dict]:
    """Dispatch all tasks concurrently; results preserve input order.

    Each input task: {"instruction": str, "context": str, "model": str (optional)}.
    Each result:     {"ok": bool, "model": str, "response"?: str, "error"?: str}.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        return await asyncio.gather(*(_run_one(client, t) for t in tasks))


async def _try_router_models_endpoint() -> dict | None:
    """Query the router's /v1/models if it implements one. Returns None if the
    endpoint is 404 or the router is unreachable (callers should fall back)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{ROUTER_URL}/v1/models")
    except httpx.RequestError:
        return None  # router unreachable
    if resp.status_code == 404:
        return None  # endpoint not implemented
    resp.raise_for_status()
    data = resp.json()
    models = {}
    for m in data.get("data", []):
        name = m.get("id")
        if not name:
            continue
        models[name] = m.get("url") or m.get("backend_url") or True
    return models


async def list_models() -> dict:
    """Return the model names the router knows about, with backend URLs.

    Tries the router's /v1/models endpoint first (if implemented); falls back
    to parsing the `models:` section of the router's config file.
    """
    models = await _try_router_models_endpoint()
    if models is not None:
        return {"router_url": ROUTER_URL, "source": "router:/v1/models",
                "models": models}

    if not ROUTER_CONFIG.exists():
        raise FileNotFoundError(
            f"router /v1/models is not implemented and config file not "
            f"found at {ROUTER_CONFIG}"
        )
    models = _parse_router_config(ROUTER_CONFIG)
    return {"router_url": ROUTER_URL, "source": f"config:{ROUTER_CONFIG}",
            "models": models}


def _parse_router_config(path: Path) -> dict:
    """Extract {name: backend_url} from a router config (YAML or TOML).

    The router's config format is simple enough (`models:` is a flat
    name -> URL mapping) that we parse it without requiring PyYAML.
    """
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return _parse_yaml_models(text)
    if suffix == ".toml":
        import tomllib
        data = tomllib.loads(text)
        raw = data.get("models", {})
        return {name: url for name, url in raw.items()
                if isinstance(url, str)}
    raise ValueError(f"unsupported router config extension {suffix!r}: {path}")


def _parse_yaml_models(text: str) -> dict:
    """Parse the `models:` section of the router's YAML config.

    Handles the flat `name: "url"` layout the router uses. Values may be
    quoted or bare; inline comments are stripped.
    """
    models = {}
    in_models = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t")):  # top-level key
            in_models = stripped == "models:"
            continue
        if not in_models:
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().strip("\"'")
        value = value.strip()
        # Strip inline comments and surrounding quotes.
        if value and value[0] not in "\"'":
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos]
            value = value.strip()
        value = value.strip("\"'")
        if key and value:
            models[key] = value
    return models


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="dispatch_task",
        description=(
            "Send a single task to a local model through the ai-tools router. "
            f"Router at {ROUTER_URL}. The 'model' field selects which backend "
            "the router uses (e.g. 'worker' for the fast 35B, 'orchestrator' "
            "for the larger model). Returns the model's response text."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "The task instruction to send to the model.",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Optional system-prompt context: background, files, "
                        "constraints. Empty for none."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Model name as configured in the router. Default: 'worker'."
                    ),
                },
            },
            "required": ["instruction"],
        },
    ),
    Tool(
        name="dispatch_tasks_parallel",
        description=(
            "Send many tasks to local models concurrently through the ai-tools "
            "router. Each task is dispatched in parallel via asyncio; results "
            "come back in input order. A failed task yields an error dict "
            "rather than failing the whole batch."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "List of tasks to dispatch in parallel.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "instruction": {"type": "string"},
                            "context": {"type": "string"},
                            "model": {"type": "string"},
                        },
                        "required": ["instruction"],
                    },
                },
            },
            "required": ["tasks"],
        },
    ),
    Tool(
        name="list_models",
        description=(
            "List the model names the ai-tools router knows about and their "
            f"backend URLs (router at {ROUTER_URL})."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
]


async def handle_list_tools(ctx, _params) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def handle_call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
    args = params.arguments or {}
    name = params.name

    try:
        if name == "dispatch_task":
            text = await dispatch_task(
                instruction=args.get("instruction", ""),
                context=args.get("context", ""),
                model=args.get("model", "worker"),
            )
            return CallToolResult(content=[TextContent(type="text", text=text)])

        if name == "dispatch_tasks_parallel":
            results = await dispatch_tasks_parallel(args.get("tasks", []))
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(results, indent=2))]
            )

        if name == "list_models":
            result = await list_models()
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result, indent=2))]
            )

        return CallToolResult(
            content=[TextContent(type="text", text=f"Unknown tool: {name}")],
            is_error=True,
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError,
            FileNotFoundError) as e:
        log.exception("tool %s failed", name)
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")],
            is_error=True,
        )


app = Server(
    "mcp-worker-dispatcher",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream,
                      app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
