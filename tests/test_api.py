"""Authenticated HTTP contract and durable input handling."""

import asyncio

import httpx
import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.runtime.models import DiagnosticModel
from deep_harness.transports.api import create_api

TOKEN = "test-local-operator-token-with-32-characters"


async def test_first_model_fragment_reaches_waiting_client_before_completion(tmp_path):
    publish = asyncio.Event()
    finish = asyncio.Event()
    model_started = asyncio.Event()

    class StreamingModel(DiagnosticModel):
        async def stream(self, prompt, messages, tools):
            model_started.set()
            await publish.wait()
            yield {"kind": "delta", "text": "First"}
            await finish.wait()
            yield {"kind": "delta", "text": " final"}
            yield {"kind": "result", "response": {"role": "assistant", "content": "First final"}}

    host = ProjectHost(tmp_path / "state", tmp_path, lambda _: StreamingModel())
    api = create_api(host, TOKEN, workspace_id="one")
    prefix = "/api/v1/workspaces/one/sessions"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://local",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            created = await client.post(prefix, json={"request_id": "stream"})
            route = f"{prefix}/{created.json()['id']}"
            accepted = await client.post(route + "/runs", json={
                "submission_id": "stream-run", "text": "Example",
            })
            assert accepted.status_code == 202
            async with asyncio.timeout(3):
                await model_started.wait()
                history = (await client.get(route + "/event-history")).json()["events"]
                cursor = history[-1]["seq"]
                waiting = asyncio.create_task(client.get(route + "/event-history", params={
                    "after": cursor, "wait": 3,
                }))
                await asyncio.sleep(0)
                publish.set()
                page = (await waiting).json()["events"]
            fragments = [
                event["payload"]["text"] for event in page if event["type"] == "model.delta"
            ]
            assert fragments == ["First"]
            assert (await client.get(route)).json()["latest_admission"]["status"] == "started"
            finish.set()
            await (await host.application("one")).wait_idle()
            status = (await client.get(route)).json()
            assert status["latest_admission"]["result"] == "First final"
    finally:
        publish.set()
        finish.set()
        await host.close()


async def test_deep_harness_and_delegation_catalog_are_authentic_session_fields(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    api = create_api(host, TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://local"
        ) as client:
            created = await client.post(
                "/profiles/one/sessions",
                headers=headers,
                json={
                    "request_id": "create",
                    "harness": "research",
                    "tools": ["read", "delegate_task"],
                },
            )
            assert created.status_code == 200
            assert created.json()["harness"] == "research"
            route = f"/profiles/one/sessions/{created.json()['id']}/delegations"
            assert (await client.get(route)).status_code == 401
            assert (await client.get(route, headers=headers)).json() == {"delegations": []}
            assert (await client.get(f"{route}/missing", headers=headers)).status_code == 404
            assert (
                await client.post(
                    f"{route}/missing/steers",
                    headers=headers,
                    json={"submission_id": "steer", "text": "next task"},
                )
            ).status_code == 404
            assert (await client.post(f"{route}/missing/stop", headers=headers)).status_code == 404
            invalid = await client.post(
                "/profiles/one/sessions",
                headers=headers,
                json={"request_id": "invalid", "engine": "standard", "harness": "coding"},
            )
            assert invalid.status_code == 422
    finally:
        await host.close()


async def test_authenticated_submit_returns_durable_receipt_and_replayable_events(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    api = create_api(host, TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api),
            base_url="http://local",
        ) as client:
            assert (
                await client.post(
                    "/profiles/one/sessions",
                    json={"request_id": "create"},
                )
            ).status_code == 401
            created = await client.post(
                "/profiles/one/sessions",
                headers=headers,
                json={"request_id": "create"},
            )
            assert created.status_code == 200
            session_id = created.json()["id"]
            route = f"/profiles/one/sessions/{session_id}"
            first = await client.post(
                route + "/inputs",
                headers=headers,
                json={"submission_id": "send", "text": "hello"},
            )
            assert first.status_code == 202
            retry = await client.post(
                route + "/inputs",
                headers=headers,
                json={"submission_id": "send", "text": "hello"},
            )
            assert retry.json()["id"] == first.json()["id"]
            await (await host.application("one")).wait_idle()
            snapshot = await client.get(route, headers=headers)
            assert len(snapshot.json()["messages"]) == 2
            usage = await client.get(route + "/usage", headers=headers)
            assert usage.status_code == 200
            assert usage.json()["model_calls"] == 1
            assert usage.json()["contains_estimates"] is True
            first_page = await client.get(route + "/messages?limit=1", headers=headers)
            assert len(first_page.json()["messages"]) == 1
            cursor = first_page.json()["next_cursor"]
            second_page = await client.get(
                route + f"/messages?limit=1&after={cursor}", headers=headers
            )
            assert len(second_page.json()["messages"]) == 1
            search = await client.get(route + "/messages/search?q=hello", headers=headers)
            assert len(search.json()["messages"]) == 2
            foreign = await client.get(
                f"/profiles/two/sessions/{session_id}",
                headers=headers,
            )
            assert foreign.status_code == 404
            events = (await client.get(route + "/events", headers=headers)).json()["events"]
            assert [event["seq"] for event in events] == sorted(
                {event["seq"] for event in events},
            )
            assert events[-1]["type"] == "execution.completed"
    finally:
        await host.close()


async def test_versioned_local_api_keeps_the_existing_profile_and_requires_bearer(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    api = create_api(host, TOKEN, workspace_id="one")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    root = "/api/v1/workspaces/one"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://local"
        ) as client:
            assert (await client.get(root + "/sessions")).status_code == 401
            foreign = await client.get("/api/v1/workspaces/two/sessions", headers=headers)
            assert foreign.status_code == 404
            assert not (tmp_path / "state" / "profiles" / "two").exists()
            payload = {"request_id": "existing"}
            old = await client.post("/profiles/one/sessions", headers=headers, json=payload)
            new = await client.post(root + "/sessions", headers=headers, json=payload)
            assert new.status_code == 200, new.text
            assert new.json()["id"] == old.json()["id"]
            assert new.headers["cache-control"] == "no-store"
    finally:
        await host.close()


async def test_authenticated_knowledge_routes_freeze_new_session_prompt(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    api = create_api(host, TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://local"
        ) as client:
            route = "/profiles/one"
            skill = "---\nname: concise\ndescription: Brief answers\n---\nUse one sentence."
            assert (
                await client.post(route + "/skills", json={"document": skill})
            ).status_code == 401
            assert (
                await client.post(route + "/skills", headers=headers, json={"document": skill})
            ).status_code == 200
            memory = await client.post(
                route + "/memories", headers=headers, json={"content": "用户喜欢中文"}
            )
            assert memory.status_code == 200
            created = await client.post(
                route + "/sessions",
                headers=headers,
                json={"request_id": "deep", "harness": "coding"},
            )
            assert created.status_code == 200
            assert created.json()["engine"] == "deep"
            assert "Use one sentence." in created.json()["prompt"]
            assert "用户喜欢中文" in created.json()["prompt"]
            found = await client.get(route + "/memories/search?q=中文", headers=headers)
            assert found.json()["memories"][0]["id"] == memory.json()["id"]
    finally:
        await host.close()


@pytest.mark.parametrize("profile", ["../escape", "bad profile", ".hidden", ""])
async def test_profile_selectors_are_validated_before_opening_store(tmp_path, profile):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        with pytest.raises(DomainError, match="identifier"):
            await host.application(profile)
    finally:
        await host.close()
