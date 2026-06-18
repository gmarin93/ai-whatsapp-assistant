"""MCP server exposing the POS semantic layer as tools (FastMCP / stdio).

Auto-discovered by app/mcp/loader.py (``*_server.py`` glob). Each tool is a thin
async wrapper that runs the blocking sqlite3 call in a thread so the worker's
event loop is never stalled. The agent picks a tool + typed args; it cannot
write SQL.
"""
import asyncio
import logging
import os
import sys

# This file runs as its own process (``python app/mcp/pos_server.py``), so make
# the project root importable for ``app.pos.*``.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from app.pos import db, service  # noqa: E402

logger = logging.getLogger(__name__)
mcp = FastMCP("whatsapp-pos")

try:
    db.initialize()  # set WAL once (persists)
except Exception:  # noqa: BLE001
    logger.warning("POS DB initialize() failed — continuing", exc_info=True)


@mcp.tool()
async def sales_summary(period: str = "today") -> str:
    """Sales revenue, transaction count, discounts and average ticket.

    Args:
        period: today, yesterday, this_week, last_7_days, this_month,
            last_month, last_30_days, or all_time.
    """
    return await asyncio.to_thread(service.sales_summary, period)


@mcp.tool()
async def top_products(period: str = "today", limit: int = 5) -> str:
    """Best-selling products (by units) for a period. Same period values as sales_summary."""
    return await asyncio.to_thread(service.top_products, period, limit)


@mcp.tool()
async def low_stock_report() -> str:
    """List active products whose physical stock is below the minimum required."""
    return await asyncio.to_thread(service.low_stock_report)


@mcp.tool()
async def inventory_discrepancies() -> str:
    """List items where counted (physical) stock differs from the system stock."""
    return await asyncio.to_thread(service.inventory_discrepancies)


@mcp.tool()
async def customer_balances() -> str:
    """List customers who owe a pending balance."""
    return await asyncio.to_thread(service.customer_balances)


@mcp.tool()
async def find_user(name: str) -> str:
    """Find a user by name (accent/case-insensitive) and show their permissions."""
    return await asyncio.to_thread(service.find_user, name)


@mcp.tool()
async def set_user_permission(name: str, permission: str, enabled: bool) -> str:
    """Grant or revoke a user permission (audit-logged).

    Args:
        name: The user to modify (must match exactly one user).
        permission: 'refund' or 'modify_prices'.
        enabled: true to grant, false to revoke.
    """
    return await asyncio.to_thread(service.set_user_permission, name, permission, enabled)


if __name__ == "__main__":
    mcp.run(transport="stdio")
