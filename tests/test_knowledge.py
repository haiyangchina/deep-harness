"""Profile knowledge is versioned and frozen per conversation."""

from deep_harness.bootstrap import ProjectHost


def skill(body):
    return f"---\nname: concise\ndescription: Answer briefly\n---\n{body}\n"


async def test_skill_and_memory_changes_only_affect_new_sessions(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("one")
        await app.knowledge.add_skill(skill("Use one sentence."))
        memory = await app.knowledge.remember("喜欢中文回答", "operator")
        first = await app.create_session("first", "Base prompt")
        assert "Use one sentence." in first.prompt
        assert "喜欢中文回答" in first.prompt
        await app.knowledge.add_skill(skill("Use three sentences."))
        await app.knowledge.forget(memory["id"])
        await app.sessions.validate(first)
        second = await app.create_session("second", "Base prompt")
        assert "Use three sentences." in second.prompt
        assert "喜欢中文回答" not in second.prompt
        assert len(await app.knowledge.search("中文")) == 0
        other = await host.application("two")
        assert not await other.knowledge.list_skills()
    finally:
        await host.close()


class RememberingModel:
    identity = {"provider": "memory-test", "version": 1}
    request_timeout = 10

    async def complete(self, prompt, messages, tools):
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": "Saved"}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "remember-1",
                    "name": "save_memory",
                    "args": {"content": "用户偏好简体中文"},
                }
            ],
        }


async def test_model_memory_save_requires_approval_and_affects_next_session(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: RememberingModel())
    try:
        app = await host.application("one")
        first = await app.create_session("first", "Prompt", ("save_memory",))
        admission = await app.submit(first.id, "send", "remember")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, first.id)
        assert pending.status == "waiting_approval"
        assert await app.knowledge.list_memories() == []
        approval = (await app.sessions.status(first.id))["approvals"][0]
        await app.approve(first.id, approval["id"], pending.generation, "approve")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, first.id)).status == "completed"
        assert len(await app.knowledge.search("简体")) == 1
        assert "简体中文" not in first.prompt
        second = await app.create_session("second", "Prompt")
        assert "简体中文" in second.prompt
    finally:
        await host.close()
