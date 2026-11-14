"""Metadata-only probes distinguish missing, incompatible and unavailable local models."""

import httpx
import pytest

from deep_harness.application.config import load_model_settings
from deep_harness.runtime.readiness import check_model


@pytest.mark.parametrize(
    ("status", "body", "expected", "code"),
    [
        (200, {"capabilities": ["tools", "completion", "thinking"]}, "ready", None),
        (200, {"capabilities": ["completion"]}, "incompatible", None),
        (404, {"error": "not installed"}, "unavailable", "model_not_found"),
        (500, {"error": "private internal details"}, "unavailable", "ollama_http_error"),
        (200, {"capabilities": "bad"}, "unavailable", "invalid_ollama_response"),
    ],
)
async def test_ollama_probe_never_generates_or_downloads(status, body, expected, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    result = await check_model(load_model_settings(), transport=httpx.MockTransport(handler))
    assert result["status"] == expected
    assert result.get("code") == code
    assert len(calls) == 1
    assert calls[0].method == "POST"
    assert calls[0].url.path == "/api/show"
    assert b"qwen3.5:9b" in calls[0].content
    assert "private internal details" not in str(result)


async def test_probe_connection_failure_and_diagnostic_are_explicit():
    def handler(request):
        raise httpx.ConnectError("private endpoint failure", request=request)

    result = await check_model(load_model_settings(), transport=httpx.MockTransport(handler))
    assert result["code"] == "ollama_unreachable"
    diagnostic = await check_model(load_model_settings(provider="diagnostic"))
    assert diagnostic["network"] is False
    assert diagnostic["kind"] == "offline_diagnostic"
