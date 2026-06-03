"""LangChain tools for the WhatsApp agent."""

import logging
import os
from html import escape
from typing import Dict

import sendgrid
from langchain_core.tools import BaseTool, tool
from sendgrid.helpers.mail import Content, Email, Mail, To

logger = logging.getLogger(__name__)


def get_tools() -> list[BaseTool]:
    """Return tools to bind to the worker LLM."""
    tools: list[BaseTool] = [send_email, search_web]
    tools += _get_calendar_tools()
    return tools


# ── Email ────────────────────────────────────────────────────────────────────

@tool
def send_email(subject: str, body_md: str) -> Dict[str, str]:
    """Send an email. Uses SENDGRID_API_KEY. Body can be plain text or markdown-style."""
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key:
        return {"status": "skipped", "detail": "SENDGRID_API_KEY not set"}

    from_addr = os.environ.get("SENDGRID_FROM_EMAIL", "arias.fex@gmail.com")
    to_addr = os.environ.get("SENDGRID_TO_EMAIL", from_addr)

    try:
        safe = escape(body_md)
        html = (
            "<!DOCTYPE html><html><body style='font-family:ui-monospace,monospace;white-space:pre-wrap'>"
            f"{safe}</body></html>"
        )

        sg = sendgrid.SendGridAPIClient(api_key=api_key)
        from_email = Email(from_addr)
        to_email = To(to_addr)
        content = Content("text/html", html)
        mail = Mail(from_email, to_email, subject, content).get()
        response = sg.client.mail.send.post(request_body=mail)
        return {"status": "sent", "http_status": str(response.status_code)}
    except Exception as e:
        logger.exception("Email send failed")
        return {"status": "error", "detail": str(e)}


# ── Web Search ───────────────────────────────────────────────────────────────

@tool
def search_web(query: str) -> str:
    """Search the web for up-to-date information. Returns a summary of search results."""
    from ddgs import DDGS

    try:
        ddgs = DDGS()
        results = list(ddgs.text(query, max_results=5))
        if not results:
            return f"No search results found for '{query}'. I'll answer based on what I know."
        formatted = []
        for r in results:
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            formatted.append(f"- {title}: {body} ({href})")
        return "\n".join(formatted)
    except Exception as e:
        logger.warning("DDG search failed for '%s': %s — falling back to LLM knowledge", query, e)
        return (
            f"Web search unavailable right now (DDG error). "
            f"Answer the user's question about '{query}' using your own knowledge, "
            f"and let them know the answer might not reflect the very latest information."
        )


# ── Google Calendar ──────────────────────────────────────────────────────────

def _get_calendar_tools() -> list[BaseTool]:
    """Load Google Calendar tools. Returns empty list if credentials missing."""
    try:
        from langchain_google_community import CalendarToolkit

        toolkit = CalendarToolkit()
        tools = toolkit.get_tools()
        logger.info("Google Calendar tools loaded: %d tools", len(tools))
        return tools
    except Exception as e:
        logger.warning("Google Calendar tools not available: %s", e)
        return []
