"""Common HTTP application setup and versioned error contract."""

from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from deep_harness.domain.models import DomainError
from deep_harness.server_config import number


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobCommand(RequestModel):
    action: Literal["pause", "resume", "cancel", "accept"]
    position: int | None = Field(default=None, ge=0)
    evidence: str | None = Field(default=None, max_length=number("api.evidence_max_length"))


def workspace_error_status(exc):
    if exc.code in {"project_registration_denied", "operation_denied"}:
        return 403
    if exc.code.endswith("_not_found"):
        return 404
    if exc.code == "queue_full":
        return 429
    if exc.code == "upload_too_large":
        return 413
    if exc.code in {"model_secret_missing", "sandbox_unavailable", "draining"}:
        return 503
    return 409


def create_http_app(*, lifespan, legacy_error_status=None):
    app = FastAPI(title="deep-harness", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        status = workspace_error_status(exc)
        if legacy_error_status is not None and not request.url.path.startswith("/api/v1/"):
            status = legacy_error_status(exc)
        return JSONResponse({"code": exc.code, "message": str(exc)}, status)

    @app.middleware("http")
    async def prevent_api_caching(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
