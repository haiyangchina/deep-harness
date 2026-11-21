"""Authenticated lifecycle, reconciliation and backup operations."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

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


class SkillDocument(Request):
    document: str = Field(min_length=1, max_length=number("skills.document_max_length"))


class MemoryInput(Request):
    content: str = Field(min_length=1, max_length=number("memory.content_max_length"))


class CandidateDecision(Request):
    decision: Literal["accept", "reject"]


def operations_router(host, authenticate):
    router = APIRouter(dependencies=[Depends(authenticate)])

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
