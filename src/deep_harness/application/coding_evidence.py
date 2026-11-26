"""Coding evidence derived from durable tool messages in one admission."""

import json

COMMANDS = {"bash"}
FILE_MUTATIONS = {"edit", "write", "apply_patch", "merge_delegations"}


def coding_evidence(messages):
    calls = {}
    files = {}
    commands = []
    last_write = -1
    for ordinal, message in enumerate(messages):
        for call in message.get("tool_calls", []):
            calls[call["id"]] = (ordinal, call)
        receipt = calls.get(message.get("tool_call_id"))
        if receipt is None or message["role"] != "tool":
            continue
        batch, call = receipt
        name = call["name"]
        if name not in COMMANDS | FILE_MUTATIONS:
            continue
        try:
            result = json.loads(message["content"])
        except (TypeError, ValueError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        if name in COMMANDS:
            commands.append(
                {
                    "batch": batch,
                    "receipt_id": message["id"],
                    "tool": name,
                    "arguments": call["args"],
                    "exit_code": result.get("exit_code"),
                }
            )
            continue
        changes = result.get("files", []) if name in {"apply_patch", "merge_delegations"} else [
            result
        ]
        for change in changes:
            if not isinstance(change.get("path"), str) or not change.get("sha256"):
                continue
            files[change["path"]] = {
                "path": change["path"],
                "sha256": change["sha256"],
                "receipt_id": message["id"],
            }
            last_write = max(last_write, batch)
    return list(files.values()), commands, last_write
