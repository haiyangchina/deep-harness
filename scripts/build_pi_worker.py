"""Rebuild the shipped search worker from its npm lock inside the project's Conda env."""

import shutil
import subprocess
import sys
from pathlib import Path


def main():
    prefix = Path(sys.prefix)
    if prefix.name != "deep-harness" or not (prefix / "conda-meta").is_dir():
        raise SystemExit("Run with the deep-harness Conda interpreter")
    source = Path(__file__).resolve().parents[1] / "web-worker"
    staging = prefix / "share" / "deep-harness-pi-build"
    staging.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(source / name, staging / name)
    subprocess.run(
        [shutil.which("npm.cmd") or "npm", "ci", "--no-audit", "--no-fund"],
        cwd=staging,
        check=True,
    )
    subprocess.run(["node", source / "build.mjs", staging], cwd=source, check=True)


if __name__ == "__main__":
    main()
