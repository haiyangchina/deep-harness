"""LangChain model adapter and an explicitly offline diagnostic model."""

import asyncio
from contextlib import contextmanager
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langsmith import tracing_context

from deep_harness.application.tools import tool_definitions
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.server_config import number, setting


@contextmanager
def model_errors():
    from ollama import ResponseError
    from openai import APIConnectionError, APIStatusError

    try:
        yield
    except ResponseError as exc:
        if (
            exc.status_code == 400
            and "context" in exc.error.lower()
            and "exceed" in exc.error.lower()
        ):
            raise DomainError(
                "context_budget_exhausted",
                "Ollama rejected the context size. Increase --context-window (or "
                "model.context_window_tokens in TOML), or use a smaller input/new session. "
                "Inspect model support and available memory before increasing the window.",
            ) from exc
        if exc.status_code == 404:
            message = "Ollama could not find the configured model or API endpoint."
        elif exc.status_code in {502, 503, 504}:
            message = f"Ollama endpoint unavailable (HTTP {exc.status_code})."
        else:
            message = f"Ollama rejected the model request (HTTP {exc.status_code})."
        raise DomainError("ollama_response_error", message) from exc
    except (APIConnectionError, ConnectionError, httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise DomainError(
            "model_connection_failed", "Could not connect to the model endpoint."
        ) from exc
    except APIStatusError as exc:
        raise DomainError(
            "model_request_failed", f"Model endpoint rejected the request (HTTP {exc.status_code})."
        ) from exc


def to_langchain(message: dict):
    common = {"content": message.get("content", ""), "id": message.get("id")}
    role = message["role"]
    factories = {
        "user": lambda: HumanMessage(
            **common,
            additional_kwargs=(
                {"image_ids": message["image_ids"]} if message.get("image_ids") else {}
            ),
        ),
        "system": lambda: SystemMessage(**common),
        "assistant": lambda: AIMessage(
            **common,
            tool_calls=message.get("tool_calls", []),
            additional_kwargs=message.get("provider_extensions", {}),
        ),
        "tool": lambda: ToolMessage(**common, tool_call_id=message["tool_call_id"]),
    }
    return factories[role]()


def checked_response(response: dict) -> dict:
    """Refuse a silent empty answer from a model that actually generated tokens.

    Thinking-capable Ollama models (for example qwen3.5) emit the whole answer
    into a separate ``thinking`` field when reasoning is enabled. LangChain does
    not expose that streamed text in ``AIMessage.content`` or ``tool_calls``, so
    without this guard a run is recorded as ``completed`` with an empty result
    and the desktop UI renders nothing but tool output.
    """
    content = response.get("content")
    text = content.strip() if isinstance(content, str) else content
    if text or response.get("tool_calls"):
        return response
    produced = (response.get("usage") or {}).get("output_tokens") or 0
    if not produced:
        return response
    raise DomainError(
        "empty_model_response",
        "Model generated tokens but returned no answer text or tool calls. "
        "Thinking/reasoning output is not readable by this adapter; keep "
        "[model] reasoning = false (the default), or use a non-thinking model.",
    )


class LangChainModel:
    def __init__(
        self,
        model,
        request_timeout: float = number("model.remote_timeout_seconds"),
        *,
        identity=None,
        tool_specs=None,
    ):
        self.model = model
        self.request_timeout = request_timeout
        self.identity = identity or {
            "adapter": f"{type(model).__module__}.{type(model).__qualname__}"
        }
        self.tool_specs = tool_specs

    @property
    def context_budget_bytes(self):
        window = self.identity.get("context_window_tokens")
        if not window:
            return None
        # Reserve response and framing tokens, then approximate the remaining UTF-8 bytes.
        return (window - number("model.context_output_reserve_tokens")) * 4

    def with_tool_specs(self, definitions):
        return LangChainModel(
            self.model, self.request_timeout, identity=self.identity, tool_specs=definitions
        )

    async def complete(self, prompt: str, messages: list[dict], tools: tuple[str, ...]) -> dict:
        definitions = self.tool_specs if self.tool_specs is not None else tool_definitions(tools)
        bound = self.model.bind_tools(definitions) if definitions else self.model
        with tracing_context(enabled=False), model_errors():
            response = await bound.ainvoke(
                [SystemMessage(content=prompt), *(to_langchain(row) for row in messages)]
            )
        return checked_response(
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": response.tool_calls,
                "provider_extensions": response.additional_kwargs,
                "usage": response.usage_metadata,
                "response_metadata": response.response_metadata,
            }
        )

    async def stream(self, prompt, messages, tools):
        definitions = self.tool_specs if self.tool_specs is not None else tool_definitions(tools)
        bound = self.model.bind_tools(definitions) if definitions else self.model
        accumulated = None
        with tracing_context(enabled=False), model_errors():
            async for chunk in bound.astream(
                [SystemMessage(content=prompt), *(to_langchain(row) for row in messages)]
            ):
                accumulated = chunk if accumulated is None else accumulated + chunk
                if isinstance(chunk.content, str) and chunk.content:
                    yield {"kind": "delta", "text": chunk.content}
        if accumulated is not None:
            yield {
                "kind": "result",
                "response": checked_response(
                    {
                        "role": "assistant",
                        "content": accumulated.content,
                        "tool_calls": accumulated.tool_calls,
                        "provider_extensions": accumulated.additional_kwargs,
                        "usage": accumulated.usage_metadata,
                        "response_metadata": accumulated.response_metadata,
                    }
                ),
            }


class DiagnosticModel:
    """No LLM or network. /read and /write exercise the real tool/approval path."""

    request_timeout = number("model.diagnostic_timeout_seconds")
    identity = {"provider": "diagnostic", "version": 1}

    async def complete(self, prompt: str, messages: list[dict], tools: tuple[str, ...]) -> dict:
        last = messages[-1]
        if last["role"] == "tool":
            return {"role": "assistant", "content": f"Diagnostic tool result: {last['content']}"}
        text = last["content"]
        if text.startswith("/read "):
            name = "read"
            call = {"id": "read-1", "name": name, "args": {"path": text[6:]}}
            return {"role": "assistant", "content": "", "tool_calls": [call]}
        writes = (
            text.partition("\n")[2].splitlines() if text.startswith("/write-batch\n") else [text]
        )
        if writes and all(line.startswith("/write ") for line in writes):
            calls = []
            for index, line in enumerate(writes, 1):
                path, _, content = line[7:].partition(" ")
                calls.append(
                    {
                        "id": f"write-{index}",
                        "name": "write",
                        "args": {"path": path, "content": content},
                    }
                )
            return {"role": "assistant", "content": "", "tool_calls": calls}
        return {"role": "assistant", "content": f"Offline diagnostic (not an LLM): {text}"}


def openai_model(
    *,
    model: str,
    api_key: str,
    base_url: str | None = None,
    request_timeout: float = number("model.remote_timeout_seconds"),
    context_window_tokens: int | None = None,
):
    from langchain_openai import ChatOpenAI

    identity = {"provider": "openai", "name": model, "endpoint_hash": fingerprint(base_url)}
    if context_window_tokens is not None:
        identity["context_window_tokens"] = context_window_tokens
    return LangChainModel(
        ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_retries=number("model.remote_max_retries"),
            timeout=request_timeout,
        ),
        request_timeout,
        identity=identity,
    )


def deepseek_model(
    *, model, api_key, base_url, request_timeout, context_window_tokens, reasoning=False,
):
    from deep_harness.runtime.deepseek import DeepSeekChatModel

    endpoint = base_url or "https://api.deepseek.com"
    return LangChainModel(
        DeepSeekChatModel(
            model=model,
            api_key=api_key,
            base_url=endpoint,
            max_retries=number("model.remote_max_retries"),
            timeout=request_timeout,
            extra_body={"thinking": {"type": "enabled" if reasoning else "disabled"}},
            preserve_reasoning=reasoning,
        ),
        request_timeout,
        identity={
            "provider": "deepseek", "name": model, "endpoint_hash": fingerprint(endpoint),
            "context_window_tokens": context_window_tokens, "reasoning": reasoning,
        },
    )


def ollama_model(
    *,
    model: str,
    base_url: str,
    request_timeout: float = number("model.request_timeout_seconds"),
    context_window_tokens: int = number("model.context_window_tokens"),
    reasoning: bool = False,
    keep_alive: int | str | None = setting("model.keep_alive"),
):
    from langchain_ollama import ChatOllama

    # ChatOllama defaults reasoning=None, which follows the model template and
    # turns thinking ON for qwen3.5. In that mode the answer lives in Ollama's
    # `message.thinking` field, which langchain-ollama drops from streamed chunks;
    # the adapter then sees content="" and the run silently completes with no text.
    hostname = urlsplit(base_url).hostname
    try:
        loopback = ip_address(hostname).is_loopback
    except ValueError:
        loopback = (hostname or "").lower().rstrip(".") == "localhost"
    client_kwargs = {"timeout": request_timeout}
    if loopback:
        client_kwargs["trust_env"] = False
    return LangChainModel(
        ChatOllama(
            model=model,
            base_url=base_url,
            temperature=0,
            num_ctx=context_window_tokens,
            client_kwargs=client_kwargs,
            reasoning=reasoning,
            keep_alive=keep_alive,
        ),
        request_timeout,
        identity={
            "provider": "ollama",
            "name": model,
            "endpoint_hash": fingerprint(base_url),
            "temperature": 0,
            "context_window_tokens": context_window_tokens,
            "reasoning": reasoning,
        },
    )


async def with_timeout(awaitable, seconds: float):
    async with asyncio.timeout(seconds):
        return await awaitable
