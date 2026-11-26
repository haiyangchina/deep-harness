"""Frozen task tiers for tool grants, delegation and reports."""

from dataclasses import asdict, dataclass
from typing import get_args

from deep_harness.domain.models import Harness

CONTEXT_POLICY = "persisted-v2"


@dataclass(frozen=True)
class HarnessSpec:
    key: Harness
    read_only: bool
    subagents: tuple[str, ...]
    report: bool
    context: str = CONTEXT_POLICY

    def as_dict(self) -> dict:
        value = asdict(self)
        value["subagents"] = list(self.subagents)
        return value


SPECS: dict[Harness, HarnessSpec] = {
    "general": HarnessSpec(key="general", read_only=False, subagents=(), report=False),
    "coding": HarnessSpec(
        key="coding", read_only=False, subagents=("research", "coding", "review"), report=True
    ),
    "research": HarnessSpec(key="research", read_only=True, subagents=("research",), report=True),
    "research_web": HarnessSpec(
        key="research_web", read_only=True, subagents=("research",), report=True
    ),
}


HARNESSES: tuple[Harness, ...] = get_args(Harness)


def spec_for(harness: Harness) -> HarnessSpec:
    """Return the frozen spec for a harness, rejecting unknown names."""
    try:
        return SPECS[harness]
    except KeyError as exc:
        raise ValueError(f"Unknown harness: {harness}") from exc


def manifest() -> dict:
    """Serializable harness-tier description consumed by CLI, HTTP and desktop."""
    return {
        "harnesses": list(HARNESSES),
        "context_policy": CONTEXT_POLICY,
        "specs": {key: SPECS[key].as_dict() for key in HARNESSES},
    }
