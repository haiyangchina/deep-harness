"""Completed transcript pages keep a stable turn order and cursor."""

import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError


async def test_completed_history_pages_do_not_repeat_or_skip_messages(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Prompt")
        for number in range(3):
            await app.submit(session.id, f"send-{number}", f"message-{number}")
            await app.wait_idle()
        cursor = ""
        pages = []
        while True:
            page = await app.store.history_page(session.id, after=cursor, limit=2)
            pages.extend(page["messages"])
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
        assert pages == await app.store.history(session.id)
        assert len({item["id"] for item in pages}) == 6
        matches = await app.store.search_history(session.id, "message-1")
        assert len(matches) == 2
        assert matches[0]["content"] == "message-1"
        assert (await app.store.search_history(session.id, "missing")) == []
        chinese = await app.submit(session.id, "chinese", "请验证流式搜索成功")
        await app.wait_idle()
        assert (await app.store.get_admission(chinese.id, session.id)).status == "completed"
        assert len(await app.store.search_history(session.id, "流式")) == 2
        with pytest.raises(DomainError, match="cursor"):
            await app.store.history_page(session.id, after="9999999999999999999:1")
    finally:
        await host.close()


async def test_live_history_reads_tail_older_pages_and_only_new_commits(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, recover_queued=False)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Prompt")
        for number in range(4):
            await app.store.admit(
                session, principal="operator", submission_id=str(number), text=str(number)
            )
        tail = await app.store.live_history(session.id, limit=2)
        assert tail["has_more"]
        assert [row["message"]["content"] for row in tail["items"]] == ["2", "3"]
        before, after = (row["seq"] for row in tail["items"])
        older = await app.store.live_history(session.id, before=before, limit=2)
        assert [row["message"]["content"] for row in older["items"]] == ["0", "1"]
        assert not older["has_more"]
        assert (await app.store.live_history(session.id, after=after))["items"] == []
        await app.store.admit(session, principal="operator", submission_id="4", text="4")
        delta = await app.store.live_history(session.id, after=after)
        assert [row["message"]["content"] for row in delta["items"]] == ["4"]
    finally:
        await host.close()
