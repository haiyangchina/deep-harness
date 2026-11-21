"""Local development HTTP API; credentials never enter model/checkpoint state."""

import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from deep_harness.application.capabilities import manifest
from deep_harness.application.workspace import PROJECT_PROMPT
from deep_harness.domain.models import DomainError, Harness
from deep_harness.runtime.inference import usage_totals
from deep_harness.transports.events import stream_events
from deep_harness.transports.operations import operations_router


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSession(RequestModel):
    request_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(default=PROJECT_PROMPT, max_length=32_000)
    tools: tuple[str, ...] | None = None
    engine: Literal["standard", "deep"] = "standard"
    harness: Harness = "general"
    permission_mode: Literal["ask", "auto", "full"] | None = None
    extensions: tuple[str, ...] = ()
    mcp_tools: tuple[str, ...] = ()
    risk_acknowledged: bool = False


class ChangePermissions(RequestModel):
    permission_mode: Literal["ask", "auto", "full"]
    expected_revision: int = Field(ge=0, strict=True)
    risk_acknowledged: bool = False


class RevokeReuse(RequestModel):
    admission_id: str = Field(min_length=1, max_length=256)


class SubmitInput(RequestModel):
    submission_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=128_000)


class ApprovalResponse(RequestModel):
    interaction_id: str = Field(min_length=1, max_length=256)
    generation: int = Field(ge=1)
    decision: Literal["approve", "deny"]


class ForkSession(RequestModel):
    request_id: str = Field(min_length=1, max_length=128)


class BindRuntime(RequestModel):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def create_api(host, token: str) -> FastAPI:
    if len(token) < 32:
        raise ValueError("Local operator token must contain at least 32 characters")

    @asynccontextmanager
    async def lifespan(app):
        yield
        await host.close()

    app = FastAPI(title="deep-harness development API", lifespan=lifespan)

    async def authenticate(authorization: Annotated[str | None, Header()] = None):
        if authorization is None or not secrets.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "A local operator token is required")

    app.include_router(operations_router(host, authenticate))

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        status = 404 if exc.code.endswith("_not_found") else 409
        return JSONResponse(status_code=status, content={"code": exc.code, "message": str(exc)})

    @app.get("/health")
    async def health():
        return {"status": "ok", "stage": "development", "engines": ["standard", "deep"]}

    @app.get("/profiles/{profile_id}/capabilities", dependencies=[Depends(authenticate)])
    async def capabilities(profile_id: str):
        """Built-in capabilities are a product contract, not a per-session toggle."""
        return manifest()

    @app.post("/profiles/{profile_id}/sessions", dependencies=[Depends(authenticate)])
    async def create(profile_id: str, body: CreateSession):
        service = await host.application(profile_id)
        session = await service.create_session(
            body.request_id,
            body.prompt,
            body.tools,
            engine=body.engine,
            harness=body.harness,
            permission_mode=body.permission_mode or "legacy",
            extensions=body.extensions,
            mcp_tools=body.mcp_tools,
            full_access_acknowledged=body.risk_acknowledged,
        )
        return asdict(session)

    @app.post(
        "/profiles/{profile_id}/sessions/{session_id}/permissions",
        dependencies=[Depends(authenticate)],
    )
    async def permissions(profile_id: str, session_id: str, body: ChangePermissions):
        service = await host.application(profile_id)
        return asdict(
            await service.set_permissions(
                session_id,
                body.permission_mode,
                body.expected_revision,
                body.risk_acknowledged,
            )
        )

    @app.post(
        "/profiles/{profile_id}/sessions/{session_id}/revoke-reuse",
        dependencies=[Depends(authenticate)],
    )
    async def revoke_reuse(profile_id: str, session_id: str, body: RevokeReuse):
        service = await host.application(profile_id)
        return await service.revoke_reuse(session_id, body.admission_id)

    @app.get("/profiles/{profile_id}/mcp/tools", dependencies=[Depends(authenticate)])
    async def mcp_tools(profile_id: str):
        service = await host.application(profile_id)
        return {"tools": await service.mcp.discover()}

    @app.get("/profiles/{profile_id}/sessions", dependencies=[Depends(authenticate)])
    async def sessions(
        profile_id: str,
        after: str = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        service = await host.application(profile_id)
        return jsonable_encoder(
            await service.store.list_sessions("operator", after=after, limit=limit)
        )

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/status",
        dependencies=[Depends(authenticate)],
    )
    async def status(profile_id: str, session_id: str, include_runtime: bool = True):
        service = await host.application(profile_id)
        if include_runtime:
            return jsonable_encoder(await service.sessions.status(session_id))
        return jsonable_encoder(await service.store.session_status(session_id, "operator"))

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/usage",
        dependencies=[Depends(authenticate)],
    )
    async def usage(profile_id: str, session_id: str):
        service = await host.application(profile_id)
        await service.store.get_session(session_id, "operator")
        return await usage_totals(service.store, session_id)

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/delegations",
        dependencies=[Depends(authenticate)],
    )
    async def delegations(profile_id: str, session_id: str):
        service = await host.application(profile_id)
        return {"delegations": await service.list_delegations(session_id)}

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/delegations/{delegation_id}",
        dependencies=[Depends(authenticate)],
    )
    async def delegation(profile_id: str, session_id: str, delegation_id: str):
        service = await host.application(profile_id)
        return await service.get_delegation(session_id, delegation_id)

    @app.post(
        "/profiles/{profile_id}/sessions/{session_id}/delegations/{delegation_id}/steers",
        status_code=202,
        dependencies=[Depends(authenticate)],
    )
    async def steer_delegation(
        profile_id: str, session_id: str, delegation_id: str, body: SubmitInput
    ):
        service = await host.application(profile_id)
        return asdict(
            await service.steer_delegation(session_id, delegation_id, body.submission_id, body.text)
        )

    @app.post(
        "/profiles/{profile_id}/sessions/{session_id}/delegations/{delegation_id}/stop",
        dependencies=[Depends(authenticate)],
    )
    async def stop_delegation(profile_id: str, session_id: str, delegation_id: str):
        service = await host.application(profile_id)
        return await service.stop_delegation(session_id, delegation_id)

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/messages",
        dependencies=[Depends(authenticate)],
    )
    async def messages_page(
        profile_id: str,
        session_id: str,
        after: str = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        service = await host.application(profile_id)
        await service.store.get_session(session_id, "operator")
        return await service.store.history_page(session_id, after=after, limit=limit)

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/messages/search",
        dependencies=[Depends(authenticate)],
    )
    async def search_messages(
        profile_id: str,
        session_id: str,
        q: Annotated[str, Query(min_length=1, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ):
        service = await host.application(profile_id)
        await service.store.get_session(session_id, "operator")
        return {"messages": await service.store.search_history(session_id, q, limit=limit)}

    @app.post(
        "/profiles/{profile_id}/sessions/{session_id}/forks",
        dependencies=[Depends(authenticate)],
    )
    async def fork(profile_id: str, session_id: str, body: ForkSession):
        service = await host.application(profile_id)
        return asdict(await service.fork_session(session_id, body.request_id))

    @app.post(
        "/profiles/{profile_id}/sessions/{session_id}/runtime-binding",
        dependencies=[Depends(authenticate)],
    )
    async def bind(profile_id: str, session_id: str, body: BindRuntime):
        service = await host.application(profile_id)
        return jsonable_encoder(await service.bind_runtime(session_id, body.fingerprint))

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/events/stream",
        dependencies=[Depends(authenticate)],
    )
    async def events_stream(
        request: Request,
        profile_id: str,
        session_id: str,
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        service = await host.application(profile_id)
        await service.store.get_session(session_id, "operator")
        if last_event_id is not None:
            if (
                not last_event_id.isascii()
                or not last_event_id.isdecimal()
                or len(last_event_id) > 20
            ):
                raise HTTPException(422, "Last-Event-ID must be a non-negative integer")
            after = max(after, int(last_event_id))
            if after > 2**63 - 1:
                raise HTTPException(422, "Last-Event-ID exceeds the journal sequence range")
        return StreamingResponse(
            stream_events(service.store, session_id, after, request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/profiles/{profile_id}/sessions/{session_id}/inputs",
        status_code=202,
        dependencies=[Depends(authenticate)],
    )
    async def submit(profile_id: str, session_id: str, body: SubmitInput):
        service = await host.application(profile_id)
        return asdict(await service.submit(session_id, body.submission_id, body.text))

    @app.get("/profiles/{profile_id}/sessions/{session_id}", dependencies=[Depends(authenticate)])
    async def snapshot(profile_id: str, session_id: str):
        service = await host.application(profile_id)
        session = await service.store.get_session(session_id, "operator")
        return {"session": asdict(session), "messages": await service.store.history(session_id)}

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/messages/live",
        dependencies=[Depends(authenticate)],
    )
    async def live_messages(
        profile_id: str,
        session_id: str,
        after: Annotated[int | None, Query(ge=0, le=2**53 - 1)] = None,
        before: Annotated[int | None, Query(ge=1, le=2**53 - 1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ):
        service = await host.application(profile_id)
        await service.store.get_session(session_id, "operator")
        return await service.store.live_history(session_id, after=after, before=before, limit=limit)

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/events", dependencies=[Depends(authenticate)]
    )
    async def event_history(
        profile_id: str,
        session_id: str,
        after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        tail: bool = False,
    ):
        service = await host.application(profile_id)
        await service.store.get_session(session_id, "operator")
        return {"events": await service.store.event_history(session_id, after, tail=tail)}

    @app.get(
        "/profiles/{profile_id}/sessions/{session_id}/admissions/{admission_id}",
        dependencies=[Depends(authenticate)],
    )
    async def admission_status(profile_id: str, session_id: str, admission_id: str):
        service = await host.application(profile_id)
        await service.store.get_session(session_id, "operator")
        return asdict(await service.store.get_admission(admission_id, session_id))

    @app.post(
        "/profiles/{profile_id}/sessions/{session_id}/approvals",
        status_code=202,
        dependencies=[Depends(authenticate)],
    )
    async def respond(profile_id: str, session_id: str, body: ApprovalResponse):
        service = await host.application(profile_id)
        admission_id = await service.approve(
            session_id,
            body.interaction_id,
            body.generation,
            body.decision,
        )
        return {"admission_id": admission_id}

    return app
