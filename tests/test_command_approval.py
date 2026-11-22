"""Every local command follows the same approval policy; no command is privileged."""

import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from deep_harness.application.capabilities import CORE_TOOLS, WEB_TOOLS
from deep_harness.application.permissions import approval_reason
from deep_harness.application.shell import BashArgs, bash
from deep_harness.application.tools import TOOL_ARGUMENTS, ToolGateway, tool_definitions
from deep_harness.application.workspace import PROJECT_PROMPT
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import ProfileScope, Session, ToolCall
from deep_harness.runtime.models import LangChainModel

COMMANDS = (
    "pwd",
    "echo safe",
    "python --version",
    "git status --short",
    "echo hi > note.txt",
    "curl https://example.invalid",
    "ls -la",
    "npm test",
)
COMMAND = "echo harness-stdout"


def scope_for(tmp_path):
    return ProfileScope("profile", tmp_path / "state", tmp_path, tmp_path / "state")


def session_for(mode):
    return Session(
        "one", "profile", "operator", "prompt", ("bash",), "standard", permission_mode=mode
    )


@pytest.mark.parametrize("mode", ["ask", "auto"])
@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_requires_per_call_approval(tmp_path, mode, command):
    session = session_for(mode)
    scope = scope_for(tmp_path)
    call = ToolCall("one", "bash", {"command": command, "cwd": "."})
    gateway = ToolGateway(scope, session, None, None)
    assert gateway.approval_reason(call) is not None
    assert approval_reason(call, session, scope, lambda path: tmp_path / path) is not None


@pytest.mark.parametrize("command", ["pwd", "echo safe", "npm test"])
def test_full_mode_still_skips_approval(tmp_path, command):
    session = session_for("full")
    scope = scope_for(tmp_path)
    call = ToolCall("one", "bash", {"command": command, "cwd": "."})
    assert ToolGateway(scope, session, None, None).approval_reason(call) is None
    assert approval_reason(call, session, scope, lambda path: tmp_path / path) is None


def test_outside_cwd_reason_is_preserved(tmp_path):
    session = session_for("ask")
    scope = scope_for(tmp_path)
    call = ToolCall("one", "bash", {"command": COMMAND, "cwd": ".."})
    reason = approval_reason(call, session, scope, lambda path: (tmp_path / path).resolve())
    assert reason == "在工作区外或敏感目录执行命令"


def test_core_grant_has_no_special_case_commands():
    assert "local_time" not in TOOL_ARGUMENTS
    assert set(CORE_TOOLS) == {
        "read",
        "list_files",
        "search_files",
        "write",
        "edit",
        "bash",
        *WEB_TOOLS,
    }
    definition = next(spec for spec in tool_definitions(("bash",)) if spec["name"] == "bash")
    description = definition["description"]
    assert "per call" in description
    assert "clock" not in description.lower()
    assert "clock" not in PROJECT_PROMPT.lower()


async def test_real_shell_command_returns_stdout_text(tmp_path):
    result = await bash(
        BashArgs(command=COMMAND, shell="auto", timeout_seconds=15),
        lambda path: tmp_path / path,
    )
    assert result["exit_code"] == 0
    assert "harness-stdout" in result["stdout"]


class OneCommandChat(BaseChatModel):
    command: str = COMMAND

    @property
    def _llm_type(self):
        return "command-approval-test"

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "harness-test", "ls_model_name": "command-approval"}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        results = [message.content for message in messages if message.type == "tool"]
        if not results:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {"name": "bash", "args": {"command": self.command}, "id": "command-1"}
                ],
            )
        else:
            answer = AIMessage(content=json.dumps(results))
        return ChatResult(generations=[ChatGeneration(message=answer)])


@pytest.mark.parametrize("mode", ["ask", "auto", "full"])
async def test_agent_command_follows_approval_mode(tmp_path, mode, monkeypatch):
    dispatched = []

    async def fake_bash(arguments, path):
        dispatched.append(arguments.command)
        return {"exit_code": 0, "stdout": "harness-stdout", "stderr": ""}

    monkeypatch.setattr("deep_harness.application.tools.bash", fake_bash)
    host = ProjectHost(
        tmp_path / "state",
        tmp_path,
        lambda scope: LangChainModel(OneCommandChat(command=COMMAND)),
    )
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create", "Use bash", ("bash",), permission_mode=mode,
            full_access_acknowledged=(mode == "full"),
        )
        admission = await app.submit(session.id, "send", "运行一条本地命令")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        if mode != "full":
            assert pending.status == "waiting_approval"
            assert dispatched == []
            approval = (await app.sessions.status(session.id))["approvals"][0]
            assert approval["proposal"]["tool_name"] == "bash"
            assert approval["proposal"]["arguments"]["command"] == COMMAND
            await app.approve(session.id, approval["id"], pending.generation, "approve")
            await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
        assert dispatched == [COMMAND]
    finally:
        await host.close()
