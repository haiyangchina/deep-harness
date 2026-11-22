"""Deterministic workflow steps use the same graph, approvals and effect receipts."""

import json

from jsonschema import Draft202012Validator

from deep_harness.server_config import number


def structured(value):
    value = value.strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    return json.loads(value)


def report(status, summary, output=None):
    serialized = json.dumps(output, ensure_ascii=False)
    if len(serialized) > number("stage_report.max_chars") // 2:
        status = "incomplete"
        summary = "Step output exceeds the structured handoff limit"
        serialized = ""
    return {
        "role": "assistant",
        "content": json.dumps(
            {
                "status": status,
                "summary": summary,
                "artifacts": [serialized] if serialized else [],
                "next_step": None,
            },
            ensure_ascii=False,
        ),
    }


class WorkflowModel:
    request_timeout = 10

    def __init__(self, step):
        self.step = step
        self.identity = {"provider": "workflow", "name": step["kind"]}

    async def complete(self, prompt, messages, tools):
        tail = messages[-1]
        try:
            value = structured(tail["content"])
        except (AttributeError, ValueError):
            return report("incomplete", "Step did not receive a valid structured result")
        if self.step["kind"] == "verify":
            if not Draft202012Validator(self.step["schema"]).is_valid(value):
                return report("incomplete", "Step output failed the configured JSON Schema")
            return report("complete", "Output passed the configured JSON Schema", value)
        if tail["role"] == "tool":
            return report("complete", "Tool returned a confirmed result", value)
        if not isinstance(value, dict):
            return report("incomplete", "Tool input must be a JSON object")
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "workflow-tool", "name": self.step["tools"][0], "args": value}],
        }
