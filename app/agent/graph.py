import asyncio
import logging
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent.state import AgentState
from app.config import get_settings
from app.agent.prompts import get_worker_prompt

logger = logging.getLogger(__name__)


def _is_broken_history(exc: Exception) -> bool:
    """True if *exc* indicates an unrecoverable conversation history.

    Happens when an assistant message with ``tool_calls`` was persisted but the
    matching tool responses weren't (e.g. a tool crashed mid-run). OpenAI then
    rejects every replay with a 400 about ``tool_call_ids`` lacking responses.
    """
    msg = str(exc).lower()
    return "tool_call_id" in msg or "must be followed by tool messages" in msg


def _make_checkpointer():
    """Return a Redis checkpointer if REDIS_URL is set, else fall back to in-memory.

    NOTE: ``AsyncRedisSaver`` requires a one-time ``await checkpointer.asetup()``
    to create its Redis search indices. The constructor itself is lazy (it does
    not open a connection), so it is safe to call from sync code; ``asetup`` is
    invoked on first use via ``WhatsAppAgent._ensure_checkpointer``.
    """
    settings = get_settings()
    redis_url = settings.redis_url

    if redis_url:
        try:
            from langgraph.checkpoint.redis.aio import AsyncRedisSaver

            checkpointer = AsyncRedisSaver(redis_url=redis_url)
            logger.info("Using Redis checkpointer at %s", redis_url)
            return checkpointer
        except Exception as exc:
            logger.warning("Redis checkpointer unavailable (%s); falling back to MemorySaver", exc)

    from langgraph.checkpoint.memory import MemorySaver
    logger.warning("Using in-memory checkpointer — conversation state will not persist across restarts")
    return MemorySaver()


class WhatsAppAgent:
    """LangGraph react agent: worker → (tools?) → worker → END.

    Tools are injected at construction time so that MCP-backed tools
    (loaded asynchronously during startup) can be provided without
    re-initialising the LLM on every request.

    Conversation state is persisted in Redis when REDIS_URL is configured,
    so any worker process can continue a user's thread.
    """

    def __init__(self, tools: list[BaseTool] | None = None) -> None:
        self.graph = None
        self.tools = tools or []
        settings = get_settings()
        llm = ChatOpenAI(model=settings.openai_model)
        self.llm = llm.bind_tools(self.tools) if self.tools else llm
        self.checkpointer = _make_checkpointer()
        self._checkpointer_ready = False
        self._setup_lock = asyncio.Lock()
        logger.info(
            "WhatsAppAgent initialised with %d tool(s): %s",
            len(self.tools),
            [t.name for t in self.tools],
        )

    def build(self) -> None:
        graph_builder = StateGraph(AgentState)
        graph_builder.add_node("worker", self._worker)
        if self.tools:
            # handle_tool_errors=True → a raising tool yields an error ToolMessage
            # instead of crashing the graph. Crashing mid-run would persist an
            # assistant message whose tool_calls never get responses, which then
            # bricks the whole thread (OpenAI 400 on every later turn).
            graph_builder.add_node(
                "tools", ToolNode(tools=self.tools, handle_tool_errors=True)
            )
            graph_builder.add_conditional_edges(
                "worker",
                self._worker_router,
                {"tools": "tools", "end": END},
            )
            graph_builder.add_edge("tools", "worker")
        else:
            graph_builder.add_edge("worker", END)
        graph_builder.add_edge(START, "worker")
        self.graph = graph_builder.compile(checkpointer=self.checkpointer)

    async def _worker(self, state: AgentState) -> dict[str, Any]:
        system = get_worker_prompt()
        messages = list(state["messages"])
        if not any(isinstance(m, SystemMessage) for m in messages):
            messages = [SystemMessage(content=system)] + messages

        response = await self.llm.ainvoke(messages)
        return {"messages": [response]}

    def _worker_router(self, state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return "end"

    async def _ensure_checkpointer(self) -> None:
        """Run the checkpointer's one-time async ``asetup`` (Redis index creation).

        ``MemorySaver`` has no ``asetup`` so this is a no-op for the dev fallback.
        Guarded by a lock so concurrent jobs in the same worker set up only once.
        """
        if self._checkpointer_ready:
            return
        async with self._setup_lock:
            if self._checkpointer_ready:
                return
            asetup = getattr(self.checkpointer, "asetup", None)
            if asetup is not None:
                try:
                    await asetup()
                except Exception as exc:
                    logger.warning("Checkpointer asetup failed (%s); continuing", exc)
            self._checkpointer_ready = True

    async def reply(self, wa_id: str, text: str, thread_id: str | None = None) -> str:
        """Run one turn.

        ``thread_id`` defaults to ``wa_id`` so each user has their own memory.
        Pass a distinct ``thread_id`` (e.g. ``"briefing:<wa_id>"``) for one-off
        flows that shouldn't pollute the user's conversation history, while still
        addressing tools/replies to the real ``wa_id``.
        """
        if self.graph is None:
            self.build()
        await self._ensure_checkpointer()

        config = {"configurable": {"thread_id": thread_id or wa_id}}
        payload = {"messages": [HumanMessage(content=text)], "wa_id": wa_id}

        try:
            result = await self.graph.ainvoke(payload, config=config)
        except Exception as exc:
            # A thread whose history has an assistant tool_calls message with no
            # matching tool responses is unrecoverable — OpenAI 400s on every
            # replay. Wipe it and retry once from a clean slate so a single bad
            # turn can't permanently brick the conversation.
            if _is_broken_history(exc):
                tid = config["configurable"]["thread_id"]
                logger.warning("Resetting corrupted thread %s and retrying (%s)", tid, exc)
                deleted = await self._delete_thread(config)
                # If the wipe worked, the same thread is now clean. If not, retry
                # on a fresh thread so the user still gets a reply this turn.
                retry_config = config if deleted else {
                    "configurable": {"thread_id": f"{tid}:r{uuid4().hex[:8]}"}
                }
                result = await self.graph.ainvoke(payload, config=retry_config)
            else:
                raise

        last = result["messages"][-1]
        content = getattr(last, "content", None) or str(last)
        return content if isinstance(content, str) else str(content)

    async def _delete_thread(self, config: dict) -> bool:
        """Wipe a thread's checkpoints. Returns True on success.

        Deletion lives on the checkpointer (``adelete_thread(thread_id)``), not
        on the compiled graph — the latter has no delete API in this version.
        """
        thread_id = config.get("configurable", {}).get("thread_id")
        adelete = getattr(self.checkpointer, "adelete_thread", None)
        if adelete is None:
            logger.warning("Checkpointer has no adelete_thread; cannot reset %s", thread_id)
            return False
        try:
            await adelete(thread_id)
            return True
        except Exception as exc:
            logger.warning("Could not delete thread %s: %s", thread_id, exc)
            return False

    async def reset_thread(self, wa_id: str) -> None:
        """Delete conversation checkpoint for wa_id."""
        if self.graph is None:
            return
        await self._delete_thread({"configurable": {"thread_id": wa_id}})
