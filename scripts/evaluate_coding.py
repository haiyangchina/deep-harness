"""Run the repeatable coding workflow evaluation through the repository test runner."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docker", action="store_true", help="Include installed Docker smoke cases"
    )
    parser.add_argument("--bash", help="Absolute path to Bash or Git Bash")
    options = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    bash = options.bash
    if not bash and os.name == "nt" and (git := shutil.which("git")):
        candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
        if candidate.is_file():
            bash = str(candidate)
    bash = bash or shutil.which("bash")
    if not bash:
        parser.error("Bash is required to run scripts/run_tests.sh")
    env = {**os.environ, "Harness_PYTHON": sys.executable}
    env["HARNESS_DOCKER_TEST"] = "1" if options.docker else "0"
    with TemporaryDirectory(prefix="deep-harness-eval-") as temporary:
        report = Path(temporary) / "report.xml"
        command = [
            bash,
            "scripts/run_tests.sh",
            "-o",
            "junit_family=legacy",
            "--junitxml",
            report.as_posix(),
            "tests/test_task_workspaces.py",
            "tests/test_change_bundles_and_navigation.py",
            "tests/test_web_instance.py",
        ]
        result = subprocess.run(command, cwd=repository, env=env, stdout=sys.stderr, check=False)
        if not report.exists():
            return result.returncode or 1
        cases = []
        for case in ET.parse(report).iter("testcase"):
            outcome = next(
                (name for name in ("failure", "error", "skipped") if case.find(name) is not None),
                "passed",
            )
            cases.append(
                {
                    "case": case.get("name"),
                    "outcome": outcome,
                    "seconds": float(case.get("time", 0)),
                    "metrics": {
                        row.get("name"): row.get("value")
                        for row in case.findall("./properties/property")
                    },
                }
            )
        print(json.dumps({"cases": cases}, ensure_ascii=False, indent=2))
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
