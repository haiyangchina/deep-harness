"""Public SDK integration smoke tests using actual LangChain and Deep Agents."""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from deep_harness.runtime.deep import build_readonly_probe
from deep_harness.runtime.models import LangChainModel


class LocalChatModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "harness-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="SDK integration works"))]
        )


async def test_langchain_adapter_runs_real_model_interface_without_network():
    adapter = LangChainModel(LocalChatModel())
    result = await adapter.complete(
        "Frozen prompt",
        [{"id": "u1", "role": "user", "content": "hello"}],
        ("read",),
    )
    assert result["content"] == "SDK integration works"
    assert result["tool_calls"] == []


async def test_deepagents_compiles_and_executes_with_readonly_virtual_backend():
    graph = build_readonly_probe(LocalChatModel(), InMemorySaver())
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "Say hello without using tools"}]},
        {"configurable": {"thread_id": "deep-test"}},
    )
    assert result["messages"][-1].content == "SDK integration works"
