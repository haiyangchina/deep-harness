"""Long-task coordination over real admissions and deterministic models."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import insert, update

from deep_harness.application.config import ExecutionSettings
from deep_harness.application.jobs import CreateJob
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s
from deep_harness.runtime.models import DiagnosticModel
from deep_harness.transports.api import create_api


class StageModel(DiagnosticModel):
    def __init__(self):
        self.tasks = []
        self.started = asyncio.Event()
        self.release = None

    async def complete(self, prompt, messages, tools):
        last = messages[-1]
        if last["role"] == "user":
            self.tasks.append(last["content"])
            self.started.set()
            if self.release is not None:
                await self.release.wait()
            if "Stage: 写入" in last["content"]:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "write",
                            "id": "file",
                            "args": {"path": "one.txt", "content": "once"},
                        }
                    ],
                }
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "status": "complete",
                    "summary": "stage evidence",
                    "artifacts": ["file:one.txt"],
                    "next_step": None,
                }
            ),
        }


def plan(*names, **kwargs):
    return CreateJob(
        request_id="request",
        goal="Test the staged workflow",
        stages=[
            {"title": name, "goal": "Run this stage", "acceptance": "Record evidence"}
            for name in names
        ],
        **kwargs,
    )


async def test_long_task_runs_beyond_one_model_call_and_passes_evidence(tmp_path):
    model = StageModel()
    host = ProjectHost(
        tmp_path / "state",
        tmp_path,
        lambda _: model,
        execution_limits=ExecutionSettings(max_model_calls=1),
    )
    try:
        app = await host.application("jobs")
        definition = plan("调研", "实现", "验证")
        job = await app.create_job(definition)
        assert (await app.create_job(definition))["id"] == job["id"]
        await app.wait_idle()
        job = await app.jobs.detail(job["id"])
        assert job["status"] == "completed"
        assert job["used"]["max_model_calls"] == 3
        assert len(set(stage["admission_id"] for stage in job["stages"])) == 3
        assert "file:one.txt" in model.tasks[1]
        for stage in job["stages"]:
            assert stage["verified"]
            assert stage["limits"]["max_model_calls"] == 1
        with pytest.raises(DomainError, match="job page"):
            await app.submit(job["stages"][0]["session_id"], "outside", "new input")
    finally:
        await host.close()


async def test_pause_and_restart_preserves_completed_stage_without_replay(tmp_path):
    model = StageModel()
    model.release = asyncio.Event()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda _: model)
    try:
        app = await host.application("jobs")
        job = await app.create_job(plan("first", "second"))
        await asyncio.wait_for(model.started.wait(), 3)
        await app.command_job(job["id"], "pause")
        model.release.set()
        await app.wait_idle()
        paused = await app.jobs.detail(job["id"])
        assert paused["status"] == "paused"
        assert paused["stages"][0]["verified"]
        assert paused["stages"][1]["session_id"] is None
    finally:
        await host.close()

    restarted = ProjectHost(tmp_path / "state", tmp_path, lambda _: model)
    try:
        app = await restarted.application("jobs")
        await app.command_job(job["id"], "resume")
        await app.wait_idle()
        completed = await app.jobs.detail(job["id"])
        assert completed["status"] == "completed"
        assert len(model.tasks) == 2
    finally:
        await restarted.close()


async def test_approval_does_not_start_paused_stage_until_resume(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, lambda _: StageModel())
    try:
        app = await host.application("jobs")
        job = await app.create_job(plan("写入", "验证"))
        await app.wait_idle()
        view = await app.jobs.detail(job["id"])
        session_id = view["stages"][0]["session_id"]
        status = await app.store.session_status(session_id, "operator")
        approval = status["approvals"][0]
        await app.command_job(job["id"], "pause")
        await app.approve(session_id, approval["id"], approval["generation"], "approve")
        await app.wait_idle()
        assert not (tmp_path / "one.txt").exists()
        await app.command_job(job["id"], "resume")
        await app.wait_idle()
        assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "once"
        assert (await app.jobs.detail(job["id"]))["status"] == "completed"
    finally:
        await host.close()


async def test_dispatched_unknown_never_replays_on_reopen(tmp_path):
    model = StageModel()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda _: model)
    try:
        app = await host.application("jobs")
        job = await app.create_job(plan("写入", "验证"))
        await app.wait_idle()
        stage = (await app.jobs.detail(job["id"]))["stages"][0]
        async with app.store.transaction() as conn:
            await conn.execute(
                update(s.admissions)
                .where(s.admissions.c.id == stage["admission_id"])
                .values(status="started")
            )
            await conn.execute(
                insert(s.effects).values(
                    id="dispatched-but-unconfirmed",
                    admission_id=stage["admission_id"],
                    proposal_hash="test-proposal",
                    status="dispatched",
                )
            )
    finally:
        await host.close()
    reopened = ProjectHost(tmp_path / "state", tmp_path, lambda _: model)
    try:
        app = await reopened.application("jobs")
        with pytest.raises(DomainError, match="Review"):
            await app.command_job(job["id"], "resume")
        with pytest.raises(DomainError):
            await app.command_job(job["id"], "accept", position=0, evidence="unverified")
        assert len(model.tasks) == 1
        assert (await app.jobs.detail(job["id"]))["stages"][0]["admission_status"] == "unknown"
        await app.wait_idle()
        admission = await app.store.get_admission(stage["admission_id"], stage["session_id"])
        await app.reconcile(
            stage["session_id"],
            admission.id,
            admission.generation,
            "Operator checked the workspace",
            [
                {
                    "effect_id": "dispatched-but-unconfirmed",
                    "proposal_hash": "test-proposal",
                    "outcome": "applied",
                }
            ],
        )
        await app.command_job(job["id"], "accept", position=0, evidence="File checked")
        await app.command_job(job["id"], "resume")
        await app.wait_idle()
        final = await app.jobs.detail(job["id"])
        assert final["status"] == "completed"
        assert final["stages"][0]["admission_id"] == stage["admission_id"]
        assert len(model.tasks) == 2
        assert final["stages"][0]["evidence"]["effects"][0]["status"] == "confirmed_applied"

    finally:
        await reopened.close()


async def test_authenticated_job_routes_and_manual_review(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    token = "token-expression-for-local-tests-only"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(host, token)), base_url="http://test"
        ) as client:
            root = "/profiles/jobs/jobs"
            assert (await client.get(root)).status_code == 401
            client.headers["Authorization"] = f"Bearer {token}"
            response = await client.post(root, json=plan("first").model_dump())
            assert response.status_code == 200
            detail_url = f"{root}/{response.json()['id']}"
            app = await host.application("jobs")
            await app.wait_idle()
            assert (await client.get(detail_url)).json()["status"] == "blocked"
            accept = {"action": "accept", "position": 0, "evidence": "Manually validated"}
            assert (await client.post(f"{detail_url}/commands", json=accept)).status_code == 200
            await client.post(f"{detail_url}/commands", json={"action": "resume"})
            await app.wait_idle()
            assert (await client.get(detail_url)).json()["status"] == "completed"
    finally:
        await host.close()


def test_parallel_group_rejects_stages_that_can_write():
    stages = [{"title": title, "goal": "g", "acceptance": "a", "group": 0} for title in ("a", "b")]
    with pytest.raises(ValidationError, match="read-only"):
        CreateJob(request_id="id", goal="g", stages=stages, parallelism=2)


async def test_active_stage_pauses_before_pending_tool_and_resumes_from_checkpoint(tmp_path):
    model = StageModel()
    model.release = asyncio.Event()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda _: model)
    try:
        app = await host.application("jobs")
        job = await app.create_job(plan("写入"))
        await asyncio.wait_for(model.started.wait(), 3)
        await app.command_job(job["id"], "pause")
        model.release.set()
        await app.wait_idle()
        paused = (await app.jobs.detail(job["id"]))["stages"][0]
        assert paused["admission_status"] == "job_paused"
        assert not (tmp_path / "one.txt").exists()
        await app.command_job(job["id"], "resume")
        await app.wait_idle()
        status = await app.store.session_status(paused["session_id"], "operator")
        approval = status["approvals"][0]
        await app.approve(paused["session_id"], approval["id"], approval["generation"], "approve")
        await app.wait_idle()
        done = await app.jobs.detail(job["id"])
        assert done["status"] == "completed"
        assert done["stages"][0]["admission_id"] == paused["admission_id"]
        assert len(model.tasks) == 1
        assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "once"
    finally:
        await host.close()


async def test_parallel_read_only_group_is_bounded_and_serializes_model_access(tmp_path):
    model = StageModel()
    model.release = asyncio.Event()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda _: model)
    try:
        app = await host.application("jobs")
        stages = [
            {"title": title, "goal": "Read", "acceptance": "Summary", "read_only": True, "group": 0}
            for title in ("first", "second", "third")
        ]
        job = await app.create_job(
            CreateJob(request_id="request", goal="Parallel", stages=stages, parallelism=2)
        )
        await asyncio.wait_for(model.started.wait(), 3)
        for _ in range(30):
            detail = await app.jobs.detail(job["id"])
            if detail["stages"][1]["admission_id"]:
                break
            await asyncio.sleep(0.02)
        assert detail["stages"][1]["admission_id"]
        assert detail["stages"][2]["session_id"] is None
        assert len(model.tasks) == 1
        model.release.set()
        await app.wait_idle()
        assert (await app.jobs.detail(job["id"]))["status"] == "completed"
    finally:
        model.release.set()
        await host.close()
