"""Install requested development dependencies only into the dedicated Conda environment."""

import argparse
import json
import subprocess
import sys
from importlib.metadata import distributions
from pathlib import Path

import tomlkit
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT = Path(__file__).resolve().parents[1]


def require_environment():
    prefix = Path(sys.prefix)
    if prefix.name != "deep-harness" or not (prefix / "conda-meta").is_dir():
        raise SystemExit(
            "Use the deep-harness Conda interpreter; base/legacy environments are refused"
        )


def freeze():
    packages = {}
    for dist in distributions():
        name = canonicalize_name(dist.metadata["Name"])
        if name != "deep-harness":
            packages[name] = dist.version
    lines = ["# Exact installed Python dependencies for the validated development environment."]
    lines.extend(f"{name}=={value}" for name, value in sorted(packages.items()))
    (PROJECT / "requirements.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")
    evidence = {"python": sys.version.split()[0], "packages": dict(sorted(packages.items()))}
    (PROJECT / "environment-evidence.json").write_text(
        json.dumps(evidence, indent=2) + "\n",
        encoding="utf-8",
    )


def add(specification: str):
    requirement = Requirement(specification)
    if requirement.url or not requirement.specifier:
        raise SystemExit("Supply a named, bounded package requirement; URLs are not accepted")
    operators = {item.operator for item in requirement.specifier}
    if not operators.intersection({"==", "<", "<=", "~="}):
        raise SystemExit("A version upper bound or exact pin is required")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--timeout",
            "30",
            "--retries",
            "2",
            str(requirement),
        ],
        check=True,
        timeout=900,
    )
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True, timeout=60)
    path = PROJECT / "pyproject.toml"
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    deps = document["project"]["dependencies"]
    normalized = canonicalize_name(requirement.name)
    matching = [
        index
        for index, value in enumerate(deps)
        if canonicalize_name(Requirement(value).name) == normalized
    ]
    if matching:
        deps[matching[0]] = str(requirement)
    else:
        deps.append(str(requirement))
    temporary = path.with_suffix(".toml.pending")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.replace(path)
    freeze()


def main():
    require_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("freeze")
    install = commands.add_parser("add")
    install.add_argument("requirement")
    args = parser.parse_args()
    if args.command == "add":
        add(args.requirement)
    else:
        freeze()


if __name__ == "__main__":
    main()
