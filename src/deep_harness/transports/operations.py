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


def operations_router(host, authenticate):
    router = APIRouter(dependencies=[Depends(authenticate)])

    @router.get("/profiles/{profile_id}/sessions/{session_id}/delivery")
    async def preview_delivery(profile_id: str, session_id: str):
        app = await host.application(profile_id)
        return await app.deliver(session_id)

    @router.get("/profiles/{profile_id}/sessions/{session_id}/task-control")
    async def task_state(profile_id: str, session_id: str):
        app = await host.application(profile_id)
        return await app.task_control(session_id)

    @router.post("/profiles/{profile_id}/sessions/{session_id}/task-control")
    async def task_command(profile_id: str, session_id: str, body: TaskControl):
        app = await host.application(profile_id)
        return await app.task_control(session_id, body.action)

    @router.post("/profiles/{profile_id}/sessions/{session_id}/delivery")
    async def deliver(profile_id: str, session_id: str, body: DeliveryCommand):
        app = await host.application(profile_id)
        return await app.deliver(session_id, body.action, body.revision, body.change_set_id)

    @router.post("/profiles/{profile_id}/bundles")
    async def import_bundle(profile_id: str, body: BundleInput):
        app = await host.application(profile_id)
        return await app.imported_bundle(raw=body.package.encode("utf-8"))

    @router.get("/profiles/{profile_id}/bundles/{identifier}")
    async def inspect_bundle(profile_id: str, identifier: str):
        app = await host.application(profile_id)
        return await app.imported_bundle(identifier=identifier)

    @router.post("/profiles/{profile_id}/bundles/{identifier}")
    async def apply_bundle(profile_id: str, identifier: str, body: BundleAction):
        app = await host.application(profile_id)
        return await app.imported_bundle(identifier=identifier, action=body.action)

    @router.get("/profiles/{profile_id}/sessions/{session_id}/processes")
    async def processes(profile_id: str, session_id: str):
        app = await host.application(profile_id)
        return await app.development_processes(session_id)

    @router.post("/profiles/{profile_id}/sessions/{session_id}/processes")
    async def start_process(profile_id: str, session_id: str, body: DevelopmentCommand):
        app = await host.application(profile_id)
        return await app.development_processes(session_id, start=body.model_dump())

    @router.post("/profiles/{profile_id}/sessions/{session_id}/processes/{process_id}/stop")
    async def stop_process(
        profile_id: str, session_id: str,
        process_id: Annotated[str, ApiPath(pattern=r"^[a-f0-9]{64}$")],
    ):
        app = await host.application(profile_id)
        return await app.development_processes(session_id, stop=process_id)

    @router.post("/profiles/{profile_id}/sessions/{session_id}/admissions/{admission_id}/cancel")
    async def cancel(profile_id: str, session_id: str, admission_id: str):
        app = await host.application(profile_id)
        return jsonable_encoder(await app.cancel(session_id, admission_id))

    @router.post(
        "/profiles/{profile_id}/sessions/{session_id}/admissions/{admission_id}/resolution"
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

    @router.post("/profiles/{profile_id}/backup")
    async def backup(profile_id: str, body: ArchivePath):
        app = await host.application(profile_id)
        return await app.backup(Path(body.path))

    @router.post("/profiles/{profile_id}/restore")
    async def restore(profile_id: str, body: ArchivePath):
        return await host.restore(profile_id, Path(body.path))

    @router.get("/profiles/{profile_id}/skills")
    async def list_skills(profile_id: str):
        app = await host.application(profile_id)
        return {"skills": await app.knowledge.list_skills()}

    @router.post("/profiles/{profile_id}/skills")
    async def install_skill(profile_id: str, body: SkillDocument):
        app = await host.application(profile_id)
        return await app.install_skill(body.document)

    @router.delete("/profiles/{profile_id}/skills/{name}")
    async def disable_skill(profile_id: str, name: str):
        app = await host.application(profile_id)
        await app.disable_skill(name)
        return {"disabled": name}

    @router.get("/profiles/{profile_id}/memories")
    async def list_memories(profile_id: str):
        app = await host.application(profile_id)
        return {"memories": await app.knowledge.list_memories()}

    @router.post("/profiles/{profile_id}/memories")
    async def add_memory(profile_id: str, body: MemoryInput):
        app = await host.application(profile_id)
        return await app.add_memory(body.content)

    @router.get("/profiles/{profile_id}/memories/search")
    async def search_memories(profile_id: str, q: str):
        app = await host.application(profile_id)
        return {"memories": await app.knowledge.search(q)}

    @router.delete("/profiles/{profile_id}/memories/{memory_id}")
    async def forget_memory(profile_id: str, memory_id: str):
        app = await host.application(profile_id)
        await app.forget_memory(memory_id)
        return {"forgotten": memory_id}

    @router.post("/profiles/{profile_id}/sessions/{session_id}/curation")
    async def curate(profile_id: str, session_id: str):
        app = await host.application(profile_id)
        return await app.curate(session_id)

    @router.get("/profiles/{profile_id}/memory-candidates")
    async def list_candidates(profile_id: str):
        app = await host.application(profile_id)
        return {"candidates": await app.curator.ledger.list_candidates()}

    @router.post("/profiles/{profile_id}/memory-candidates/{candidate_id}/decision")
    async def decide_candidate(profile_id: str, candidate_id: str, body: CandidateDecision):
        app = await host.application(profile_id)
        return await app.decide_memory_candidate(candidate_id, body.decision)

    return router
