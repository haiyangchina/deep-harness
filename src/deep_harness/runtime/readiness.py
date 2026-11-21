"""Explicit, metadata-only model readiness checks; never generate or pull a model."""

import httpx
from pydantic import BaseModel, ValidationError

from deep_harness.server_config import number


class OllamaDetails(BaseModel):
    capabilities: list[str]


async def check_model(settings, *, transport=None):
    result = {"provider": settings.provider, "model": settings.name}
    if settings.provider == "diagnostic":
        return {**result, "status": "ready", "network": False, "kind": "offline_diagnostic"}
    if settings.provider != "ollama":
        return {**result, "status": "unsupported", "code": "readiness_provider_not_supported"}
    if not settings.base_url:
        return {**result, "status": "unavailable", "code": "missing_base_url"}
    try:
        async with httpx.AsyncClient(
            timeout=number("model.probe_timeout_seconds"), transport=transport, trust_env=False
        ) as client:
            response = await client.post(
                settings.base_url.rstrip("/") + "/api/show",
                json={"model": settings.name, "verbose": False},
            )
            response.raise_for_status()
            details = OllamaDetails.model_validate(response.json())
    except httpx.TimeoutException:
        return {**result, "status": "unavailable", "code": "model_probe_timeout"}
    except httpx.HTTPStatusError as exc:
        code = "model_not_found" if exc.response.status_code == 404 else "ollama_http_error"
        return {**result, "status": "unavailable", "code": code}
    except httpx.RequestError:
        return {**result, "status": "unavailable", "code": "ollama_unreachable"}
    except (ValueError, ValidationError):
        return {**result, "status": "unavailable", "code": "invalid_ollama_response"}
    capabilities = sorted(set(details.capabilities))
    required = {"completion", "tools"}
    return {
        **result,
        "status": "ready" if required.issubset(capabilities) else "incompatible",
        "capabilities": capabilities,
        "missing_capabilities": sorted(required.difference(capabilities)),
    }
