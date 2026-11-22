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
from deep_harness.application.jobs import CreateJob, JobBudget
from deep_harness.application.workspace import PROJECT_PROMPT
from deep_harness.domain.models import DomainError, Harness
from deep_harness.runtime.inference import usage_totals
from deep_harness.server_config import number
from deep_harness.transports.events import stream_events
from deep_harness.transports.operations import operations_router


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSession(RequestModel):
    request_id: str = Field(min_length=1, max_length=number("api.request_id_max_length"))
    prompt: str = Field(default=PROJECT_PROMPT, max_length=number("api.prompt_max_length"))
    tools: tuple[str, ...] | None = None
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
    admission_id: str = Field(min_length=1, max_length=number("api.admission_id_max_length"))


class SubmitInput(RequestModel):
    submission_id: str = Field(min_length=1, max_length=number("api.submission_id_max_length"))
    text: str = Field(min_length=1, max_length=number("api.text_max_length"))


class ApprovalResponse(RequestModel):
    interaction_id: str = Field(min_length=1, max_length=number("api.interaction_id_max_length"))
    generation: int = Field(ge=1)
    decision: Literal["approve", "deny"]


class JobCommand(RequestModel):
    action: Literal["pause", "resume", "cancel", "accept"]
    position: int | None = Field(default=None, ge=0)
    evidence: str | None = Field(default=None, max_length=number("api.evidence_max_length"))


class ForkSession(RequestModel):
    request_id: str = Field(min_length=1, max_length=number("api.request_id_max_length"))


def create_api(host, token: str) -> FastAPI:
    if len(token) < number("transport.min_operator_token_chars"):
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
        # is described by the four tiers instead.
        return {"status": "ok", "stage": "development", "harnesses": list(manifest()["harnesses"])}

    @app.get("/profiles/{profile_id}/jobs", dependencies=[Depends(authenticate)])
    async def list_jobs(profile_id: str):
        service = await host.application(profile_id)
        return await service.jobs.ledger.list()

    @app.post("/profiles/{profile_id}/jobs", dependencies=[Depends(authenticate)])
    async def create_job(profile_id: str, body: CreateJob):
        service = await host.application(profile_id)
        return await service.create_job(body)

    @app.get("/profiles/{profile_id}/jobs/defaults", dependencies=[Depends(authenticate)])
    async def job_defaults(profile_id: str):
        await host.application(profile_id)
        return {
            "wall_seconds": JobBudget().wall_seconds,
            "wall_seconds_max": number("job_budget.wall_seconds_max"),
            "stages_max": number("jobs.stages_max_length"),
            "parallelism_max": number("jobs.parallelism_max"),
        }

    @app.get("/profiles/{profile_id}/jobs/{job_id}", dependencies=[Depends(authenticate)])
    async def job_detail(profile_id: str, job_id: str):
        service = await host.application(profile_id)
        return await service.jobs.detail(job_id)

    @app.post("/profiles/{profile_id}/jobs/{job_id}/commands", dependencies=[Depends(authenticate)])
    async def command_job(profile_id: str, job_id: str, body: JobCommand):
        service = await host.application(profile_id)
        return await service.command_job(
            job_id, body.action, position=body.position, evidence=body.evidence
        )

    @app.get("/profiles/{profile_id}/capabilities", dependencies=[Depends(authenticate)])
    async def capabilities(profile_id: str):
        """Built-in capabilities are a product contract, not a per-session toggle."""
        return manifest()

    @app.post("/profiles/{profile_id}/sessions", dependencies=[Depends(authenticate)])
    async def create(profile_id: str, body: CreateSession):
        service = await host.application(profile_id)
        engine = "deep" if body.harness != "general" else "standard"
        session = await service.create_session(
            body.request_id,
            body.prompt,
            body.tools,
            engine=engine,
            harness=body.harness,
            permission_mode=body.permission_mode or "ask",
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
        limit: Annotated[int, Query(ge=1, le=number("pagination.max_page_size"))] = number(
            "pagination.page_size"
        ),
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
            result = await service.sessions.status(session_id)
        else:
            result = await service.store.session_status(session_id, "operator")
        stage = await service.jobs.ledger.owner(session_id)
        if stage:
            result["job_id"] = stage["job_id"]
        return jsonable_encoder(result)

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
        limit: Annotated[int, Query(ge=1, le=number("pagination.max_page_size"))] = number(
            "pagination.page_size"
        ),
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
        q: Annotated[str, Query(min_length=1, max_length=number("pagination.search_query_chars"))],
        limit: Annotated[int, Query(ge=1, le=number("pagination.max_search_results"))] = number(
            "pagination.search_results"
        ),
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
        limit: Annotated[int, Query(ge=1, le=number("pagination.max_page_size"))] = number(
            "pagination.live_page_size"
        ),
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
        "/profiles/{profile_id}/sessions/{session_id}/submission",
        dependencies=[Depends(authenticate)],
    )
    async def submission(profile_id: str, session_id: str, submission_id: str):
        service = await host.application(profile_id)
        await service.store.get_session(session_id, "operator")
        admission = await service.store.find_submission(session_id, submission_id)
        return asdict(admission) if admission is not None else None

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
