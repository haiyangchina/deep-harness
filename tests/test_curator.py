"""Curator proposals require review and survive retries as one run."""

import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError


class CuratingModel:
    identity = {"provider": "curator-test", "version": 1}
    request_timeout = 10

    def __init__(self):
        self.curation_calls = 0

    async def complete(self, prompt, messages, tools):
        if "Extract at most five stable facts" in prompt:
            self.curation_calls += 1
            return {"content": '["用户偏好中文回答", "喜欢简短总结"]', "usage": {}}
        return {"role": "assistant", "content": "Understood"}


async def test_curator_review_is_atomic_and_new_sessions_only(tmp_path):
    model = CuratingModel()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Base")
        admitted = await app.submit(session.id, "send", "请记住我偏好中文回答")
        await app.wait_idle()
        assert (await app.store.get_admission(admitted.id, session.id)).status == "completed"
        proposal = await app.curate(session.id)
        assert len(proposal["candidates"]) == 2
        assert model.curation_calls == 1
        assert (await app.curate(session.id))["run"]["id"] == proposal["run"]["id"]
        assert model.curation_calls == 1
        assert await app.knowledge.list_memories() == []
        first, second = proposal["candidates"]
        await app.decide_memory_candidate(first["id"], "accept")
        await app.decide_memory_candidate(second["id"], "reject")
        with pytest.raises(DomainError, match="already reviewed"):
            await app.decide_memory_candidate(first["id"], "accept")
        assert len(await app.knowledge.list_memories()) == 1
        assert "偏好中文回答" not in session.prompt
        next_session = await app.create_session("next", "Base")
        assert "偏好中文回答" in next_session.prompt
        assert "喜欢简短总结" not in next_session.prompt
        other = await host.application("two")
        with pytest.raises(DomainError, match="not accessible"):
            await other.decide_memory_candidate(first["id"], "accept")
    finally:
        await host.close()


async def test_configured_auto_curator_generates_reviewable_candidates(tmp_path):
    home = tmp_path / "state" / "profiles" / "one"
    home.mkdir(parents=True)
    (home / "curation.toml").write_text("auto = true\n", encoding="utf-8")
    model = CuratingModel()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Base")
        admission = await app.submit(session.id, "send", "请记住我偏好中文回答")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
        assert model.curation_calls == 1
        candidates = await app.curator.ledger.list_candidates()
        assert len(candidates) == 2
        assert await app.knowledge.list_memories() == []
        await app.curate(session.id)
        assert model.curation_calls == 1
    finally:
        await host.close()
