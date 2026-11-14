"""Native command effects require a durable human decision."""

import json
import sys

from deep_harness.bootstrap import ProjectHost


class CommandModel:
    request_timeout = 10
    identity = {"provider": "command-test", "version": 1}

    async def complete(self, prompt, messages, tools):
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": messages[-1]["content"]}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "command-1",
                    "name": "run_command",
                    "args": {
                        "argv": [
                            sys.executable,
                            "-c",
                            "open('terminal-result.txt', 'w').write('ok')",
                        ],
                        "cwd": ".",
                    },
                }
            ],
        }


async def test_command_waits_for_approval_and_records_one_effect(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: CommandModel())
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Prompt", ("run_command",))
        admission = await app.submit(session.id, "send", "run")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "waiting_approval"
        assert not (tmp_path / "terminal-result.txt").exists()
        approval = (await app.sessions.status(session.id))["approvals"][0]
        await app.approve(session.id, approval["id"], pending.generation, "approve")
        await app.wait_idle()
        completed = await app.store.get_admission(admission.id, session.id)
        assert completed.status == "completed"
        assert (tmp_path / "terminal-result.txt").read_text() == "ok"
        assert json.loads(completed.result)["exit_code"] == 0
        effects = (await app.sessions.status(session.id))["active_effects"]
        assert effects == []
    finally:
        await host.close()
