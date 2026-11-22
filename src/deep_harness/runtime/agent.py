"""Deep Agents registration and prompts for the active runtimes."""

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


def specialist_guide(kind: str) -> str:
    """The frozen child prompt for a delegation kind declared in HarnessSpec."""
    return SUBAGENT_GUIDES[kind]
