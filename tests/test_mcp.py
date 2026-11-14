"""MCP tools are pinned to the session and always require an approval."""

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.runtime.mcp import ServerConfig


class McpModel:
    identity = {"provider": "mcp-test", "version": 1}
    request_timeout = 10

    async def complete(self, prompt, messages, tools):
        if messages[-1]["role"] == "tool":
            return {"role": "assistant", "content": messages[-1]["content"]}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "echo-1", "name": "mcp__echo__echo", "args": {"text": "hello"}}],
        }


async def test_mcp_tool_is_explicitly_granted_and_approval_gated(tmp_path):
    server = MCPServer("echo")
    calls = []

    @server.tool()
    async def echo(text: str) -> str:
        calls.append(text)
        return text.upper()

    host = ProjectHost(
        tmp_path / "state",
        tmp_path,
        lambda scope: McpModel(),
        mcp_objects={"one": {"echo": server}},
    )
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Prompt", ("mcp__echo__echo",))
        admission = await app.submit(session.id, "send", "call echo")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "waiting_approval"
        assert calls == []
        proposal = (await app.sessions.status(session.id))["approvals"][0]
        await app.approve(session.id, proposal["id"], pending.generation, "approve")
        await app.wait_idle()
        completed = await app.store.get_admission(admission.id, session.id)
        assert completed.status == "completed"
        assert calls == ["hello"]
        assert "HELLO" in completed.result
    finally:
        await host.close()


async def test_configured_stdio_server_is_discovered_and_called(tmp_path):
    home = tmp_path / "state" / "profiles" / "one"
    home.mkdir(parents=True)
    fixture = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    (home / "mcp.toml").write_text(
        "[[servers]]\nname = 'echo'\ntransport = 'stdio'\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = [{json.dumps(str(fixture))}]\n",
        encoding="utf-8",
    )
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: McpModel())
    try:
        app = await host.application("one")
        catalog = await app.mcp.discover()
        assert "mcp__echo__echo" in catalog
        original_connection = app.mcp.connections["echo"][1]
        assert "HELLO" in await app.mcp.call(
            "mcp__echo__echo", {"text": "hello"}, catalog["mcp__echo__echo"]["schema_hash"]
        )
        assert app.mcp.connections["echo"][1] is original_connection
        session = await app.create_session("create", "Prompt", ("mcp__echo__echo",))
        admission = await app.submit(session.id, "send", "call echo")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        proposal = (await app.sessions.status(session.id))["approvals"][0]
        await app.approve(session.id, proposal["id"], pending.generation, "approve")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
        await host.close()
        (home / "mcp.toml").write_text(
            "[[servers]]\nname = 'echo'\ntransport = 'stdio'\n"
            f"command = {json.dumps(sys.executable)}\n"
            f"args = [{json.dumps(str(fixture))}, '--changed']\n",
            encoding="utf-8",
        )
        host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: McpModel())
        app = await host.application("one")
        status = await app.sessions.status(session.id)
        assert status["runtime"]["compatible"] is False
        with pytest.raises(DomainError, match="runtime changed"):
            await app.submit(session.id, "send", "call echo")
    finally:
        await host.close()


def test_oauth_config_requires_explicit_issuer_and_secret_references():
    with pytest.raises(ValueError, match="issuer"):
        ServerConfig.model_validate(
            {
                "name": "remote",
                "transport": "http",
                "url": "https://example.org/mcp",
                "auth": "client_credentials",
                "client_id_env": "MCP_ID",
                "client_secret_env": "MCP_SECRET",
            }
        )
    configured = ServerConfig.model_validate(
        {
            "name": "remote",
            "transport": "http",
            "url": "https://example.org/mcp",
            "auth": "client_credentials",
            "client_id_env": "MCP_ID",
            "client_secret_env": "MCP_SECRET",
            "issuer": "https://example.org",
        }
    )
    assert "MCP_SECRET" in configured.model_dump_json()
    assert "actual-secret" not in configured.model_dump_json()


async def test_oauth_missing_credentials_fails_before_network(tmp_path, monkeypatch):
    home = tmp_path / "state" / "profiles" / "one"
    home.mkdir(parents=True)
    (home / "mcp.toml").write_text(
        "[[servers]]\nname = 'oauth'\ntransport = 'http'\n"
        "url = 'https://example.org/mcp'\nauth = 'client_credentials'\n"
        "issuer = 'https://example.org'\nclient_id_env = 'DEEP_HARNESS_TEST_CLIENT_ID'\n"
        "client_secret_env = 'DEEP_HARNESS_TEST_CLIENT_SECRET'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEP_HARNESS_TEST_CLIENT_ID", raising=False)
    monkeypatch.delenv("DEEP_HARNESS_TEST_CLIENT_SECRET", raising=False)
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: McpModel())
    try:
        app = await host.application("one")
        with pytest.raises(DomainError, match="credentials are missing"):
            await app.mcp.discover()
    finally:
        await host.close()


async def test_unrelated_unavailable_server_does_not_block_granted_tool(tmp_path):
    server = MCPServer("echo")

    @server.tool()
    async def echo(text: str) -> str:
        return text

    home = tmp_path / "state" / "profiles" / "one"
    home.mkdir(parents=True)
    (home / "mcp.toml").write_text(
        "[[servers]]\nname = 'offline'\ntransport = 'stdio'\ncommand = 'C:/missing/server.exe'\n",
        encoding="utf-8",
    )
    host = ProjectHost(
        tmp_path / "state",
        tmp_path,
        lambda scope: McpModel(),
        mcp_objects={"one": {"echo": server}},
    )
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Prompt", ("mcp__echo__echo",))
        assert session.tools == ("mcp__echo__echo",)
        with pytest.raises(DomainError, match="executable is missing"):
            await app.mcp.discover()
    finally:
        await host.close()


async def test_configured_http_transport_calls_external_server(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(fixture),
        "--http",
        str(port),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: McpModel())
    try:
        for _ in range(50):
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("MCP HTTP fixture did not start")
        home = tmp_path / "state" / "profiles" / "one"
        home.mkdir(parents=True)
        (home / "mcp.toml").write_text(
            "[[servers]]\nname = 'echo'\ntransport = 'http'\n"
            f"url = 'http://127.0.0.1:{port}/mcp'\n",
            encoding="utf-8",
        )
        app = await host.application("one")
        catalog = await app.mcp.discover()
        assert "mcp__echo__echo" in catalog
        assert "HELLO" in await app.mcp.call(
            "mcp__echo__echo", {"text": "hello"}, catalog["mcp__echo__echo"]["schema_hash"]
        )
    finally:
        await host.close()
        process.terminate()
        await process.wait()
