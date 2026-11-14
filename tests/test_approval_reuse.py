"""The three-mode policy table and per-admission approval reuse."""

from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from deep_harness.application.capabilities import (
    ALL_SCOPE_KEYS,
    ALWAYS_PER_CALL,
    policy_for,
    reusable,
)
from deep_harness.application.permissions import approval_reason, approval_scope, classify
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import ProfileScope, Session, ToolCall
from deep_harness.infrastructure import schema as s


def session_for(mode):
    return Session(
        "one", "profile", "operator", "prompt", ("read", "write", "bash"), "standard",
        permission_mode=mode,
    )


def scope_for(tmp_path):
    return ProfileScope("profile", tmp_path / "state", tmp_path, tmp_path / "state")


@pytest.mark.parametrize("mode", ["ask", "auto", "full"])
def test_policy_table_is_exhaustive(mode):
    assert {key: policy_for(mode, key) for key in ALL_SCOPE_KEYS}
    assert policy_for(mode, "file_read:workspace") == "exempt"
    for key in ALWAYS_PER_CALL:
        assert not reusable(mode, key)
        assert policy_for("full", key) == "exempt"
        assert policy_for("ask", key) == "per_call"
    assert reusable("auto", "file_write:workspace") is True
    assert reusable("auto", "terminal:workspace") is True
    assert reusable("ask", "file_write:workspace") is False
    assert reusable("full", "file_write:workspace") is False


@pytest.mark.parametrize(
    "name,arguments,expected_scope",
    [
        ("read", {"path": "inside.txt"}, "file_read:workspace"),
        ("write", {"path": "inside.txt", "content": "x"}, "file_write:workspace"),
        ("bash", {"command": "date", "cwd": "."}, "terminal:workspace"),
        ("read", {"path": "../outside.txt"}, "outside:workspace"),
        ("read", {"path": ".env"}, "sensitive:path"),
        ("mcp__srv__tool", {}, "internet:external"),
        ("save_memory", {"content": "note"}, "persistent:state"),
    ],
)
def test_classify_never_inspects_command_text(tmp_path, name, arguments, expected_scope):
    scope = scope_for(tmp_path)
    call = ToolCall("one", name, arguments)
    key, _ = classify(call, scope, lambda path: (tmp_path / path).resolve())
    assert key == expected_scope


def test_approval_scope_drives_the_three_modes(tmp_path):
    scope = scope_for(tmp_path)
    call = ToolCall("one", "write", {"path": "a.txt", "content": "x"})
    def resolve(path):
        return (tmp_path / path).resolve()
    assert approval_scope(call, session_for("ask"), scope, resolve) == (
        "file_write:workspace", "per_call", False,
    )
    assert approval_scope(call, session_for("auto"), scope, resolve) == (
        "file_write:workspace", "per_round", True,
    )
    assert approval_scope(call, session_for("full"), scope, resolve) == (
        "file_write:workspace", "exempt", False,
    )
    outside = ToolCall("one", "read", {"path": "../outside.txt"})
    assert approval_scope(outside, session_for("auto"), scope, resolve)[1:] == (
        "per_call", False,
    )
    external = ToolCall("one", "mcp__srv__tool", {})
    assert approval_scope(external, session_for("auto"), scope, resolve)[1:] == (
        "per_call", False,
    )


def test_auto_no_longer_silently_allows_workspace_writes(tmp_path):
    scope = scope_for(tmp_path)
    call = ToolCall("one", "write", {"path": "a.txt", "content": "x"})
    def resolve(path):
        return (tmp_path / path).resolve()
    assert approval_reason(call, session_for("auto"), scope, resolve) == "修改文件需要批准"
    assert approval_reason(call, session_for("full"), scope, resolve) is None


async def test_scope_grant_lifecycle_is_scoped_and_revocable(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "prompt", permission_mode="auto")
        claim = SimpleNamespace(
            admission=SimpleNamespace(id="adm-1", session_id=session.id)
        )
        async with app.store.transaction() as conn:
            await conn.execute(
                insert(s.interactions).values(
                    id="adm-1:call-1",
                    session_id=session.id,
                    admission_id="adm-1",
                    proposal_hash="digest",
                    proposal={
                        "scope_key": "terminal:workspace",
                        "reusable": True,
                        "permission_revision": session.permission_revision,
                    },
                    decision="approve",
                )
            )
        assert await app.receipts.scope_granted(
            claim, "terminal:workspace", session.permission_revision, "adm-1:call-2"
        )
        # A different scope, a changed mode revision and another admission never reuse.
        assert not await app.receipts.scope_granted(
            claim, "internet:external", session.permission_revision, "adm-1:call-2"
        )
        assert not await app.receipts.scope_granted(
            claim, "terminal:workspace", session.permission_revision + 1, "adm-1:call-2"
        )
        other = SimpleNamespace(
            admission=SimpleNamespace(id="adm-2", session_id=session.id)
        )
        assert not await app.receipts.scope_granted(
            other, "terminal:workspace", session.permission_revision
        )
        # The operator can withdraw reuse for the current admission.
        await app.receipts.revoke_reuse(session.id, "adm-1")
        assert not await app.receipts.scope_granted(
            claim, "terminal:workspace", session.permission_revision, "adm-1:call-2"
        )
        assert await app.receipts.scope_granted(
            other, "terminal:workspace", session.permission_revision
        ) is False
    finally:
        await host.close()
