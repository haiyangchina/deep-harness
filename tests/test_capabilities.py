"""Capability manifest is the single source of truth for new session grants."""

from typing import get_args

import pytest

from deep_harness.application.capabilities import (
    CAPABILITY_VERSION,
    CORE_TOOLS,
    EXTENSION_TOOLS,
    HARNESSES,
    LEGACY_TOOLS,
    READ_ONLY_CORE,
    READ_ONLY_HARNESSES,
    WEB_TOOLS,
    derive_grants,
    manifest,
)
from deep_harness.application.workspace import CODING_TOOLS, MUTATING_TOOLS, READ_TOOLS
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError, Harness
from deep_harness.runtime.harnesses import GUIDES, harness_contract, harness_prompt
from deep_harness.runtime.models import DiagnosticModel
from deep_harness.transports.api import CreateSession, create_api


def test_manifest_describes_four_core_capabilities():
    data = manifest()
    assert data["capability_version"] == CAPABILITY_VERSION
    assert [cap["key"] for cap in data["capabilities"]] == [
        "file_read",
        "file_write",
        "command_exec",
        "web_access",
    ]
    assert all("sub_capabilities" not in cap for cap in data["capabilities"])
    assert data["core_tools"] == list(CORE_TOOLS)
    assert len(CORE_TOOLS) == 10 and "local_time" not in CORE_TOOLS
    assert set(WEB_TOOLS) <= set(CORE_TOOLS)
    assert set(WEB_TOOLS) <= set(READ_ONLY_CORE)
    assert set(data["legacy_tools"]) == set(LEGACY_TOOLS)
    assert set(data["extensions"]) == set(EXTENSION_TOOLS)
    assert data["harnesses"] == list(HARNESSES)
    assert set(data["read_only_harnesses"]) == READ_ONLY_HARNESSES
    assert [scope["key"] for scope in data["access_scopes"]] == [
        "file_operations",
        "terminal_commands",
        "internet_access",
    ]
    policy = data["permission_policy"]
    assert set(policy["matrix"]) == {"ask", "auto", "full"}
    assert policy["matrix"]["ask"]["file_write:workspace"] == "per_call"
    assert policy["matrix"]["auto"]["file_write:workspace"] == "per_round"
    assert policy["matrix"]["auto"]["terminal:workspace"] == "per_round"
    assert policy["matrix"]["auto"]["internet:external"] == "per_call"
    assert policy["matrix"]["full"]["file_write:workspace"] == "exempt"
    assert policy["matrix"]["full"]["file_read:workspace"] == "exempt"
    assert policy["requires_acknowledgement"] == ["full"]
    assert data["hard_boundaries"]


def test_harness_contract_is_shared_by_domain_guides_and_manifest():
    assert HARNESSES == get_args(Harness)
    assert set(HARNESSES) == {"general", "coding", "research", "research_web"}
    assert READ_ONLY_HARNESSES == frozenset({"research", "research_web"})
    assert set(GUIDES) == set(HARNESSES)
    assert "[n]" in GUIDES["research_web"]
    assert harness_prompt("base", "research_web").startswith("base\n\n")
    assert harness_contract("research_web") != harness_contract("research")


def test_api_contract_accepts_research_web():
    body = CreateSession(request_id="x", prompt="p", engine="deep", harness="research_web")
    assert body.harness == "research_web"


def test_workspace_constants_are_derived():
    assert set(READ_ONLY_CORE) == {"read", "list_files", "search_files", *WEB_TOOLS}
    assert not set(WEB_TOOLS) & MUTATING_TOOLS
    assert set(READ_TOOLS) == set(READ_ONLY_CORE) | {"read_file"}
    assert {"write", "edit", "bash"} <= MUTATING_TOOLS
    assert set(CORE_TOOLS) | set(LEGACY_TOOLS) == set(CODING_TOOLS)


@pytest.mark.parametrize(
    "engine,harness,expected",
    [
        ("standard", "general", CORE_TOOLS),
        ("deep", "coding", CORE_TOOLS),
        ("deep", "research", READ_ONLY_CORE),
        ("deep", "research_web", READ_ONLY_CORE),
    ],
)
def test_derive_grants_for_each_harness(engine, harness, expected):
    assert derive_grants(engine, harness) == tuple(sorted(expected))


@pytest.mark.parametrize("harness", sorted(READ_ONLY_HARNESSES))
def test_read_only_harnesses_never_grant_mutating_tools(harness):
    granted = set(derive_grants("deep", harness))
    assert granted == set(READ_ONLY_CORE)
    assert not granted & MUTATING_TOOLS
    assert set(WEB_TOOLS) <= granted
    with pytest.raises(DomainError, match="Research harness cannot receive mutating tools"):
        derive_grants("deep", harness, ("save_memory",))


def test_extensions_are_explicit_and_gated():
    assert derive_grants("deep", "general", ("delegate_task",)) == tuple(
        sorted({*CORE_TOOLS, "delegate_task", "check_delegation"})
    )
    assert derive_grants("standard", "general", ("save_memory",)) == tuple(
        sorted({*CORE_TOOLS, "save_memory"})
    )
    assert "mcp__srv__tool" in derive_grants(
        "standard", "general", mcp_tools=("mcp__srv__tool",)
    )
    with pytest.raises(DomainError, match="Delegation is available only in Deep"):
        derive_grants("standard", "general", ("delegate_task",))
    with pytest.raises(DomainError, match="Unknown extension capability"):
        derive_grants("standard", "general", ("unknown_tool",))
    with pytest.raises(DomainError, match="Research harness cannot receive mutating tools"):
        derive_grants("deep", "research", ("save_memory",))
    with pytest.raises(DomainError, match="mcp__server__tool"):
        derive_grants("standard", "general", mcp_tools=("mcp__broken",))


def test_api_exposes_capabilities_route(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    app = create_api(host, "local-operator-token-" + "x" * 32)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/profiles/{profile_id}/capabilities" in paths


async def test_create_session_derives_grants_and_keeps_explicit_override(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: DiagnosticModel())
    try:
        app = await host.application("one")
        session = await app.create_session("create", "prompt")
        assert session.tools == tuple(sorted(CORE_TOOLS))
        # Replaying the same request_id is idempotent and keeps the frozen grant.
        replay = await app.create_session("create", "prompt")
        assert replay.id == session.id and replay.tools == session.tools
        explicit = await app.create_session("explicit", "prompt", ("read",))
        assert explicit.tools == ("read",)
        research = await app.create_session(
            "research", "prompt", engine="deep", harness="research"
        )
        assert set(research.tools) == set(READ_ONLY_CORE)
        with pytest.raises(DomainError, match="Research harness cannot receive mutating tools"):
            await app.create_session(
                "bad-research", "prompt", engine="deep", harness="research",
                extensions=("save_memory",),
            )
    finally:
        await host.close()


async def test_research_web_session_is_read_only_even_with_explicit_tools(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: DiagnosticModel())
    try:
        app = await host.application("web")
        # HTTP / desktop omit `tools`: backend derivation must stay read-only here.
        web = await app.create_session("web", "prompt", engine="deep", harness="research_web")
        assert web.harness == "research_web"
        assert set(web.tools) == set(READ_ONLY_CORE)
        assert set(WEB_TOOLS) <= set(web.tools)
        assert not set(web.tools) & MUTATING_TOOLS
        # An explicit grant cannot smuggle write/command tools into a research harness.
        with pytest.raises(DomainError, match="Research harness cannot receive mutating tools"):
            await app.create_session(
                "web-write", "prompt", ("bash",), engine="deep", harness="research_web"
            )
        with pytest.raises(DomainError, match="Research harness cannot receive mutating tools"):
            await app.create_session(
                "web-memory", "prompt", engine="deep", harness="research_web",
                extensions=("save_memory",),
            )
        with pytest.raises(DomainError, match="Specialized harness requires Deep engine"):
            await app.create_session("web-standard", "prompt", harness="research_web")
    finally:
        await host.close()
