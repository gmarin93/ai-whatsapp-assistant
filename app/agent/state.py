from typing import Annotated, Any, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    """Shared state for the LangGraph agent."""

    messages: Annotated[list[Any], add_messages]
    wa_id: str
    # Extend as you build features, e.g. mode: str, last_error: Optional[str]
