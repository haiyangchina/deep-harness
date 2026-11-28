"""Same-origin, login-free HTTP entry for a configured instance."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import unquote

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from deep_harness.application.capabilities import manifest
from deep_harness.application.instance_config import is_loopback
from deep_harness.application.jobs import CreateJob, JobBudget
from deep_harness.domain.images import MAX_IMAGES
from deep_harness.domain.models import DomainError, Harness, WorkspaceMode
from deep_harness.runtime.inference import usage_totals
from deep_harness.server_config import number
from deep_harness.transports.events import stream_events
from deep_harness.transports.http import JobCommand, RequestModel, create_http_app
from deep_harness.transports.operations import (
    DevelopmentCommand,
    EffectDecision,
    TaskControl,
    operations_router,
)

WORKSPACE = "/workspaces/{workspace_id}"
STATIC = Path(__file__).with_name("web_static")


class WebSession(RequestModel):
    request_id: str = Field(min_length=1, max_length=128)
    model_id: str | None = Field(default=None, max_length=48)
    agent_id: str | None = Field(default=None, max_length=48)
    prompt: str = Field(default="Help with the current task.", min_length=1, max_length=16000)
    harness: Harness = "general"
    tools: tuple[str, ...] | None = Field(default=None, max_length=64)
    verification_command: str | None = Field(default=None, min_length=1, max_length=4096)
    permission_mode: Literal["ask", "auto", "full"] | None = None
    extensions: tuple[str, ...] = ()
    mcp_tools: tuple[str, ...] = ()
    risk_acknowledged: bool = False
    workspace_mode: WorkspaceMode | None = None


class WebProject(RequestModel):
    path: str = Field(min_length=1, max_length=4096)
    label: str = Field(default="", max_length=120)
    mode: Literal["read_only", "sandbox"] = "read_only"


class WebInput(RequestModel):
    submission_id: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=64000)
    image_ids: tuple[str, ...] = Field(default=(), max_length=MAX_IMAGES)
    artifact_ids: tuple[str, ...] = Field(default=(), max_length=8)


class WebDecision(RequestModel):
    decision: str = Field(pattern=r"^(approve|deny)$")
    generation: int = Field(ge=0)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class WebReconciliation(RequestModel):
    generation: int = Field(ge=0)
    evidence: str = Field(min_length=1, max_length=2000)
    effects: list[EffectDecision] = Field(max_length=64)


class WebWorkflow(RequestModel):
    request_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=48)
    goal: str = Field(min_length=1, max_length=2000)
    model_id: str | None = Field(default=None, max_length=48)


class PermissionChange(RequestModel):
    permission_mode: Literal["ask", "auto", "full"]
    expected_revision: int = Field(ge=0, strict=True)
    risk_acknowledged: bool = False


class ForkRequest(RequestModel):
    request_id: str = Field(min_length=1, max_length=128)


class RevokeRequest(RequestModel):
    admission_id: str = Field(min_length=1, max_length=256)


class SessionMetadataRequest(RequestModel):
    expected_revision: int = Field(ge=0, strict=True)
    title: str | None = Field(default=None, max_length=160)
    pinned: bool | None = Field(default=None, strict=True)


def run_payload(admission):
    return {
        "run_id": admission.id,
        "session_id": admission.session_id,
        "status": admission.status,
        "generation": admission.generation,
        "result": admission.result,
    }


def public_messages(page):
    for row in page["items"]:
        message = row["message"]
        message.pop("provider_extensions", None)
        message.pop("response_metadata", None)
        content = message.get("content")
        if isinstance(content, list):
            message["content"] = [
                part
                for part in content
                if isinstance(part, str)
                or (isinstance(part, dict) and part.get("type") in {"text", "output_text"})
            ]
    return page


def create_web_api(instance, assets=STATIC):
    @asynccontextmanager
    async def lifespan(app):
        await instance.start()
        try:
            yield
        finally:
            await instance.close()

    app = create_http_app(lifespan=lifespan)
    settings = instance.settings.web
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts), www_redirect=False
    )
    public_origin = settings.public_origin.rstrip("/")

    def local_projects(request):
        return (
            settings.local_projects
            and request.client is not None
            and is_loopback(request.client.host)
        )

    @app.middleware("http")
    async def same_origin(request, call_next):
        route = request.url.path.split("/")
        if (
            route[:4] == ["", "api", "v1", "workspaces"]
            and len(route) > 4
            and route[4] in instance.registered
            and route[4] not in instance.settings.workspaces
            and not local_projects(request)
        ):
            return JSONResponse(
                {"code": "workspace_not_found", "message": "Workspace is not configured"}, 404
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            if (origin is not None and origin != public_origin) or fetch_site == "cross-site":
                return JSONResponse({"code": "origin_denied", "message": "Origin denied"}, 403)
            if request.url.path.startswith("/api/") and not (
                request.method == "DELETE" and request.headers.get("content-length") in {None, "0"}
                and "transfer-encoding" not in request.headers
            ):
                mime = request.headers.get("content-type", "").split(";", 1)[0].lower()
                allowed = (
                    (
                        mime == "application/octet-stream"
                        and request.headers.get("x-harness-upload") == "1"
                    )
                    if request.url.path.endswith(("/attachments", "/images"))
                    else mime == "application/json"
                )
                if not allowed:
                    return JSONResponse(
                        {"code": "invalid_content_type", "message": "Unsupported content type"}, 415
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; "
                "object-src 'none'; base-uri 'none'"
            )
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "web"}

    router = APIRouter(prefix="/api/v1")

    def require_local(request):
        if not local_projects(request):
            raise DomainError("project_registration_denied", "请在本机 Web 工作台添加项目。")

    @router.get("/instance")
    async def catalog(request: Request):
        return {
            **instance.public_catalog(),
            "workspaces": available_workspaces(request),
            "local_projects": local_projects(request),
            "project_sandbox": instance.can_add_sandbox,
        }

    @router.get("/models")
    async def models():
        return instance.models.list()

    @router.get("/workspaces")
    async def workspaces(request: Request):
        return available_workspaces(request)

    def available_workspaces(request):
        rows = instance.public_catalog()["workspaces"]
        if local_projects(request):
            return rows
        return [row for row in rows if row["id"] in instance.settings.workspaces]

    @router.get("/project-folders")
    async def project_folders(request: Request, path: str = Query("", max_length=4096)):
        require_local(request)
        initial = instance.settings.workspaces[instance.settings.default_workspace].path.parent
        return await asyncio.to_thread(instance.projects.browse, path, initial)

    @router.post("/workspaces")
    async def add_workspace(request: Request, body: WebProject):
        require_local(request)
        return await instance.add_project(body.path, body.label.strip(), body.mode)

    @router.get("/agents")
    async def agents():
        return instance.public_catalog()["agents"]

    app.include_router(router)
    app.include_router(workspace_router(instance))

    async def knowledge_access(profile_id: str):
        workspace = instance.workspaces.get(profile_id)
        if workspace is None:
            raise DomainError("workspace_not_found", "Workspace is not configured")
        if not workspace.manage_knowledge:
            raise DomainError("operation_denied", "Knowledge management is disabled")

    app.include_router(operations_router(
        instance, knowledge_access, versioned=True, knowledge_only=True,
    ))
    if assets is not None:
        app.mount("/", StaticFiles(directory=assets, html=True), name="web-assets")
    return app


def workspace_router(instance, *, dependencies=(), local=False):
    """The versioned workspace contract used by both server and desktop launchers."""
    router = APIRouter(prefix="/api/v1", dependencies=list(dependencies))

    @router.get(WORKSPACE + "/capabilities")
    async def capabilities(workspace_id: str):
        service = await instance.application(workspace_id)
        return {
            **manifest(),
            "operations": {
                "manage_permissions": local,
                "manage_knowledge": local or instance.workspaces[workspace_id].manage_knowledge,
                "manage_delivery": local,
                "native_commands": local,
                "sandbox_commands": service.scope.server_mode == "sandbox",
                "connector_status": not local,
            },
        }

    @router.get(WORKSPACE + "/mcp/tools")
    async def mcp_tools(workspace_id: str):
        service = await instance.application(workspace_id)
        if not local:
            raise DomainError("operation_denied", "MCP is managed in the instance configuration")
        return {"tools": await service.mcp.discover()}

    @router.post(WORKSPACE + "/sessions")
    async def create_session(workspace_id: str, body: WebSession):
        return asdict(await instance.create_session(workspace_id, body))

    @router.get(WORKSPACE + "/sessions")
    async def sessions(
        workspace_id: str, after: str = Query("", max_length=256),
        limit: int = Query(50, ge=1, le=100),
    ):
        service = await instance.application(workspace_id)
        return await service.session_management.page("operator", after, limit)

    @router.get(WORKSPACE + "/session-deletions")
    async def session_deletions(workspace_id: str):
        service = await instance.application(workspace_id)
        return {"deletions": await service.session_management.cleanup_queue()}

    @router.patch(WORKSPACE + "/sessions/{session_id}")
    async def session_metadata(workspace_id: str, session_id: str, body: SessionMetadataRequest):
        service = await instance.application(workspace_id)
        changes = body.model_dump(exclude={"expected_revision"}, exclude_none=True)
        if not changes:
            raise DomainError("session_metadata_invalid", "Provide a title or pin state")
        if "title" in changes:
            title = changes["title"].strip()
            if not 1 <= len(title) <= 80 or any(ord(letter) < 32 for letter in title):
                raise DomainError("session_metadata_invalid", "Enter a title of 1 to 80 characters")
            changes["title"] = title
        return await service.change_session_metadata(session_id, body.expected_revision, changes)

    @router.delete(WORKSPACE + "/sessions/{session_id}", status_code=202)
    async def delete_session(
        workspace_id: str, session_id: str, request: Request, response: Response,
    ):
        service = await instance.application(workspace_id)
        result = await service.delete_session(session_id)
        response.headers["Location"] = request.url_for(
            "session_deletion_status", workspace_id=workspace_id, session_id=session_id,
        ).path
        return result

    @router.get(WORKSPACE + "/sessions/{session_id}/deletion")
    async def session_deletion_status(workspace_id: str, session_id: str):
        service = await instance.application(workspace_id)
        return await service.session_deletion.status(session_id)

    @router.get(WORKSPACE + "/sessions/{session_id}")
    async def session(workspace_id: str, session_id: str, include_runtime: bool = False):
        service = await instance.application(workspace_id)
        if local and include_runtime:
            status = await service.sessions.status(session_id)
        else:
            status = await service.store.session_status(session_id, "operator")
        binding = await service.sessions.policies.get(session_id)
        # Keep frozen prompts and connector connection configuration out of runtime diagnostics.
        snapshot = binding["snapshot"] if binding else {}
        coding_task = snapshot.get("coding_task")
        status["managed_task"] = bool(coding_task and not coding_task.get("owner"))
        status["model_id"] = snapshot.get("model_id")
        status["vision"] = service.sessions.supports_vision(status["model_id"])
        status["model_revision"] = snapshot.get("model_revision")
        stage = await service.jobs.ledger.owner(session_id)
        status["pause_supported"] = stage is None and not (coding_task or {}).get("owner")
        status["pause_requested"] = service.workspaces.paused(session_id)
        if stage:
            status["job_id"] = stage["job_id"]
        encoded = jsonable_encoder(status)
        encoded["session"].update(await service.session_management.presentation(session_id))
        return encoded

    @router.get(WORKSPACE + "/sessions/{session_id}/messages")
    async def messages(
        workspace_id: str,
        session_id: str,
        before: int | None = Query(None, ge=0),
        after: int | None = Query(None, ge=0, le=2**53 - 1),
    ):
        service = await instance.application(workspace_id)
        await service.store.get_session(session_id, "operator")
        page = await service.store.live_history(session_id, before=before, after=after)
        return page if local else public_messages(page)

    @router.get(WORKSPACE + "/sessions/{session_id}/messages/search")
    async def search_messages(
        workspace_id: str, session_id: str,
        q: str = Query(min_length=1, max_length=number("pagination.search_query_chars")),
    ):
        service = await instance.application(workspace_id)
        await service.store.get_session(session_id, "operator")
        rows = await service.store.search_history(session_id, q)
        if not local:
            rows = [item["message"] for item in public_messages(
                {"items": [{"message": row} for row in rows]}
            )["items"]]
        return {"messages": rows}

    @router.get(WORKSPACE + "/sessions/{session_id}/usage")
    async def session_usage(workspace_id: str, session_id: str):
        service = await instance.application(workspace_id)
        await service.store.get_session(session_id, "operator")
        return await usage_totals(service.store, session_id)

    @router.get(WORKSPACE + "/sessions/{session_id}/delegations")
    async def delegations(workspace_id: str, session_id: str):
        service = await instance.application(workspace_id)
        return {"delegations": await service.list_delegations(session_id)}

    @router.post(
        WORKSPACE + "/sessions/{session_id}/delegations/{delegation_id}/steers", status_code=202,
    )
    async def steer_delegation(
        workspace_id: str, session_id: str, delegation_id: str, body: WebInput,
    ):
        service = await instance.application(workspace_id)
        return await service.steer_delegation(
            session_id, delegation_id, body.submission_id, body.text,
        )

    @router.post(WORKSPACE + "/sessions/{session_id}/delegations/{delegation_id}/stop")
    async def stop_delegation(workspace_id: str, session_id: str, delegation_id: str):
        service = await instance.application(workspace_id)
        return await service.stop_delegation(session_id, delegation_id)

    @router.post(WORKSPACE + "/sessions/{session_id}/forks")
    async def fork(workspace_id: str, session_id: str, body: ForkRequest):
        service = await instance.application(workspace_id)
        return await service.fork_session(session_id, body.request_id)

    @router.post(WORKSPACE + "/sessions/{session_id}/permissions")
    async def permissions(workspace_id: str, session_id: str, body: PermissionChange):
        service = await instance.application(workspace_id)
        if not local:
            raise DomainError("operation_denied", "Server sessions use explicit approval")
        return await service.set_permissions(
            session_id, body.permission_mode, body.expected_revision, body.risk_acknowledged,
        )

    @router.post(WORKSPACE + "/sessions/{session_id}/revoke-reuse")
    async def revoke_reuse(workspace_id: str, session_id: str, body: RevokeRequest):
        service = await instance.application(workspace_id)
        if not local:
            raise DomainError("operation_denied", "Server sessions use explicit approval")
        return await service.revoke_reuse(session_id, body.admission_id)

    @router.get(WORKSPACE + "/sessions/{session_id}/event-history")
    async def event_history(
        workspace_id: str,
        session_id: str,
        after: int = Query(0, ge=0, le=2**53 - 1),
        tail: bool = False,
        wait: float = Query(0, ge=0, le=12),
    ):
        service = await instance.application(workspace_id)
        await service.store.get_session(session_id, "operator")
        store = service.store
        clock = asyncio.get_running_loop().time
        deadline = clock() + wait
        while True:
            revision = store.revision
            events = await store.event_history(session_id, after, tail=tail)
            remaining = deadline - clock()
            if events or tail or remaining <= 0:
                return {"events": events}
            try:
                async with asyncio.timeout(remaining):
                    await store.wait_for_change(revision)
            except TimeoutError:
                pass
            await store.get_session(session_id, "operator")

    @router.post(WORKSPACE + "/sessions/{session_id}/runs", status_code=202)
    async def submit(workspace_id: str, session_id: str, body: WebInput):
        if len(set(body.artifact_ids)) != len(body.artifact_ids):
            raise DomainError("artifact_selection", "Attachment IDs must be unique")
        result = await instance.submit(
            workspace_id,
            session_id,
            body.submission_id,
            body.text,
            body.artifact_ids,
            body.image_ids,
        )
        return run_payload(result)

    @router.get(WORKSPACE + "/sessions/{session_id}/submission")
    async def submission(
        workspace_id: str, session_id: str, submission_id: str = Query(max_length=128)
    ):
        service = await instance.application(workspace_id)
        await service.store.get_session(session_id, "operator")
        row = await service.store.find_submission(session_id, submission_id)
        return run_payload(row) if row else None

    @router.get(WORKSPACE + "/runs/{run_id}")
    async def run(workspace_id: str, run_id: str):
        service = await instance.application(workspace_id)
        session_id = await service.store.run_session(run_id)
        result = run_payload(await service.store.get_admission(run_id, session_id))
        result["usage"] = await usage_totals(service.store, session_id, run_id)
        return result

    @router.get(WORKSPACE + "/runs/{run_id}/diff")
    async def run_diff(workspace_id: str, run_id: str):
        service = await instance.application(workspace_id)
        session_id = await service.store.run_session(run_id)
        admission = await service.store.get_admission(run_id, session_id)
        if admission.status == "completed":
            try:
                workspace = json.loads(admission.result)["execution_evidence"]["workspace"]
                return Response(workspace["diff"], media_type="text/x-diff", headers={
                    "Content-Disposition": 'attachment; filename="coding-task.diff"',
                    "X-Diff-Partial": str(workspace["diff_truncated"]).lower(),
                })
            except (KeyError, TypeError, ValueError):
                pass
        raise DomainError("diff_not_found", "No saved coding diff is available")

    @router.get(WORKSPACE + "/sessions/{session_id}/task-control")
    async def task_status(workspace_id: str, session_id: str):
        service = await instance.application(workspace_id)
        return await service.task_control(session_id)

    @router.get(WORKSPACE + "/sessions/{session_id}/bundle")
    async def export_bundle(workspace_id: str, session_id: str):
        service = await instance.application(workspace_id)
        identifier, raw = await service.deliver(session_id, "export")
        return Response(raw, media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="changes-{identifier[:12]}.json"',
        })

    @router.post(WORKSPACE + "/sessions/{session_id}/task-control")
    async def task_control(workspace_id: str, session_id: str, body: TaskControl):
        service = await instance.application(workspace_id)
        return await service.task_control(session_id, body.action)

    @router.get(WORKSPACE + "/sessions/{session_id}/processes")
    async def development_processes(workspace_id: str, session_id: str):
        service = await instance.application(workspace_id)
        return await service.development_processes(session_id)

    @router.post(WORKSPACE + "/sessions/{session_id}/processes")
    async def start_process(workspace_id: str, session_id: str, body: DevelopmentCommand):
        service = await instance.application(workspace_id)
        return await service.development_processes(session_id, start=body.model_dump())

    @router.post(WORKSPACE + "/sessions/{session_id}/processes/{identifier}/stop")
    async def stop_process(workspace_id: str, session_id: str, identifier: str):
        service = await instance.application(workspace_id)
        return await service.development_processes(session_id, stop=identifier)

    @router.get(WORKSPACE + "/runs/{run_id}/events")
    async def run_events(
        request: Request,
        workspace_id: str,
        run_id: str,
        after: int = Query(0, ge=0, le=2**63 - 1),
        last_event_id: Annotated[int | None, Header(ge=0, le=2**63 - 1)] = None,
    ):
        service = await instance.application(workspace_id)
        session_id = await service.store.run_session(run_id)
        cursor = last_event_id if last_event_id is not None else after
        return StreamingResponse(
            stream_events(
                service.store, session_id, cursor, request.is_disconnected, run_id=run_id
            ),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @router.get(WORKSPACE + "/sessions/{session_id}/events")
    async def events(
        request: Request,
        workspace_id: str,
        session_id: str,
        after: int = Query(0, ge=0, le=2**63 - 1),
        last_event_id: Annotated[int | None, Header(ge=0, le=2**63 - 1)] = None,
    ):
        service = await instance.application(workspace_id)
        await service.store.get_session(session_id, "operator")
        cursor = last_event_id if last_event_id is not None else after
        return StreamingResponse(
            stream_events(service.store, session_id, cursor, request.is_disconnected),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @router.post(WORKSPACE + "/runs/{run_id}/approvals/{approval_id}/decision")
    async def decide(workspace_id: str, run_id: str, approval_id: str, body: WebDecision):
        service = await instance.application(workspace_id)
        session_id = await service.store.run_session(run_id)
        await service.receipts.verify_proposal(run_id, approval_id, body.proposal_hash)
        await service.approve(session_id, approval_id, body.generation, body.decision)
        return {"run_id": run_id, "decision": body.decision}

    @router.post(WORKSPACE + "/runs/{run_id}/cancel")
    async def cancel(workspace_id: str, run_id: str):
        service = await instance.application(workspace_id)
        session_id = await service.store.run_session(run_id)
        return jsonable_encoder(await service.cancel(session_id, run_id))

    @router.post(WORKSPACE + "/runs/{run_id}/retry-workspace")
    async def retry_workspace(workspace_id: str, run_id: str):
        service = await instance.application(workspace_id)
        return await service.retry_workspace(await service.store.run_session(run_id), run_id)

    @router.post(WORKSPACE + "/runs/{run_id}/retry-summary")
    async def retry_summary(workspace_id: str, run_id: str):
        service = await instance.application(workspace_id)
        return await service.retry_summary(await service.store.run_session(run_id), run_id)

    @router.post(WORKSPACE + "/runs/{run_id}/reconciliation")
    async def reconcile(workspace_id: str, run_id: str, body: WebReconciliation):
        service = await instance.application(workspace_id)
        session_id = await service.store.run_session(run_id)
        return jsonable_encoder(
            await service.reconcile(
                session_id,
                run_id,
                body.generation,
                body.evidence,
                [effect.model_dump() for effect in body.effects],
            )
        )

    @router.get(WORKSPACE + "/runs/{run_id}/effects/{effect_id}/status")
    async def effect_status(workspace_id: str, run_id: str, effect_id: str):
        service = await instance.application(workspace_id)
        tool = await service.receipts.dispatched_tool(run_id, effect_id)
        if instance.connectors is None or tool not in instance.connectors.definitions:
            raise DomainError("connector_reconcile", "This effect requires manual reconciliation")
        return await instance.connectors.reconcile(tool, effect_id)

    @router.post(WORKSPACE + "/sessions/{session_id}/images", status_code=201)
    async def upload_image(
        request: Request,
        workspace_id: str,
        session_id: str,
        x_filename: Annotated[str, Header(max_length=1000)] = "image",
    ):
        service = await instance.application(workspace_id)
        return await service.publish_attachment(
            session_id, request.stream(), unquote(x_filename), image=True
        )

    @router.get(WORKSPACE + "/sessions/{session_id}/images/{image_id}")
    async def read_image(
        workspace_id: str, session_id: str, image_id: str, thumbnail: bool = False
    ):
        service = await instance.application(workspace_id)
        row, path = await service.get_image(session_id, image_id)
        if thumbnail:
            return Response(await service.artifacts.thumbnail(row, path), media_type="image/png")
        return FileResponse(path, media_type=row["kind"])

    @router.post(WORKSPACE + "/sessions/{session_id}/attachments", status_code=201)
    async def upload(
        request: Request,
        workspace_id: str,
        session_id: str,
        x_filename: Annotated[str, Header(max_length=1000)] = "attachment",
    ):
        service = await instance.application(workspace_id)
        await service.store.get_session(session_id, "operator")
        return await service.publish_attachment(session_id, request.stream(), unquote(x_filename))

    @router.get(WORKSPACE + "/runs/{run_id}/artifacts")
    async def artifacts(workspace_id: str, run_id: str):
        service = await instance.application(workspace_id)
        await service.store.run_session(run_id)
        return await service.artifacts.list(run_id)

    @router.get(WORKSPACE + "/artifacts/{artifact_id}")
    async def download(workspace_id: str, artifact_id: str):
        service = await instance.application(workspace_id)
        row, path = await service.artifacts.get(artifact_id)
        return FileResponse(path, filename=row["name"], media_type="application/octet-stream")

    @router.post(WORKSPACE + "/workflows/{workflow_id}/runs", status_code=202)
    async def start_workflow(workspace_id: str, workflow_id: str, body: WebWorkflow):
        return await instance.start_workflow(workspace_id, workflow_id, body)

    @router.get(WORKSPACE + "/jobs/defaults")
    async def job_defaults(workspace_id: str):
        await instance.application(workspace_id)
        return {
            "wall_seconds": JobBudget().wall_seconds,
            "wall_seconds_max": number("job_budget.wall_seconds_max"),
            "stages_max": number("jobs.stages_max_length"),
            "parallelism_max": number("jobs.parallelism_max"),
        }

    @router.post(WORKSPACE + "/jobs")
    async def create_job(workspace_id: str, body: CreateJob):
        service = await instance.application(workspace_id)
        if any(stage.step or (not local and (not stage.read_only or stage.verify))
               for stage in body.stages):
            raise DomainError("operation_denied", "Use the configured workflow for these stages")
        return await service.create_job(body)

    @router.get(WORKSPACE + "/jobs/{job_id}")
    async def job(workspace_id: str, job_id: str):
        service = await instance.application(workspace_id)
        return await service.jobs.detail(job_id)

    @router.get(WORKSPACE + "/jobs")
    async def jobs(workspace_id: str):
        service = await instance.application(workspace_id)
        return await service.jobs.ledger.list()

    @router.post(WORKSPACE + "/jobs/{job_id}/commands")
    async def command_job(workspace_id: str, job_id: str, body: JobCommand):
        service = await instance.application(workspace_id)
        return await service.command_job(
            job_id, body.action, position=body.position, evidence=body.evidence
        )

    return router
