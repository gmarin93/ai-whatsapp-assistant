"""Load MCP server tools and expose them as LangChain BaseTool instances.

Each MCP server gets a POOL of persistent stdio subprocesses per worker
process (size = ``settings.mcp_pool_size``). Sessions are created once at
startup (``init_mcp_pool``) and checked out per tool call from an
``asyncio.Queue``, which both caps concurrency at the pool size and guarantees
a single in-flight call per (single-threaded) stdio session. Call
``close_mcp_pool`` during shutdown to clean up.

For even higher throughput run multiple ARQ worker processes; each worker owns
its own pool of subprocesses.
"""
import asyncio
import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)

MCP_DIR = Path(__file__).parent

# Pool: script path → queue of idle ClientSessions ready to be checked out.
_session_pools: dict[str, "asyncio.Queue[ClientSession]"] = {}
_exit_stack = AsyncExitStack()
_pool_lock = asyncio.Lock()


# ── Pydantic model builder ────────────────────────────────────────────────────

_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _build_args_model(name: str, schema: dict) -> type[BaseModel]:
    """Create a Pydantic model from a JSON Schema object (primitives only)."""
    props: dict[str, Any] = schema.get("properties") or {}
    required: set[str] = set(schema.get("required") or [])

    fields: dict[str, Any] = {}
    for field_name, field_schema in props.items():
        raw_type = field_schema.get("type", "string")
        py_type: type = _TYPE_MAP.get(raw_type, str)
        description: str = field_schema.get("description", "")

        if field_name in required:
            fields[field_name] = (py_type, Field(..., description=description))
        else:
            fields[field_name] = (Optional[py_type], Field(None, description=description))

    return create_model(f"{name}Args", **fields)


# ── Persistent session helpers ────────────────────────────────────────────────

def _server_params(script: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(script)],
        env=dict(os.environ),
    )


async def _open_session(script: Path) -> ClientSession:
    """Open a persistent stdio session for *script* and register it via the exit stack."""
    params = _server_params(script)
    read, write = await _exit_stack.enter_async_context(stdio_client(params))
    session = await _exit_stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


async def _ensure_pool(script: Path, size: int) -> "asyncio.Queue[ClientSession]":
    """Open *size* persistent sessions for *script* (once) and return the idle queue."""
    key = str(script)
    if key not in _session_pools:
        async with _pool_lock:
            if key not in _session_pools:
                queue: "asyncio.Queue[ClientSession]" = asyncio.Queue()
                for _ in range(max(1, size)):
                    queue.put_nowait(await _open_session(script))
                _session_pools[key] = queue
    return _session_pools[key]


# ── Dynamic BaseTool factory ──────────────────────────────────────────────────

def _make_langchain_tool(script: Path, mcp_tool: Any) -> BaseTool:
    """Return a LangChain BaseTool backed by a persistent MCP session."""

    tool_name: str = mcp_tool.name
    tool_desc: str = mcp_tool.description or ""
    input_schema: dict = mcp_tool.inputSchema or {}
    ArgsModel = _build_args_model(tool_name, input_schema)

    _script = script
    _name = tool_name

    class _MCPTool(BaseTool):
        name: str = _name
        description: str = tool_desc
        args_schema: type[BaseModel] = ArgsModel

        def _run(self, **kwargs: Any) -> str:  # sync fallback
            return asyncio.get_event_loop().run_until_complete(self._arun(**kwargs))

        async def _arun(self, **kwargs: Any) -> str:
            clean = {k: v for k, v in kwargs.items() if v is not None}
            queue = _session_pools[str(_script)]
            session = await queue.get()
            try:
                result = await session.call_tool(_name, clean)
            finally:
                queue.put_nowait(session)
            for block in result.content or []:
                if hasattr(block, "text"):
                    return block.text
            return str(result.content)

    return _MCPTool()


# ── Public API ────────────────────────────────────────────────────────────────

async def init_mcp_pool() -> list[BaseTool]:
    """Open persistent session pools for all bundled MCP servers and return tools.

    Call once during application/worker startup.
    """
    from app.config import get_settings

    pool_size = get_settings().mcp_pool_size

    server_scripts = sorted(MCP_DIR.glob("*_server.py"))
    if not server_scripts:
        logger.warning("No *_server.py files found in %s", MCP_DIR)
        return []

    all_tools: list[BaseTool] = []

    for script in server_scripts:
        try:
            queue = await _ensure_pool(script, pool_size)
            # Borrow one session just to enumerate the server's tools.
            session = await queue.get()
            try:
                result = await session.list_tools()
            finally:
                queue.put_nowait(session)
            lc_tools = [_make_langchain_tool(script, t) for t in result.tools]
            logger.info(
                "MCP server '%s' — %d tool(s) across %d session(s): %s",
                script.name,
                len(lc_tools),
                queue.qsize(),
                [t.name for t in lc_tools],
            )
            all_tools.extend(lc_tools)
        except Exception:
            logger.exception("Failed to init MCP server '%s' — skipping", script.name)

    return all_tools


async def close_mcp_pool() -> None:
    """Close all persistent MCP sessions. Call during shutdown."""
    await _exit_stack.aclose()
    _session_pools.clear()
    logger.info("MCP session pool closed")


# Backwards-compatible alias
load_mcp_tools = init_mcp_pool
