"""Authenticated lifecycle, reconciliation and backup operations."""

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi import Path as ApiPath
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from deep_harness.infrastructure.change_bundles import MAX_BUNDLE_BYTES
from deep_harness.server_config import number


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EffectDecision(Request):
    effect_id: str = Field(min_length=1)
    proposal_hash: str = Field(min_length=1)
    outcome: Literal["applied", "not_applied"]


class Resolution(Request):
    generation: int = Field(ge=0)
    evidence: str = Field(min_length=1, max_length=number("reconciliation.evidence_max_length"))
    effects: list[EffectDecision] = Field(
        default_factory=list, max_length=number("reconciliation.effects_max_length")
    )


class ArchivePath(Request):
    path: str = Field(min_length=1, max_length=number("archives.path_max_length"))


class DeliveryCommand(Request):
    action: Literal["apply", "undo", "discard", "reconcile"]
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    change_set_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class BundleInput(Request):
    package: str = Field(min_length=1, max_length=MAX_BUNDLE_BYTES)


class BundleAction(Request):
    action: Literal["apply", "undo", "reconcile"]


class TaskControl(Request):
    action: Literal["pause", "resume"]


class DevelopmentCommand(Request):
    request_id: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1, max_length=number("shell.command_max_length"))
    cwd: str = Field(default=".", min_length=1, max_length=4096)
    lifetime: float = Field(default=3600, ge=1, le=86400)


class SkillDocument(Request):
    document: str = Field(min_length=1, max_length=number("skills.document_max_length"))


class MemoryInput(Request):
    content: str = Field(min_length=1, max_length=number("memory.content_max_length"))


class CandidateDecision(Request):
    decision: Literal["accept", "reject"]


def operations_router(host, authenticate=None, *, versioned=False, knowledge_only=False):
    router = APIRouter(dependencies=[Depends(authenticate)] if authenticate else [])
    root = "/api/v1/workspaces/{profile_id}" if versioned else "/profiles/{profile_id}"
    lifecycle = APIRouter() if knowledge_only else router
    legacy = APIRouter() if versioned else router

    @lifecycle.get(root + "/sessions/{session_id}/delivery")
    async def preview_delivery(profile_id: str, session_id: str):
        app = await host.application(profile_id)
        return await app.deliver(session_id)

    @legacy.get(root + "/sessions/{session_id}/task-control")
    async def task_state(profile_id: str, session_id: str):
        app = await host.application(profile_id)
        return await app.task_control(session_id)

    @legacy.post(root + "/sessions/{session_id}/task-control")
    async def task_command(profile_id: str, session_id: str, body: TaskControl):
        app = await host.application(profile_id)
        return await app.task_control(session_id, body.action)

    @lifecycle.post(root + "/sessions/{session_id}/delivery")
    async def deliver(profile_id: str, session_id: str, body: DeliveryCommand):
        app = await host.application(profile_id)
        return await app.deliver(session_id, body.action, body.revision, body.change_set_id)

    @lifecycle.post(root + "/bundles")
    async def import_bundle(profile_id: str, body: BundleInput):
        app = await host.application(profile_id)
        return await app.imported_bundle(raw=body.package.encode("utf-8"))

    @lifecycle.get(root + "/bundles/{identifier}")
    async def inspect_bundle(profile_id: str, identifier: str):
        app = await host.application(profile_id)
        return await app.imported_bundle(identifier=identifier)

    @lifecycle.post(root + "/bundles/{identifier}")
    async def apply_bundle(profile_id: str, identifier: str, body: BundleAction):
        app = await host.application(profile_id)
        return await app.imported_bundle(identifier=identifier, action=body.action)

    @legacy.get(root + "/sessions/{session_id}/processes")
    async def processes(profile_id: str, session_id: str):
        app = await host.application(profile_id)
        return await app.development_processes(session_id)

    @legacy.post(root + "/sessions/{session_id}/processes")
    async def start_process(profile_id: str, session_id: str, body: DevelopmentCommand):
        app = await host.application(profile_id)
        return await app.development_processes(session_id, start=body.model_dump())

    @legacy.post(root + "/sessions/{session_id}/processes/{process_id}/stop")
    async def stop_process(
        profile_id: str, session_id: str,
        process_id: Annotated[str, ApiPath(pattern=r"^[a-f0-9]{64}$")],
    ):
        app = await host.application(profile_id)
        return await app.development_processes(session_id, stop=process_id)

    @legacy.post(root + "/sessions/{session_id}/admissions/{admission_id}/cancel")
    async def cancel(profile_id: str, session_id: str, admission_id: str):
        app = await host.application(profile_id)
        return jsonable_encoder(await app.cancel(session_id, admission_id))

    @legacy.post(root + "/sessions/{session_id}/admissions/{admission_id}/retry-workspace")
    async def retry_workspace(profile_id: str, session_id: str, admission_id: str):
        app = await host.application(profile_id)
        return jsonable_encoder(await app.retry_workspace(session_id, admission_id))

    @legacy.post(
        root + "/sessions/{session_id}/admissions/{admission_id}/retry-summary"
    )
    async def retry_summary(profile_id: str, session_id: str, admission_id: str):
        app = await host.application(profile_id)
        return jsonable_encoder(await app.retry_summary(session_id, admission_id))

    @legacy.post(
        root + "/sessions/{session_id}/admissions/{admission_id}/resolution"
    )
    async def resolve(profile_id: str, session_id: str, admission_id: str, body: Resolution):
        app = await host.application(profile_id)
        return jsonable_encoder(
            await app.reconcile(
                session_id,
                admission_id,
                body.generation,
                body.evidence,
                [effect.model_dump() for effect in body.effects],
            )
        )

    @legacy.post(root + "/backup")
    async def backup(profile_id: str, body: ArchivePath):
        app = await host.application(profile_id)
        return await app.backup(Path(body.path))

    @legacy.post(root + "/restore")
    async def restore(profile_id: str, body: ArchivePath):
        return await host.restore(profile_id, Path(body.path))

    @router.get(root + "/skills")
    async def list_skills(profile_id: str):
        app = await host.application(profile_id)
        return {"skills": await app.knowledge.list_skills()}

    @router.post(root + "/skills")
    async def install_skill(profile_id: str, body: SkillDocument):
        app = await host.application(profile_id)
        return await app.install_skill(body.document)

    @router.delete(root + "/skills/{name}")
    async def disable_skill(profile_id: str, name: str):
        app = await host.application(profile_id)
        await app.disable_skill(name)
        return {"disabled": name}

    @router.get(root + "/memories")
    async def list_memories(profile_id: str):
        app = await host.application(profile_id)
        return {"memories": await app.knowledge.list_memories()}

    @router.post(root + "/memories")
    async def add_memory(profile_id: str, body: MemoryInput):
        app = await host.application(profile_id)
        return await app.add_memory(body.content)

    @router.get(root + "/memories/search")
    async def search_memories(profile_id: str, q: str):
        app = await host.application(profile_id)
        return {"memories": await app.knowledge.search(q)}

    @router.delete(root + "/memories/{memory_id}")
    async def forget_memory(profile_id: str, memory_id: str):
        app = await host.application(profile_id)
        await app.forget_memory(memory_id)
        return {"forgotten": memory_id}

    @router.post(root + "/sessions/{session_id}/curation")
    async def curate(profile_id: str, session_id: str):
        app = await host.application(profile_id)
        return await app.curate(session_id)

    @router.get(root + "/memory-candidates")
    async def list_candidates(profile_id: str):
        app = await host.application(profile_id)
        return {"candidates": await app.curator.ledger.list_candidates()}

    @router.post(root + "/memory-candidates/{candidate_id}/decision")
    async def decide_candidate(profile_id: str, candidate_id: str, body: CandidateDecision):
        app = await host.application(profile_id)
        return await app.decide_memory_candidate(candidate_id, body.decision)

    return router
