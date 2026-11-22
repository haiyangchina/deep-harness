"""The single agent-graph builder for the unified execution kernel.

There is exactly one graph in deep-harness: a deepagents ``create_deep_agent``
graph assembled here. Harness tiers (general/coding/research/research_web) are
configuration profiles supplied by ``HarnessSpec`` -- they change prompts,
reports and delegation availability, never the underlying loop. This is what
removes the old user-facing engine choice (standard vs deep).

The four contracts live in ``HarnessGuardMiddleware`` (approvals, receipts,
fencing, recovery) and in the guarded tools produced by the runtime; the
ledger layer is untouched.
"""

from deepagents.middleware import FilesystemMiddleware
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)

from deep_harness.application.harness_spec import spec_for
from deep_harness.runtime.harnesses import HarnessReport, harness_prompt


def register_profile(provider: str, model_name: str) -> None:
    """Register the harness profile once per model identity.

    deepagents' native subagents, memory and summarisation are intentionally
    disabled: the project's own ledger-backed delegation and guard own those
    concerns. Keeping native capabilities off is what lets the same graph honour
    approval / receipt / generation-fencing / crash-recovery contracts.
    """
    register_harness_profile(
        f"{provider}:{model_name}",
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            excluded_tools=frozenset({"read_file"}),
            excluded_middleware=frozenset({"SummarizationMiddleware"}),
        ),
    )


def response_format_for(harness: str):
    """Specialised harnesses finish with a JSON HarnessReport; general does not."""
    return HarnessReport if spec_for(harness).report else None


def system_prompt_for(base_prompt: str, harness: str) -> str:
    return harness_prompt(base_prompt, harness)


def filesystem_middleware(backend) -> FilesystemMiddleware:
    """The virtual read_file middleware, kept on the boundary-aware backend."""
    return FilesystemMiddleware(backend=backend, tools=["read_file"])


# Specialist descriptors are declared by HarnessSpec, not duplicated across the
# runtime. ``mode="fork"`` is the contract-critical choice: fork children inherit
# this graph's guard middleware (approval / receipts / fencing / recovery).
# The ledger-backed delegation path in runtime/deep.py consumes these descriptors
# to create durable child sessions with their own checkpoints, so background
# work, steering and exactly-once effect accounting remain intact.
SUBAGENT_GUIDES: dict[str, str] = {
    "research": (
        "Research this bounded question using only the granted sources. "
        "State uncertainty clearly and cite paths or source identifiers."
    ),
    "coding": (
        "Complete this bounded coding task using only granted tools. "
        "Inspect before editing, request approval for changes and commands, "
        "and report actual changes and test results."
    ),
}


def subagent_descriptors(harness: str) -> tuple[dict, ...]:
    """Fork-mode specialist descriptors authorised for a harness tier."""
    descriptors = []
    for kind in spec_for(harness).subagents:
        descriptors.append(
            {
                "name": kind,
                "description": SUBAGENT_GUIDES[kind],
                "system_prompt": SUBAGENT_GUIDES[kind],
                "mode": "fork",
                "response_format": HarnessReport,
            }
        )
    return tuple(descriptors)


def specialist_guide(kind: str) -> str:
    """The frozen child prompt for a delegation kind declared in HarnessSpec."""
    return SUBAGENT_GUIDES[kind]
