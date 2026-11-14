"""Native child-process crash recovery: uncertain effects block queued followers."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError


async def test_killed_worker_releases_owner_but_does_not_replay_external_effect(tmp_path):
    worker = Path(__file__).parent / "fixtures" / "crash_worker.py"
    inherited = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "LOCALAPPDATA",
        "APPDATA",
    )
    environment = {key: os.environ[key] for key in inherited if key in os.environ}
    environment["PYTHONUTF8"] = "1"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        str(worker),
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    competing = ProjectHost(tmp_path / "state", tmp_path)
    try:
        ready = await asyncio.wait_for(process.stdout.readline(), timeout=20)
        if not ready:
            stderr = await process.stderr.read()
            pytest.fail(f"Worker did not initialize: {stderr.decode('utf-8')}")
        session_id, admission_id = ready.decode("utf-8").strip().split()
        with pytest.raises(DomainError, match="already owns"):
            await competing.application("crash")
        process.kill()
        await asyncio.wait_for(process.wait(), timeout=10)
        app = await competing.application("crash")
        await app.wait_idle()
        first = await app.store.get_admission(admission_id, session_id)
        assert first.status == "unknown"
        assert await app.executions.claim(session_id, app.owner) is None
        assert (tmp_path / "external-effect.txt").read_text(encoding="utf-8") == "occurred-once"
        history = await app.store.history(session_id)
        assert [message["content"] for message in history] == ["first", "follower"]
        assert any(
            row["type"] == "execution.unknown" for row in await app.store.event_history(session_id)
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await competing.close()
