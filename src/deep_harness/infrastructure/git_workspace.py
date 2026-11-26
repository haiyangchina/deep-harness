"""Read the tracked and non-ignored path set without modifying Git state."""

import os
import subprocess
from pathlib import Path
from tempfile import TemporaryFile

from deep_harness.application.process_environment import command_environment


def visible_git_paths(root, *, boundary=None):
    try:
        if boundary is not None:
            top = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                cwd=root, env=command_environment(), capture_output=True, timeout=5, check=True,
            )
            repository = Path(os.fsdecode(top.stdout.rstrip(b"\r\n"))).resolve()
            if not repository.is_relative_to(boundary):
                return None
        with TemporaryFile() as output:
            subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                 "--exclude-standard"],
                cwd=root,
                env=command_environment(),
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=True,
            )
            output.seek(0)
            paths = output.read(4 * 1024 * 1024 + 1)
    except (OSError, subprocess.SubprocessError):
        return None
    if len(paths) > 4 * 1024 * 1024:
        return None
    return {root / os.fsdecode(name) for name in paths.split(b"\0") if name}
