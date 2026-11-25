"""Multimodal admissions, model requests and image ownership without a live model."""

import io
import json
from base64 import b64decode
from contextlib import asynccontextmanager

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from PIL import Image

from deep_harness.application.instance import InstanceHost
from deep_harness.application.instance_config import InstanceSettings
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.images import MAX_IMAGE_BYTES
from deep_harness.domain.models import DomainError
from deep_harness.runtime.models import LangChainModel
from deep_harness.transports.api import create_api
from deep_harness.transports.web import create_web_api


class VisionFixture(BaseChatModel):
    calls: list = []
    action: str = ""

    @property
    def _llm_type(self):
        return "harness-image-test"

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "harness-test", "ls_model_name": "images"}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        images = []
        prompts = []
        for message in messages:
            if message.type == "human" and isinstance(message.content, list):
                prompts.append(message.content[0]["text"])
                for block in message.content[1:]:
                    images.append(b64decode(block["image_url"]["url"].split(",", 1)[1]))
        self.calls.append((images, prompts))
        has_tool_result = any(item.type == "tool" for item in messages)
        if self.action and not has_tool_result and "specialist" not in prompts:
            name = "write" if self.action == "write" else "delegate_task"
            args = (
                {"path": "approved.txt", "content": "approved"}
                if self.action == "write"
                else {
                    "kind": "research",
                    "task": "specialist",
                    "share_images": True,
                    "background": self.action == "background",
                }
            )
            message = AIMessage(
                content="", tool_calls=[{"id": "vision-tool", "name": name, "args": args}]
            )
        else:
            report = dict(summary="Image seen", evidence=[], changes=[], tests=[], limitations=[])
            message = AIMessage(content=json.dumps(report))
        return ChatResult(generations=[ChatGeneration(message=message)])


def build_model(action=""):
    adapter = LangChainModel(VisionFixture(action=action), 15, identity={"fixture": "vision"})
    adapter.vision = True
    return adapter


def picture(format="PNG", color="blue"):
    stream = io.BytesIO()
    Image.new("RGB", (40, 24), color).save(stream, format)
    return stream.getvalue()


async def upload(app, session, data):
    async def chunks():
        yield data

    return (await app.publish_attachment(session.id, chunks(), "sample", image=True))["id"]


def project(tmp_path, model):
    return ProjectHost(tmp_path / "state", tmp_path, lambda _: model, upload_limit=MAX_IMAGE_BYTES)


@asynccontextmanager
async def opened_project(tmp_path, model):
    host = project(tmp_path, model)
    try:
        yield host
    finally:
        await host.close()


@pytest.mark.parametrize("harness", ["general", "research", "research_web", "coding"])
async def test_images_reach_every_harness_and_remain_available_after_restart(tmp_path, harness):
    model = build_model()
    async with opened_project(tmp_path, model) as host:
        app = await host.application("origin")
        session = await app.create_session(
            "create",
            "Use the inputs",
            (),
            harness=harness,
            engine="standard" if harness == "general" else "deep",
        )
        plain = await app.submit(session.id, "plain", "plain text")
        await app.wait_idle()
        assert (await app.store.get_admission(plain.id, session.id)).status == "completed"
        assert model.model.calls[-1] == ([], [])
        first, second = picture(), picture("JPEG", "red")
        ids = [await upload(app, session, data) for data in (first, second)]
        admitted = await app.submit(session.id, "images", "", image_ids=ids)
        await app.wait_idle()
        assert (await app.store.get_admission(admitted.id, session.id)).status == "completed"
        assert model.model.calls[-1] == ([first, second], ["请分析这些图片。"])
        assert (await app.store.history(session.id, admission_id=admitted.id))[0][
            "image_ids"
        ] == ids
        assert (await app.store.session_status(session.id, "operator"))["image_context"] == ids
        assert (await app.submit(session.id, "images", "", image_ids=ids)).id == admitted.id
        with pytest.raises(DomainError, match="payload changed"):
            await app.submit(session.id, "images", "", image_ids=ids[::-1])

    async with opened_project(tmp_path, model) as restarted:
        app = await restarted.application("origin")
        next_run = await app.submit(session.id, "followup", "Read the previous pictures")
        await app.wait_idle()
        assert (await app.store.get_admission(next_run.id, session.id)).status == "completed"
        assert model.model.calls[-1][0] == [first, second]
        mixed = await app.submit(session.id, "mixed", "This is red", image_ids=[ids[1]])
        await app.wait_idle()
        assert (await app.store.get_admission(mixed.id, session.id)).status == "completed"
        assert model.model.calls[-1] == (
            [first, second, second],
            ["请分析这些图片。", "This is red"],
        )


async def test_pending_image_approval_survives_backup_and_branch_preserves_ownership(tmp_path):
    model = build_model("write")
    async with opened_project(tmp_path, model) as host:
        app = await host.application("origin")
        session = await app.create_session(
            "create", "Write", ("write",), engine="deep", harness="coding"
        )
        other = await app.create_session("other", "Test isolation", ())
        data = picture()
        image_id = await upload(app, session, data)
        with pytest.raises(DomainError, match="not accessible"):
            await app.submit(other.id, "foreign", "Read", image_ids=[image_id])
        run = await app.submit(session.id, "send", "Write after looking", image_ids=[image_id])
        await app.wait_idle()
        pending = await app.store.session_status(session.id, "operator")
        approval = pending["approvals"][0]
        assert pending["latest_admission"].status == "waiting_approval"
        archive = tmp_path / "images.zip"
        await app.backup(archive)
        await host.restore("restored", archive)
        restored = await host.application("restored")
        await restored.approve(session.id, approval["id"], approval["generation"], "approve")
        await restored.wait_idle()
        assert (await restored.store.get_admission(run.id, session.id)).status == "completed"
        assert model.model.calls[-1][0] == [data]
        branch = await restored.fork_session(session.id, "fork")
        row, path = await restored.get_image(branch.id, image_id)
        assert await restored.artifacts.image_bytes(row, path) == data
        assert (await restored.store.live_history(branch.id))["items"][0]["message"][
            "image_ids"
        ] == [image_id]


@pytest.mark.parametrize("action", ["sync", "background"])
async def test_delegation_explicitly_shares_current_message_images(tmp_path, action):
    model = build_model(action)
    async with opened_project(tmp_path, model) as host:
        app = await host.application("default")
        session = await app.create_session("create", "Delegate", ("delegate_task",), engine="deep")
        data = picture()
        image_id = await upload(app, session, data)
        await app.submit(session.id, "send", "Parent task", image_ids=[image_id])
        await app.wait_idle()
        assert ([data], ["specialist"]) in model.model.calls


@pytest.mark.parametrize("web", [False, True])
async def test_image_http_contract(tmp_path, web):
    model = build_model()
    token = "a" * 40
    if web:
        config = InstanceSettings.model_validate(
            {
                "workspaces": {"default": {"path": tmp_path}},
                "models": {"local": {"adapter": "ollama", "model": "qwen3.5:9b"}},
                "default_workspace": "default",
                "default_model": "local",
            }
        )
        host = InstanceHost(config, tmp_path / "state", model_factory=lambda _: model)
        api = create_web_api(host, None)
        location, submit = "/api/v1/workspaces/default/sessions", "runs"
        headers = {"x-harness-upload": "1"}
    else:
        host = project(tmp_path, model)
        api = create_api(host, token)
        location, submit = "/profiles/default/sessions", "inputs"
        headers = {"Authorization": f"Bearer {token}"}
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://127.0.0.1", headers=headers
        ) as client,
    ):
        one = (await client.post(location, json={"request_id": "one"})).json()["id"]
        two = (await client.post(location, json={"request_id": "two"})).json()["id"]
        path = f"{location}/{one}/images"
        mime = {"Content-Type": "application/octet-stream"}
        data = picture()
        invalid = await client.post(path, content=b"fake PNG", headers=mime)
        assert invalid.json()["code"] == "image_format"
        response = await client.post(path, content=data, headers=mime)
        assert response.status_code == 201, response.text
        image_id = response.json()["id"]
        assert (await client.get(f"{path}/{image_id}")).content == data
        thumbnail = await client.get(f"{path}/{image_id}?thumbnail=1")
        assert thumbnail.headers["Content-Type"] == "image/png"
        assert (await client.get(f"{location}/{two}/images/{image_id}")).status_code == 404
        body = {"submission_id": "image-only", "image_ids": [image_id]}
        response = await client.post(f"{location}/{one}/{submit}", json=body)
        assert response.status_code == 202, response.text
        found = await client.get(f"{location}/{one}/submission?submission_id=image-only")
        assert found.json() is not None
        assert (await client.post(f"{location}/{two}/{submit}", json=body)).status_code == 404
        await (await host.application("default")).wait_idle()
        assert model.model.calls[-1][0] == [data]


async def test_image_budget_evicts_oldest_image_and_accepts_a_new_reference(tmp_path, monkeypatch):
    from deep_harness.runtime import images

    first, second = picture(color="red"), picture(color="blue")
    monkeypatch.setattr(images, "IMAGE_REQUEST_BYTES", max(len(first), len(second)))
    model = build_model()
    async with opened_project(tmp_path, model) as host:
        app = await host.application("origin")
        session = await app.create_session("create", "Look at images", ())
        first_id, second_id = [await upload(app, session, data) for data in (first, second)]
        for message, image_id in (("first", first_id), ("second", second_id), ("reuse", first_id)):
            await app.submit(session.id, message, message, image_ids=[image_id])
            await app.wait_idle()
            status = await app.store.session_status(session.id, "operator")
            assert status["image_context"] == [image_id]
            assert status["latest_admission"].status == "completed"
        assert model.model.calls[-1][0] == [first]


async def test_text_model_rejects_image_upload_and_keeps_text_input(tmp_path):
    model = build_model()
    model.vision = False
    async with opened_project(tmp_path, model) as host:
        app = await host.application("origin")
        session = await app.create_session("create", "Use text only", ())
        with pytest.raises(DomainError, match="cannot accept images"):
            await upload(app, session, picture())
        await app.submit(session.id, "send", "hello")
        await app.wait_idle()
        assert model.model.calls[-1] == ([], [])
