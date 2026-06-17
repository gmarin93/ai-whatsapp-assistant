"""The interactive quick-menu shown on greetings / ``/menu`` / chat open.

The menu is a WhatsApp *list* message so it can surface every capability at once.
When the user taps a row, WhatsApp sends back an ``interactive`` message whose
reply ``id`` is one of the keys below; the webhook expands that id into a
natural-language instruction (``MENU_EXPANSIONS``) and feeds it to the agent, so
taps flow through the exact same agent pipeline as typed messages.
"""

MENU_BODY = "👋 Hi! I'm your assistant. Here's what I can do — tap an option:"
MENU_BUTTON_TEXT = "Choose an option"
MENU_SECTION_TITLE = "What I can do"

# (id, title ≤24 chars, description ≤72 chars). WhatsApp lists allow up to 10 rows.
MENU_ROWS: list[tuple[str, str, str]] = [
    ("menu_search", "🔎 Search the web", "Find up-to-date info online"),
    ("menu_email", "✉️ Send an email", "Compose and send an email"),
    ("menu_agenda", "📅 Today's agenda", "See today's calendar events"),
    ("menu_event", "🗓️ New calendar event", "Add an event to your calendar"),
    ("menu_reminder", "⏰ Set a reminder", "Get pinged at a later time"),
    ("menu_briefing", "📰 Daily briefing", "Calendar + matches + weather"),
]

# Tap id → the instruction handed to the agent. For tools that need details
# (search/email/event), the expansion makes the agent ask the right questions.
MENU_EXPANSIONS: dict[str, str] = {
    "menu_search": "I want to search the web. Ask me what I'd like to look up.",
    "menu_email": (
        "I want to send an email. Ask me for the recipient, subject and message, "
        "then confirm before sending."
    ),
    "menu_agenda": "What's on my calendar today?",
    "menu_event": (
        "I want to add a calendar event. Ask me for the title, date, time and "
        "duration, then confirm before creating it."
    ),
    "menu_reminder": "I'd like to set a reminder. Ask me what to remind me about and when.",
    "menu_briefing": (
        "Give me my morning briefing: today's calendar events, today's FIFA "
        "World Cup 2026 matches, and the weather in San José, Costa Rica."
    ),
}

# Plain-text greetings that should pop the menu (covers "open the chat" intent).
GREETINGS: set[str] = {
    "hi", "hello", "hey", "hola", "buenas", "buenos dias", "buenas tardes",
    "start", "/start", "menu", "/menu", "help", "/help",
}


def expand_tap(reply_id: str, fallback_title: str) -> str:
    """Map a tapped row/button id to its instruction; fall back to the title."""
    return MENU_EXPANSIONS.get(reply_id, fallback_title)


def is_menu_trigger(text: str) -> bool:
    """True if a plain-text message should show the menu."""
    return text.strip().lower() in GREETINGS
