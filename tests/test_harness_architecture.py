"""Contracts for Harness authorization, runtime builders, and snapshots."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

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
from deep_harness.domain.models import Harness, ProfileScope
from deep_harness.runtime.agent import (
    response_format_for,
    specialist_guide,
    system_prompt_for,
)
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


def test_derive_grants_for_read_only_harness():
    assert derive_grants("research") == derive_grants("research", ())
    assert not set(derive_grants("research")) & {"write", "edit", "bash"}


def test_agent_builders_follow_the_harness_spec():
    assert response_format_for("general") is None
    assert response_format_for("coding") is HarnessReport
    assert system_prompt_for("base", "general") == "base"
    assert "base\n\n" in system_prompt_for("base", "research_web")
    for spec in SPECS.values():
        for kind in spec.subagents:
            assert specialist_guide(kind)


def test_context_projection_key_is_unified_across_tiers():
    class _Model:
        identity = {"provider": "diagnostic", "name": "guard-test"}
        request_timeout = 1.0

    directory = Path(".")
    scope = ProfileScope("guard-test", directory, directory, directory)
    runtime = SessionRuntime(object(), scope, _Model(), ExecutionSettings())
    standard = runtime.snapshot(("read",), engine="standard")
    deep = runtime.snapshot(("read",), engine="deep")
    assert standard["context_projection"] == CONTEXT_POLICY
    assert deep["context_projection"] == CONTEXT_POLICY
