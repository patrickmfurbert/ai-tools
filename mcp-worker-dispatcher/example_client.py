#!/usr/bin/env python3
"""Standalone example: drive the worker-dispatcher MCP server directly.

Run from the mcp-worker-dispatcher/ directory (with the router running):
    .venv/bin/python example_client.py
"""

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


if __name__ == "__main__":
    asyncio.run(main())
