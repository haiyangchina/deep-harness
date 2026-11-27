"""Perform workspace construction in a process the backend can stop."""

import json
import sys
from pathlib import Path

from deep_harness.domain.models import DomainError, ProfileScope
from deep_harness.infrastructure.task_workspaces import TaskWorkspaces


def main():
    request = json.load(sys.stdin)
    scope = request["scope"]
    for field in ("workspace", "home", "state_root"):
        scope[field] = Path(scope[field])
    scope["protected_paths"] = tuple(map(Path, scope["protected_paths"]))
    workspaces = TaskWorkspaces(ProfileScope(**scope))
    phase = request["phase"]
    try:
        task = request["task_id"]
        mode = request["mode"]
        if phase == "build":
            workspaces.build(task, mode, request["attempt"])
        else:
            workspaces.baseline(task, mode)
        result = {"phase": phase}
    except DomainError as exc:
        result = {"error": exc.code, "message": str(exc)}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
