"""MCP server that exposes web search as a tool via FastMCP / stdio.

Primary backend is Serper (https://serper.dev) — Google results via a simple
HTTP API, far more reliable than scraping. Set ``SERPER_API_KEY`` to enable it.
If no key is set (or Serper errors), it falls back to DuckDuckGo scraping.
"""
import logging
import os

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("whatsapp-search")

SERPER_URL = "https://google.serper.dev/search"
MAX_RESULTS = 5


def _format_serper(data: dict) -> str:
    """Turn a Serper response into a compact, WhatsApp-friendly summary."""
    lines: list[str] = []

    answer = data.get("answerBox") or {}
    if answer.get("answer"):
        lines.append(f"Answer: {answer['answer']}")
    elif answer.get("snippet"):
        lines.append(f"Answer: {answer['snippet']}")

    kg = data.get("knowledgeGraph") or {}
    if kg.get("description"):
        title = kg.get("title", "")
        lines.append(f"{title}: {kg['description']}".strip(": "))

    for r in (data.get("organic") or [])[:MAX_RESULTS]:
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        link = r.get("link", "")
        lines.append(f"- {title}: {snippet} ({link})")

    return "\n".join(lines)


async def _serper_search(query: str, api_key: str) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": MAX_RESULTS},
        )
        resp.raise_for_status()
        data = resp.json()

    summary = _format_serper(data)
    return summary or f"No search results found for '{query}'."


def _ddg_search(query: str) -> str:
    # The package was renamed duckduckgo-search -> ddgs; support whichever is installed.
    try:
        from ddgs import DDGS
    except ModuleNotFoundError:
        from duckduckgo_search import DDGS

    results = list(DDGS().text(query, max_results=MAX_RESULTS))
    if not results:
        return f"No search results found for '{query}'."
    lines = [
        f"- {r.get('title', '')}: {r.get('body', '')} ({r.get('href', '')})"
        for r in results
    ]
    return "\n".join(lines)


@mcp.tool()
async def search_web(query: str) -> str:
    """Search the web for up-to-date information and return a summary of results.

    Args:
        query: The search query string.
    """
    api_key = os.environ.get("SERPER_API_KEY")
    if api_key:
        try:
            return await _serper_search(query, api_key)
        except Exception as exc:
            logger.warning("Serper search failed for '%s' (%s); trying DuckDuckGo", query, exc)

    try:
        return _ddg_search(query)
    except Exception as exc:
        logger.warning("Web search failed for '%s': %s", query, exc)
        return (
            f"Web search unavailable right now. "
            f"Answer the user's question about '{query}' using your own knowledge, "
            f"and note the answer might not reflect the very latest information."
        )


if __name__ == "__main__":
    mcp.run(transport="stdio")
