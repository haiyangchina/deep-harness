"""Shared session metadata, deletion fences and restart recovery."""

import asyncio
import json

import httpx
import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.identity import fingerprint
from deep_harness.infrastructure.task_workspaces import git
from deep_harness.transports.api import create_api

TOKEN = "session-management-test-token-with-32-characters"
BASE = "/api/v1/workspaces/one"


def client_for(host):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_api(host, TOKEN, workspace_id="one")),
        base_url="http://local", headers={"Authorization": f"Bearer {TOKEN}"},
    )


async def create(client, key):
    response = await client.post(f"{BASE}/sessions", json={"request_id": key})
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def test_session_list_pins_paginate_and_rename_survives_restart(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        async with client_for(host) as client:
            first, second, third = [await create(client, name) for name in ("a", "b", "c")]
            initial = (await client.get(f"{BASE}/sessions", params={"limit": 1})).json()
            route = f"{BASE}/sessions/{third}"
            saved = await client.patch(route, json={
                "expected_revision": 0, "title": "  研究记录  ", "pinned": True,
            })
            assert saved.status_code == 200, saved.text
            assert saved.json()["title"] == "研究记录"
            assert saved.json()["metadata_revision"] == 1
            assert saved.json()["pinned_at"]
            stale = await client.patch(route, json={"expected_revision": 0, "title": "冲突"})
            assert stale.json()["code"] == "session_metadata_conflict"
            blank = await client.patch(route, json={"expected_revision": 1, "title": "  "})
            assert blank.json()["code"] == "session_metadata_invalid"
            old_cursor = await client.get(
                f"{BASE}/sessions", params={"after": initial["next_cursor"]}
            )
            assert old_cursor.json()["code"] == "session_list_changed"
            page = (await client.get(f"{BASE}/sessions", params={"limit": 1})).json()
            assert page["sessions"][0]["id"] == third
            remainder = (await client.get(
                f"{BASE}/sessions", params={"after": page["next_cursor"]}
            )).json()
            assert [item["id"] for item in remainder["sessions"]] == sorted((first, second))
    finally:
        await host.close()
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        async with client_for(host) as client:
            restored = (await client.get(route)).json()["session"]
            assert restored["title"] == "研究记录"
            assert restored["pinned"]
            unpinned = await client.patch(route, json={"expected_revision": 1, "pinned": False})
            assert not unpinned.json()["pinned"]
    finally:
        await host.close()


async def test_delete_purges_artifacts_fts_checkpoints_and_blocks_create_replay(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, upload_limit=1024 * 1024)
    try:
        async with client_for(host) as client:
            sid = await create(client, "delete-me")
            retained = await create(client, "keep-me")
            route = f"{BASE}/sessions/{sid}"
            app = await host.application("one")
            uploaded = await client.post(route + "/attachments", content=b"private",
                                         headers={"Content-Type": "application/octet-stream"})
            artifact = uploaded.json()["id"]
            run = (await client.post(route + "/runs", json={
                "submission_id": "execute", "text": "Temporary material",
                "artifact_ids": [artifact],
            })).json()["run_id"]
            await app.wait_idle()
            assert await app.saver.aget_tuple({"configurable": {"thread_id": sid}})
            owned = sorted(path.name for path in app.artifacts.root.iterdir())
            assert artifact in owned
            accepted = await client.delete(route)
            assert accepted.status_code == 202, accepted.text
            assert accepted.headers["Location"] == route + "/deletion"
            await app.wait_idle()
            state = await client.get(route + "/deletion")
            assert state.json()["status"] == "deleted"
            assert (await client.delete(route)).json()["status"] == "deleted"
            for path in (route, route + "/messages", route + "/messages/search?q=material",
                         f"{BASE}/runs/{run}", f"{BASE}/artifacts/{artifact}"):
                assert (await client.get(path)).status_code == 404
            assert (await client.get(f"{BASE}/sessions/{retained}")).status_code == 200
            assert all(not (app.artifacts.root / item).exists() for item in owned)
            assert await app.saver.aget_tuple({"configurable": {"thread_id": sid}}) is None
            async with app.store.engine.connect() as conn:
                for table in (
                    "messages_fts", "messages", "events", "admissions", "session_policies",
                ):
                    rows = await conn.exec_driver_sql(
                        f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (sid,)
                    )
                    assert rows.scalar() == 0
            replay = await client.post(f"{BASE}/sessions", json={"request_id": "delete-me"})
            assert replay.json()["code"] == "session_deleted"
    finally:
        await host.close()


async def test_delete_recovers_after_restart_and_requires_explicit_retry(tmp_path, monkeypatch):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        async with client_for(host) as client:
            sid = await create(client, "restart")
            route = f"{BASE}/sessions/{sid}"
            app = await host.application("one")
            monkeypatch.setattr(app.session_deletion, "schedule", lambda session: None)
            assert (await client.delete(route)).json()["status"] == "deleting"
            assert (await client.get(route)).status_code == 404
            rejected = await client.post(route + "/runs", json={
                "submission_id": "late", "text": "Must stay hidden",
            })
            assert rejected.status_code == 404
    finally:
        await host.close()
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        async with client_for(host) as client:
            app = await host.application("one")
            await app.wait_idle()
            assert (await client.get(route + "/deletion")).json()["status"] == "deleted"
            failed = await create(client, "failed")
            failed_route = f"{BASE}/sessions/{failed}"
            task = app.workspaces.home(failed)
            task.mkdir(parents=True)
            (task / "tree").mkdir()
            await client.delete(failed_route)
            await app.wait_idle()
            assert (await client.get(failed_route + "/deletion")).json()["status"] == (
                "cleanup_required"
            )
            assert (await client.get(f"{BASE}/session-deletions")).json()["deletions"] == [
                {"session_id": failed, "status": "cleanup_required", "reason": "workspace_unknown"},
            ]
            await asyncio.to_thread((task / "tree").rmdir)
            assert (await client.delete(failed_route)).status_code == 202
            await app.wait_idle()
            assert (await client.get(failed_route + "/deletion")).json()["status"] == "deleted"
    finally:
        await host.close()


async def test_delete_unregisters_owned_git_worktree_without_touching_source(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_text("keep", encoding="utf-8")
    await asyncio.to_thread(git, workspace, "init")
    await asyncio.to_thread(git, workspace, "add", "source.txt")
    await asyncio.to_thread(
        git, workspace, "-c", "user.email=test@example.invalid", "-c", "user.name=Test",
        "commit", "-m", "baseline",
    )
    host = ProjectHost(tmp_path / "state", workspace)
    try:
        async with client_for(host) as client:
            sid = await create(client, "worktree")
            app = await host.application("one")
            task = await app.workspaces.prepare(sid, "worktree")
            assert task.root.is_dir()
            route = f"{BASE}/sessions/{sid}"
            assert (await client.delete(route)).status_code == 202
            await app.wait_idle()
            assert (await client.get(route + "/deletion")).json()["status"] == "deleted"
            assert not task.root.exists()
            listed = await asyncio.to_thread(git, workspace, "worktree", "list", "--porcelain")
            assert sum(line.startswith(b"worktree ") for line in listed.splitlines()) == 1
            assert source.read_text(encoding="utf-8") == "keep"
    finally:
        await host.close()


@pytest.mark.parametrize("dependency", [
    "approval", "branch", "unknown", "dispatched", "read", "mismatch", "write",
])
async def test_delete_checks_actual_pending_work(tmp_path, dependency):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        async with client_for(host) as client:
            sid = await create(client, "parent")
            app = await host.application("one")
            route = f"{BASE}/sessions/{sid}"
            if dependency == "approval":
                await client.post(route + "/runs", json={
                    "submission_id": "approval", "text": "/write temporary.txt example",
                })
                await app.wait_idle()
                status = (await client.get(route)).json()
                assert status["latest_admission"]["status"] == "waiting_approval"
            elif dependency == "branch":
                branch = await client.post(route + "/forks", json={"request_id": "child"})
                assert branch.status_code == 200, branch.text
            else:
                session = await app.store.get_session(sid, "operator")
                admission = await app.store.admit(
                    session, principal="operator", submission_id="example", text="Test",
                )
                async with app.store.transaction() as conn:
                    await conn.exec_driver_sql(
                        "UPDATE admissions SET status = 'unknown' WHERE id = ?", (admission.id,),
                    )
                    if dependency in {"dispatched", "read", "mismatch", "write"}:
                        tool = "write" if dependency == "write" else "list_files"
                        args = {"path": "."}
                        digest = fingerprint({"name": tool, "arguments": args})
                        await conn.exec_driver_sql(
                            "INSERT INTO effects (id, admission_id, proposal_hash, status) "
                            "VALUES (?, ?, ?, 'dispatched')",
                            (admission.id + ":tool", admission.id,
                             "test" if dependency in {"dispatched", "mismatch"} else digest),
                        )
                        if dependency != "dispatched":
                            await conn.exec_driver_sql(
                                "INSERT INTO model_receipts "
                                "(id, admission_id, request_hash, response) VALUES (?, ?, ?, ?)",
                                (admission.id + ":model", admission.id, "request", json.dumps({
                                    "tool_calls": [{"id": "tool", "name": tool, "args": args}],
                                })),
                            )
            blocked = await client.delete(route)
            if dependency in {"branch", "dispatched", "mismatch", "write"}:
                assert blocked.json()["code"] == (
                    "session_deletion_blocked" if dependency == "branch"
                    else "session_deletion_reconciliation_required"
                )
                assert (await client.get(route)).status_code == 200
            else:
                assert blocked.status_code == 202, blocked.text
                await app.wait_idle()
                assert (await client.get(route + "/deletion")).json()["status"] == "deleted"
    finally:
        await host.close()
