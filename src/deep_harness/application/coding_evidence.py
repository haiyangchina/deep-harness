"""Coding evidence derived from durable tool messages in one admission."""

import json
import re
import tomllib
from pathlib import PurePath

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


def read_receipts(messages):
    calls = {
        call["id"]: call["name"] for message in messages for call in message.get("tool_calls", [])
    }
    for message in messages:
        name = calls.get(message.get("tool_call_id"))
        if message["role"] != "tool" or name not in {"read", "read_files"}:
            continue
        try:
            result = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict):
            continue
        rows = result.get("files", []) if name == "read_files" else [result]
        for row in rows:
            if isinstance(row, dict) and row.get("sha256") and row.get("path"):
                yield message["id"], row


def reading_evidence(messages):
    evidence = {}
    for receipt, row in read_receipts(messages):
        key = (row["path"], row.get("first_line"), row.get("last_line"))
        evidence[key] = {
            "path": row["path"],
            "first_line": key[1],
            "last_line": key[2],
            "sha256": row["sha256"],
            "receipt_id": receipt,
        }
    return list(evidence.values())[-48:]


def project_facts(messages):
    """Extract declarations from the existing evidence, without opening project files."""
    facts = []
    for _, row in read_receipts(messages):
        path = PurePath(row["path"].replace(chr(92), "/"))
        if len(path.parts) != 1 or row.get("first_line", row.get("offset")) != 1:
            continue
        content = row.get("content")
        if not isinstance(content, str):
            continue
        details = []
        if path.name == "langgraph.json":
            try:
                graphs = json.loads(content).get("graphs", {})
            except (ValueError, AttributeError):
                continue
            if isinstance(graphs, dict):
                details.extend(
                    f"LangGraph 入口 {name} → {entry}"
                    for name, entry in list(graphs.items())[:4]
                    if isinstance(entry, str) and len(entry) <= 240
                )
        elif path.name == "pyproject.toml":
            section = re.search(r"(?ms)^\[project\][ \t]*$.*?(?=^\[|\Z)", content)
            if section:
                try:
                    project = tomllib.loads(section[0]).get("project", {})
                except tomllib.TOMLDecodeError:
                    continue
                for key, label in (("name", "项目"), ("requires-python", "Python 要求"),
                                   ("description", "清单说明")):
                    value = project.get(key)
                    if isinstance(value, str):
                        details.append(f"{label} {value[:180]}")
        elif path.name == "package.json":
            try:
                package = json.loads(content)
            except ValueError:
                continue
            if isinstance(package, dict) and isinstance(package.get("name"), str):
                details.append(f"包名 {package['name'][:180]}")
        if details:
            fact = f"- `{row['path']}` 声明了" + "，".join(details)
            if fact not in facts:
                facts.append(fact)
    return facts[:8]


def is_observed_reference(source, reads):
    source = source.strip().replace(chr(92), "/").removeprefix("./")
    match = re.search(r"(?::|#L)([0-9]+)(?:[-:](?:L)?([0-9]+))?$", source)
    path = source[:match.start()] if match else source
    candidates = [row for row in reads if row["path"] == path]
    if not match:
        return bool(candidates)
    start = int(match.group(1))
    end = int(match.group(2) or start)
    return any(
        (row["first_line"] or 1) <= start <= end <= (row["last_line"] or 0)
        for row in candidates
    )
