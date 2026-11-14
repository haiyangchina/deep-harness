"""Real Ollama adapter request contract, using a local HTTP transport without a model."""

import json

import httpx
import pytest
from ollama import AsyncClient

from deep_harness.bootstrap import ProjectHost
from deep_harness.cli import build_host, parser
from deep_harness.runtime.models import ollama_model


@pytest.mark.parametrize("streaming", [False, True])
async def test_ollama_sends_explicit_context_window_for_both_paths(streaming):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["options"]["num_ctx"] == 8192
        result = {
            "model": "local",
            "message": {"role": "assistant", "content": "ok"},
            "done": True,
            "prompt_eval_count": 6497,
            "eval_count": 1,
        }
        return httpx.Response(200, text=json.dumps(result) + "\n")

    model = ollama_model(model="local", base_url="http://ollama.test", context_window_tokens=8192)
    client = AsyncClient(host="http://ollama.test", transport=httpx.MockTransport(respond))
    model.model._async_client = client
    try:
        model = model.with_tool_specs([])
        if streaming:
            result = [row async for row in model.stream("Prompt", [], ())][-1]["response"]
        else:
            result = await model.complete("Prompt", [], ())
        assert result["content"] == "ok"
        assert len(requests) == 1
        assert model.identity["context_window_tokens"] == 8192
    finally:
        await client._client.aclose()


async def test_provider_context_rejection_is_actionable_and_not_unknown(tmp_path):
    def respond(request):
        return httpx.Response(
            400,
            json={
                "error": "request (6497 tokens) exceeds the available context size (4096 tokens)"
            },
        )

    model = ollama_model(model="local", base_url="http://ollama.test")
    client = AsyncClient(host="http://ollama.test", transport=httpx.MockTransport(respond))
    model.model._async_client = client
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Analyze")
        admission = await app.submit(session.id, "input", "Explain project")
        await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "interrupted"
        assert "context" in done.result.lower()
        assert "--context-window" in done.result
        assert (await app.sessions.status(session.id))["active_effects"] == []
    finally:
        await host.close()
        await client._client.aclose()


async def test_small_window_preflight_stops_before_provider_call(tmp_path):
    called = []

    def respond(request):
        called.append(request)
        raise AssertionError("Oversized request must never reach provider")

    model = ollama_model(model="local", base_url="http://ollama.test", context_window_tokens=4096)
    client = AsyncClient(host="http://ollama.test", transport=httpx.MockTransport(respond))
    model.model._async_client = client
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "分析" * 4000)
        admission = await app.submit(session.id, "input", "Explain")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "interrupted"
        assert called == []
    finally:
        await host.close()
        await client._client.aclose()


async def test_cli_context_override_reaches_model_and_frozen_identity(tmp_path):
    args = parser().parse_args(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--workspace",
            str(tmp_path),
            "--context-window",
            "16384",
            "repl",
        ]
    )
    host = build_host(args)
    try:
        app = await host.application("default")
        assert app.model.model.num_ctx == 16384
        session = await app.create_session("create", "Analyze")
        snapshot = (await app.sessions.status(session.id))["runtime"]["current_snapshot"]
        assert snapshot["model"]["context_window_tokens"] == 16384
    finally:
        await host.close()
