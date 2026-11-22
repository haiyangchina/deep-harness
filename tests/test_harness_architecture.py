"""Contract guards for the unified deepagents execution kernel.

These tests lock the architectural decisions of the refactor so a deepagents
upgrade or an accidental regression fails fast. They cover the plan's four
guard contracts at the structural level plus the single-source-of-truth and
user-facing-surface guarantees.
"""

from dataclasses import FrozenInstanceError

import pytest
from deepagents.backends.protocol import BackendProtocol

from deep_harness.application.capabilities import (
    READ_ONLY_HARNESSES,
    derive_grants,
    manifest,
)
from deep_harness.application.config import ExecutionSettings
from deep_harness.application.harness_spec import (
    CONTEXT_POLICY,
    HARNESSES,
    SPECS,
    spec_for,
)
from deep_harness.application.session_runtime import SessionRuntime
from deep_harness.domain.models import Harness
from deep_harness.runtime.agent import (
    filesystem_middleware,
    response_format_for,
    subagent_descriptors,
    system_prompt_for,
)
from deep_harness.runtime.backend import ToolGatewayBackend
from deep_harness.runtime.harnesses import HarnessReport


def test_harness_spec_is_the_single_source_of_truth():
    assert HARNESSES == tuple(Harness.__args__)
    assert set(SPECS) == set(HARNESSES)
    assert READ_ONLY_HARNESSES == frozenset(
        key for key in HARNESSES if spec_for(key).read_only
    )
    assert CONTEXT_POLICY == "persisted-v2"
    with pytest.raises(ValueError, match="Unknown harness"):
        spec_for("not-a-harness")


def test_harness_spec_is_frozen():
    spec = spec_for("general")
    with pytest.raises(FrozenInstanceError):
        spec.key = "coding"


def test_manifest_exposes_specs_and_context_policy_without_engine():
    data = manifest()
    assert data["harness_specs"] == {key: SPECS[key].as_dict() for key in HARNESSES}
    assert data["context_policy"] == CONTEXT_POLICY
    assert "engine" not in data
    assert "engines" not in data


def test_derive_grants_no_longer_takes_an_engine():
    assert derive_grants("research") == derive_grants("research", ())
    assert not set(derive_grants("research")) & {"write", "edit", "bash"}


def test_backend_satisfies_deepagents_protocol():
    assert isinstance(ToolGatewayBackend(), BackendProtocol)
    for method in ("aread", "awrite", "aedit", "als", "aglob", "agrep", "adelete"):
        assert callable(getattr(ToolGatewayBackend(), method))


def test_agent_builders_follow_the_harness_spec():
    assert response_format_for("general") is None
    assert response_format_for("coding") is HarnessReport
    assert system_prompt_for("base", "general") == "base"
    assert "base\n\n" in system_prompt_for("base", "research_web")
    # general declares no subagents; the specialised tiers declare fork children.
    assert subagent_descriptors("general") == ()
    for harness in ("coding", "research", "research_web"):
        descriptors = subagent_descriptors(harness)
        assert descriptors
        assert all(desc["mode"] == "fork" for desc in descriptors)
        assert all(desc["response_format"] is HarnessReport for desc in descriptors)


def test_filesystem_middleware_uses_the_boundary_backend():
    from deepagents.middleware import FilesystemMiddleware

    backend = ToolGatewayBackend()
    middleware = filesystem_middleware(backend)
    assert isinstance(middleware, FilesystemMiddleware)
    assert middleware.backend is backend


def test_context_projection_key_is_unified_across_tiers():
    class _Model:
        identity = {"provider": "diagnostic", "name": "guard-test"}
        request_timeout = 1.0

    class _Scope:
        workspace = __import__("pathlib").Path(".")

    runtime = SessionRuntime(object(), _Scope(), _Model(), ExecutionSettings())
    standard = runtime.snapshot(("read",), engine="standard")
    deep = runtime.snapshot(("read",), engine="deep")
    assert standard["context_projection"] == CONTEXT_POLICY
    assert deep["context_projection"] == CONTEXT_POLICY
