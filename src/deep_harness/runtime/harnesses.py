"""Frozen Deep harness contracts for the general, coding and research modes."""

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError

GUIDES = {
    "general": "",
    "coding": (
        "You lead a coding task. Inspect relevant files before changing them. "
        "Use delegate_task for independent code investigation when granted; synthesize the "
        "specialists' findings yourself. Changes and commands require the granted tools and "
        "human approval. Run focused tests when command execution is granted. "
        "End with a JSON object containing summary, evidence, changes, tests, limitations. "
        "Evidence items have source and observation. Report only actions actually performed. "
        'Example shape: {"summary":"...","evidence":[],"changes":[],"tests":[],"limitations":[]}. '
        "All five fields are required; evidence, changes, tests and limitations must be arrays."
    ),
    "research": (
        "You lead a research task. Gather independent evidence from granted sources and "
        "delegate distinct questions when granted. Compare disagreements and state uncertainty. "
        "Cite exact source paths or MCP result identifiers; never invent sources. "
        "End with a JSON object containing summary, evidence, changes, tests, limitations. "
        "Evidence items have source and observation. Keep changes and tests empty if inapplicable. "
        'Example shape: {"summary":"...","evidence":[],"changes":[],"tests":[],"limitations":[]}. '
        "All five fields are required; evidence, changes, tests and limitations must be arrays."
    ),
}


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=1000)
    observation: str = Field(min_length=1, max_length=4000)


class HarnessReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=8000)
    evidence: list[Evidence] = Field(max_length=32)
    changes: list[str] = Field(max_length=32)
    tests: list[str] = Field(max_length=32)
    limitations: list[str] = Field(max_length=32)


def harness_prompt(base: str, harness: str) -> str:
    guide = GUIDES[harness]
    return f"{base}\n\n{guide}" if guide else base


def harness_contract(harness: str) -> str:
    return fingerprint({"guide": GUIDES[harness], "report": HarnessReport.model_json_schema()})


def parse_report(content: str) -> str:
    if not isinstance(content, str) or len(content) > 64_000:
        raise DomainError("harness_output", "Specialized harness result is not bounded text")
    value = content.strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    try:
        data = json.loads(value)
        if isinstance(data, dict):
            for field in ("changes", "tests", "limitations"):
                if isinstance(data.get(field), str):
                    data[field] = [data[field]] if data[field].strip() else []
        report = HarnessReport.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise DomainError(
            "harness_output", "Specialized harness must return its report schema"
        ) from exc
    return report.model_dump_json()
