"""Fork recovery inherits complete turns and cannot replay uncertain external writes."""

import asyncio

from deep_harness.bootstrap import ProjectHost
from deep_harness.runtime.models import DiagnosticModel


class RecordingModel(DiagnosticModel):
    def __init__(self):
        self.requests = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, prompt, messages, tools):
        self.requests.append([row.copy() for row in messages])
        if messages[-1]["content"] == "branch-two":
            self.entered.set()
            await self.release.wait()
        return await super().complete(prompt, messages, tools)


async def test_fork_preserves_completed_prefix_and_leaves_unknown_effect_untouched(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    app = await host.application("default")
    source = await app.create_session("source", "Frozen", ("read_file", "write_file"))
    await app.submit(source.id, "complete", "remember the completed turn")
    await app.wait_idle()
    uncertain = await app.store.admit(
        source, principal="operator", submission_id="uncertain", text="uncertain write"
    )
    claim = await app.executions.claim(source.id, app.owner)
    await app.receipts.prepare_effect(claim, "external-effect", "exact-arguments")
    await app.receipts.dispatch_effect(claim, "external-effect")
    marker = tmp_path / "external.txt"
    marker.write_text("already written once", encoding="utf-8")
    await app.executions.recover()
    follower = await app.store.admit(
        source, principal="operator", submission_id="follower", text="excluded follower"
    )
    branch = await app.fork_session(source.id, "fork-once")
    assert (await app.fork_session(source.id, "fork-once")).id == branch.id
    assert [row["role"] for row in await app.store.history(branch.id)] == ["user", "assistant"]
    assert len((await app.sessions.status(branch.id))["lineage"]["copied_admissions"]) == 1
    await host.close()
    recording = RecordingModel()
    restarted = ProjectHost(tmp_path / "state", tmp_path, lambda scope: recording)
    try:
        app = await restarted.application("default")
        await app.submit(branch.id, "continue", "continue the branch")
        await app.wait_idle()
        assert [row["role"] for row in recording.requests[0]] == ["user", "assistant", "user"]
        assert recording.requests[0][0]["content"] == "remember the completed turn"
        assert all(
            row["content"] not in {"uncertain write", "excluded follower"}
            for row in recording.requests[0]
        )
        assert marker.read_text(encoding="utf-8") == "already written once"
        assert (await app.store.get_admission(uncertain.id, source.id)).status == "unknown"
        assert (await app.store.get_admission(follower.id, source.id)).status == "queued"
        effects = (await app.sessions.status(source.id))["active_effects"]
        assert effects[0]["status"] == "dispatched"
        assert effects[0]["proposal_hash"] == "exact-arguments"
    finally:
        await restarted.close()


async def test_branch_seed_does_not_inject_future_inputs_or_duplicate_history(tmp_path):
    model = RecordingModel()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model)
    try:
        app = await host.application("default")
        source = await app.create_session("source", "Frozen")
        await app.submit(source.id, "one", "original")
        await app.wait_idle()
        branch = await app.fork_session(source.id, "branch")
        await app.submit(branch.id, "two", "branch-two")
        async with asyncio.timeout(5):
            await model.entered.wait()
        await app.submit(branch.id, "three", "future input")
        model.release.set()
        await app.wait_idle()
        assert len(model.requests[1]) == 3
        assert model.requests[1][-1]["content"] == "branch-two"
        assert len(model.requests[2]) == 5
        assert model.requests[2][-1]["content"] == "future input"
        history = await app.store.history(branch.id)
        assert len(history) == 6
        assert [row["role"] for row in history] == ["user", "assistant"] * 3
        assert len({row["id"] for row in history}) == 6
    finally:
        model.release.set()
        await host.close()


async def test_fork_inherits_completed_tool_messages_without_executing_them(tmp_path):
    (tmp_path / "input.txt").write_text("original contents", encoding="utf-8")
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("default")
        source = await app.create_session("source", "Frozen")
        await app.submit(source.id, "one", "/read input.txt")
        await app.wait_idle()
        branch = await app.fork_session(source.id, "branch")
        (tmp_path / "input.txt").unlink()
        await app.submit(branch.id, "two", "continue without another tool")
        await app.wait_idle()
        history = await app.store.history(branch.id)
        assert [row["role"] for row in history] == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "user",
            "assistant",
        ]
        assert history[1]["tool_calls"][0]["id"] == history[2]["tool_call_id"]
        assert "original contents" in history[2]["content"]
        final = (await app.sessions.status(branch.id))["latest_admission"]
        assert final.status == "completed"
    finally:
        await host.close()
