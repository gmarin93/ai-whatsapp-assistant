"""MCP server that exposes email sending as a tool via FastMCP / stdio.

Email is sent through Resend (https://resend.com). Resend's free tier covers
3,000 emails/month (100/day), which is plenty for a personal assistant.

Two setup notes that cause most failures:
  * ``RESEND_FROM_EMAIL`` must be on a domain you've verified in Resend, OR use
    Resend's shared ``onboarding@resend.dev`` sender (which can only deliver to
    the email address that owns the Resend account).
  * A "Sending access" API key can POST emails but cannot read domains/keys.
"""
import logging
import os
from html import escape

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("whatsapp-email")


def _describe_resend_error(exc: Exception) -> str:
    """Surface Resend's actionable detail (code + message) instead of a bare repr.

    ``resend.exceptions.ResendError`` carries ``.code`` (HTTP status) and
    ``.message`` (e.g. "The gmail.com domain is not verified"). Other exceptions
    fall back to ``str(exc)``.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    return f"{code}: {message}" if code else message


@mcp.tool()
async def send_email(subject: str, body_md: str) -> str:
    """Send an email to the configured recipient via Resend.

    Requires RESEND_API_KEY, RESEND_FROM_EMAIL, and RESEND_TO_EMAIL
    environment variables. The body can be plain text or markdown-style prose.

    Args:
        subject: Email subject line.
        body_md: Email body — plain text or light markdown prose.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return "skipped: RESEND_API_KEY not configured"

    from_addr = os.environ.get("RESEND_FROM_EMAIL", "")
    to_addr = os.environ.get("RESEND_TO_EMAIL", from_addr)

    if not from_addr:
        return "skipped: RESEND_FROM_EMAIL not configured"

    try:
        import resend

        resend.api_key = api_key

        safe = escape(body_md)
        html = (
            "<!DOCTYPE html><html><body style='font-family:ui-monospace,monospace;"
            f"white-space:pre-wrap'>{safe}</body></html>"
        )

        result = resend.Emails.send(
            {
                "from": from_addr,
                "to": [to_addr],
                "subject": subject,
                "html": html,
            }
        )
        email_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        return f"sent (id {email_id})"
    except Exception as exc:
        detail = _describe_resend_error(exc)
        logger.error("Email send failed: %s", detail)
        if "not verified" in detail.lower() or "domain" in detail.lower():
            return (
                f"error: {detail} — verify the sender domain in Resend, or set "
                "RESEND_FROM_EMAIL to onboarding@resend.dev for testing."
            )
        return f"error: {detail}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
