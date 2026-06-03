from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agent.state import AgentState
from app.agent.tools import get_tools
from app.config import get_settings
from app.agent.prompts import get_worker_prompt


class WhatsAppAgent:
    """Minimal LangGraph: worker → (tools?) → worker → END."""

    def __init__(self) -> None:
        self.graph = None
        self.tools = get_tools()
        settings = get_settings()
        llm = ChatOpenAI(model=settings.openai_model)
        self.llm = llm.bind_tools(self.tools) if self.tools else llm
        self.memory = MemorySaver()

    def build(self) -> None:
        graph_builder = StateGraph(AgentState)
        graph_builder.add_node("worker", self._worker)
        if self.tools:
            graph_builder.add_node("tools", ToolNode(tools=self.tools))
            graph_builder.add_conditional_edges(
                "worker",
                self._worker_router,
                {"tools": "tools", "end": END},
            )
            graph_builder.add_edge("tools", "worker")
        else:
            graph_builder.add_edge("worker", END)
        graph_builder.add_edge(START, "worker")
        self.graph = graph_builder.compile(checkpointer=self.memory)

    def _worker(self, state: AgentState) -> dict[str, Any]:
        system = get_worker_prompt()
        messages = list(state["messages"])
        if not any(isinstance(m, SystemMessage) for m in messages):
            messages = [SystemMessage(content=system)] + messages

        response = self.llm.invoke(messages)
        return {"messages": [response]}

    def _worker_router(self, state: AgentState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return "end"

    async def reply(self, wa_id: str, text: str) -> str:
        """Run one turn; thread_id = wa_id so each user has their own memory."""
        if self.graph is None:
            self.build()

        config = {"configurable": {"thread_id": wa_id}}
        result = await self.graph.ainvoke(
            {
                "messages": [HumanMessage(content=text)],
                "wa_id": wa_id,
            },
            config=config,
        )
        last = result["messages"][-1]
        content = getattr(last, "content", None) or str(last)
        return content if isinstance(content, str) else str(content)

    async def reset_thread(self, wa_id: str) -> None:
        """TODO: clear checkpoint for wa_id when you add /reset command."""
        pass
