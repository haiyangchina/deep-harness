"""Cancellable worker for permission-checked file-tree discovery."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from deep_harness.application.tools import ToolGateway
from deep_harness.application.workspace import (
    DirectoryListArgs,
    SearchFilesArgs,
    list_files,
    search_files,
)
from deep_harness.domain.models import DomainError, ProfileScope


def main():
    request = json.load(sys.stdin)
    scope = request["scope"]
    for field in ("workspace", "home", "state_root"):
        scope[field] = Path(scope[field])
    scope["protected_paths"] = tuple(map(Path, scope["protected_paths"]))
    gateway = ToolGateway(
        ProfileScope(**scope), SimpleNamespace(permission_mode=request["permission_mode"]),
        None, None,
    )
    validator, operation = {
        "list_files": (DirectoryListArgs, list_files),
        "search_files": (SearchFilesArgs, search_files),
    }[request["name"]]
    try:
        arguments = validator.model_validate(request["arguments"])
        result = {"value": gateway._read_local(operation, arguments)}
    except (DomainError, OSError, UnicodeError) as exc:
        result = {
            "error": exc.code if isinstance(exc, DomainError) else type(exc).__name__,
            "message": (
                str(exc) if isinstance(exc, DomainError) else "The directory is inaccessible"
            ),
        }
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
