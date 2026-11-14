"""OS runtime context reaches commands while provider secrets remain private."""

import json
import os
import re
import shlex
import shutil
import sys

import pytest

from deep_harness.application.shell import BashArgs, bash
from deep_harness.application.terminal import CommandArgs, run_command


@pytest.mark.parametrize("runner", ["shell", "native"])
async def test_commands_keep_os_locations_and_timezone_without_provider_secrets(
    tmp_path, monkeypatch, runner
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-must-not-reach-command")
    keys = ["HOME", "USERPROFILE", "APPDATA", "TZ", "OPENAI_API_KEY", "Harness_HOME"]
    code = f"import os,json; print(json.dumps({{k:os.environ.get(k) for k in {keys!r}}}))"
    if runner == "native":
        result = await run_command(
            CommandArgs(argv=[sys.executable, "-c", code]), lambda p: tmp_path / p
        )
    else:
        if os.name == "nt":
            executable = sys.executable.replace("'", "''")
            command = f"& '{executable}' -c '{code.replace(chr(39), chr(39) * 2)}'"
        else:
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        result = await bash(BashArgs(command=command), lambda p: tmp_path / p)
    assert result["exit_code"] == 0, result["stderr"]
    inherited = json.loads(result["stdout"])
    for key in ("HOME", "USERPROFILE", "APPDATA", "TZ"):
        assert inherited[key] == os.environ.get(key)
    assert inherited["OPENAI_API_KEY"] is None
    assert inherited["Harness_HOME"] is None


@pytest.mark.platforms("windows")
async def test_windows_gnu_date_returns_within_the_requested_tool_timeout(tmp_path):
    if not shutil.which("date"):
        pytest.skip("Requires the GNU date executable shipped with Git for Windows")
    result = await bash(
        BashArgs(command='date +"%Y-%m-%d %H:%M:%S"', timeout_seconds=5), lambda p: tmp_path / p
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*", result["stdout"])
