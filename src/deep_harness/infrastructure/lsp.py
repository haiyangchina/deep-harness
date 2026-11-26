"""Bounded stdio LSP requests to operator-configured language servers."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from deep_harness.application.permissions import sensitive_path
from deep_harness.application.process_environment import command_environment
from deep_harness.application.shell import stop_tree
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.windows_job import WindowsJob

OPERATIONS = {
    "definition": "textDocument/definition",
    "references": "textDocument/references",
    "diagnostics": "textDocument/diagnostic",
    "symbols": "workspace/symbol",
}
LANGUAGES = {".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescriptreact"}
MAX_MESSAGE = 2 * 1024 * 1024


def configured_server(language):
    try:
        config = json.loads(os.environ.get("DEEP_HARNESS_LANGUAGE_SERVERS", "{}"))
        argv = config.get(language.removesuffix("react"))
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(part, str) and part and "\0" not in part for part in argv)
        ):
            return None
        executable = shutil.which(argv[0])
        return [str(Path(executable).resolve()), *argv[1:]] if executable else None
    except (ValueError, TypeError, AttributeError):
        return None


class Stdio:
    def __init__(self, process):
        self.process = process

    async def send(self, value):
        payload = json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False).encode()
        self.process.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        await self.process.stdin.drain()

    async def receive(self):
        size = None
        while (line := await self.process.stdout.readline()) != b"\r\n":
            if not line:
                raise EOFError
            if line.lower().startswith(b"content-length:"):
                size = int(line.split(b":", 1)[1])
        if not size or not 1 <= size <= MAX_MESSAGE:
            raise ValueError("Invalid language server frame")
        response = json.loads(await self.process.stdout.readexactly(size))
        if not isinstance(response, dict):
            raise ValueError("Invalid language server response")
        if response.get("method") and "id" in response:
            items = (response.get("params") or {}).get("items", [])
            configuration = response["method"] == "workspace/configuration"
            await self.send(
                {"id": response["id"], "result": [None] * len(items) if configuration else None}
            )
        return response

    async def request(self, identifier, method, params):
        await self.send({"id": identifier, "method": method, "params": params})
        for _ in range(128):
            response = await self.receive()
            if not response.get("method") and response.get("id") == identifier:
                if "error" in response:
                    raise ValueError("Language server declined the operation")
                return response.get("result")
        raise ValueError("Language server produced too many frames")

    async def published_diagnostics(self, uri):
        for _ in range(128):
            response = await self.receive()
            if response.get("method") == "textDocument/publishDiagnostics":
                payload = response.get("params") or {}
                if payload.get("uri") == uri:
                    return payload.get("diagnostics", [])
        raise ValueError("Language server produced too many frames")


def permitted_location(root, uri, candidate):
    if not isinstance(uri, str):
        return None
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        return None
    path = Path(url2pathname(parsed.path)).resolve()
    if not path.is_relative_to(root) or sensitive_path(path):
        return None
    name = path.relative_to(root).as_posix()
    return name if candidate(name) == path else None


def locations(response, root, current, candidate, operation):
    if isinstance(response, dict):
        values = response.get("items", []) if operation == "diagnostics" else [response]
    else:
        values = response if isinstance(response, list) else []
    results = []
    for row in values[:300]:
        if not isinstance(row, dict):
            continue
        location = row.get("location", row)
        if not isinstance(location, dict):
            continue
        uri = location.get("uri") or location.get("targetUri")
        try:
            name = permitted_location(root, uri or current.as_uri(), candidate)
        except (DomainError, OSError, ValueError):
            continue
        if name is not None:
            observation = {"path": name}
            span = location.get("targetSelectionRange") or location.get("range")
            if isinstance(span, dict):
                start = span.get("start")
                if isinstance(start, dict) and type(start.get("line")) is int:
                    observation["line"] = start["line"] + 1
                    observation["character"] = start.get("character", 0)
            for key in ("name", "kind", "message", "severity"):
                if key in row and isinstance(row[key], (str, int)):
                    observation[key] = row[key][:500] if isinstance(row[key], str) else row[key]
            results.append(observation)
    return results


async def navigate(root, current, arguments, candidate):
    language = LANGUAGES.get(current.suffix.lower())
    command = configured_server(language) if language else None
    if not command:
        return None
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = None
    job = None
    try:
        data = await asyncio.to_thread(read_document, current)
        if len(data) > 262144:
            return None
        async with asyncio.timeout(12):
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=root,
                env=command_environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **options,
            )
            if os.name == "nt":
                job = WindowsJob(process.pid)
            stdio = Stdio(process)
            initialization = await stdio.request(
                1,
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": root.as_uri(),
                    "capabilities": {},
                    "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                },
            )
            await stdio.send({"method": "initialized", "params": {}})
            document = {"uri": current.as_uri()}
            await stdio.send(
                {
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            **document,
                            "languageId": language,
                            "version": 1,
                            "text": data.decode("utf-8"),
                        },
                    },
                }
            )
            params = {"textDocument": document}
            if arguments.operation == "symbols":
                params = {"query": arguments.query}
            elif arguments.operation in {"references", "definition"}:
                params["position"] = {"line": arguments.line - 1, "character": arguments.character}
                if arguments.operation == "references":
                    params["context"] = {"includeDeclaration": True}
            capabilities = (initialization or {}).get("capabilities", {})
            if arguments.operation == "diagnostics" and not capabilities.get("diagnosticProvider"):
                response = await stdio.published_diagnostics(document["uri"])
            else:
                response = await stdio.request(2, OPERATIONS[arguments.operation], params)
            if await asyncio.to_thread(read_document, current) != data:
                return None
            return locations(response, root, current, candidate, arguments.operation)
    except (OSError, EOFError, ValueError, TypeError, TimeoutError, UnicodeError):
        return None
    finally:
        if job:
            job.close()
            await process.wait()
        elif process:
            await stop_tree(process)


def read_document(path):
    with path.open("rb") as source:
        return source.read(262145)
