#!/usr/bin/env python3
"""MCP server that dispatches tasks to the ai-tools local model router.

Exposes three tools:
  - dispatch_task(instruction, context, model="worker") -> str
  - dispatch_tasks_parallel(tasks: list[dict]) -> list[dict]
  - list_models() -> dict

The router (see ../router) is an OpenAI-compatible HTTP router in front of
local llama.cpp backends. Requests are plain /v1/chat/completions calls with
a `model` field that the router maps to a backend URL.

Configuration:
  ROUTER_URL        Base URL of the router. Default: http://localhost:8090
  ROUTER_TIMEOUT_S  Per-request timeout in seconds. Default: 120

Transport: stdio (the MCP default). Run with:
  python server.py

Built against mcp (official Python SDK) 2.x. On mcp 1.x the same handlers
work if you register them with the @app.list_tools() / @app.call_tool()
decorators instead of the on_list_tools= / on_call_tool= constructor
arguments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, \
    TextContent, Tool

log = logging.getLogger("mcp-worker-dispatcher")

ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8090").rstrip("/")
TIMEOUT_S = float(os.environ.get("ROUTER_TIMEOUT_S", "120"))


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


async def list_models() -> dict:
    """Return the router's model table: {"router_url": ..., "models": {name: url}}."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{ROUTER_URL}/v1/models")
        resp.raise_for_status()
        data = resp.json()

    models = {}
    for m in data.get("data", []):
        name = m.get("id")
        if not name:
            continue
        # The router reports the backend URL if it includes one; otherwise
        # just note the model exists.
        models[name] = m.get("url") or m.get("backend_url") or True

    return {"router_url": ROUTER_URL, "models": models}


# ---------------------------------------------------------------------------
# MCP request handlers
# ---------------------------------------------------------------------------

def _tool_definitions() -> list[Tool]:
    return [
        Tool(
            name="dispatch_task",
            description=(
                "Send a single task to a local model through the ai-tools router. "
                f"Router at {ROUTER_URL}. The 'model' field selects which backend "
                "the router uses (e.g. 'worker' for the fast 35B, 'orchestrator' "
                "for the larger model). Returns the model's response text."
            ),
            inputSchema={
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
                            "Model name as configured in the router. "
                            "Default: 'worker'."
                        ),
                    },
                },
                "required": ["instruction"],
            },
        ),
        Tool(
            name="dispatch_tasks_parallel",
            description=(
                "Send many tasks to local models concurrently through the "
                "ai-tools router. All tasks are dispatched in parallel via "
                "asyncio; results come back in input order. A failed task "
                "yields an error dict rather than failing the whole batch."
            ),
            inputSchema={
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
                "List the model names configured on the ai-tools router and the "
                "backend URL each one routes to. Use this to see what 'model' "
                "values are valid for dispatch_task and dispatch_tasks_parallel."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


async def on_list_tools(ctx, params) -> ListToolsResult:
    return ListToolsResult(tools=_tool_definitions())


async def on_call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
    """Dispatch a tool call. Errors are returned with isError=True so the
    calling agent sees them as failures, not a crashed server."""
    name = params.name
    args = params.arguments or {}
    try:
        if name == "dispatch_task":
            result = await dispatch_task(
                args.get("instruction", ""),
                args.get("context", ""),
                args.get("model", "worker"),
            )
            return CallToolResult(content=[TextContent(type="text", text=result)])

        elif name == "dispatch_tasks_parallel":
            results = await dispatch_tasks_parallel(args.get("tasks", []))
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(results, indent=2))]
            )

        elif name == "list_models":
            result = await list_models()
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result, indent=2))]
            )

        else:
            raise ValueError(f"unknown tool: {name}")
    except Exception as e:
        log.exception("tool %s failed", name)
        return CallToolResult(
            content=[TextContent(type="text",
                                 text=f"Error: {type(e).__name__}: {e}")],
            isError=True,
        )


app = Server(
    "mcp-worker-dispatcher",
    instructions=(
        f"Dispatch tasks to local LLMs via the ai-tools router at {ROUTER_URL}. "
        "Use list_models to see available model names before dispatching."
    ),
    on_list_tools=on_list_tools,
    on_call_tool=on_call_tool,
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
