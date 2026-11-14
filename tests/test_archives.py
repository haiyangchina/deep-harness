"""Consistent business/checkpoint restore, integrity failures and restore ownership."""

import json
import zipfile

import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError


async def test_backup_restores_pending_approval_with_checkpoint_to_new_profile(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("original")
        session = await app.create_session("create", "Frozen", ("write_file",))
        admission = await app.submit(session.id, "send", "/write restored.txt approved")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        archive = tmp_path / "profile.zip"
        result = await app.backup(archive)
        assert result["files"] >= 2
        await host.restore("restored", archive)
        restored = await host.application("restored")
        await restored.approve(session.id, f"{pending.id}:write-1", pending.generation, "approve")
        await restored.wait_idle()
        assert (tmp_path / "restored.txt").read_text(encoding="utf-8") == "approved"
        assert (await restored.store.get_admission(pending.id, session.id)).status == "completed"
        assert (await app.store.get_admission(pending.id, session.id)).status == "waiting_approval"
        with pytest.raises(DomainError, match="already owns"):
            await host.restore("restored", archive)
        await restored.close()
        with pytest.raises(DomainError, match="unused profile"):
            await host.restore("restored", archive)
    finally:
        await host.close()


async def test_corrupt_archive_never_publishes_partial_profile(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("source")
        archive = tmp_path / "original.zip"
        await app.backup(archive)
        corrupt = tmp_path / "corrupt.zip"
        with zipfile.ZipFile(archive) as original, zipfile.ZipFile(corrupt, "w") as target:
            for name in original.namelist():
                target.writestr(name, b"tampered" if name == "business.db" else original.read(name))
        with pytest.raises(DomainError, match="integrity manifest"):
            await host.restore("failed", corrupt)
        assert not (tmp_path / "state" / "profiles" / "failed").exists()
        await host.restore("failed", archive)
        assert (await host.application("failed")).scope.profile_id == "failed"
    finally:
        await host.close()


async def test_archive_traversal_and_sdk_mismatch_are_rejected(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("source")
        original_path = tmp_path / "original.zip"
        await app.backup(original_path)
        for kind in ("traversal", "sdk"):
            target_path = tmp_path / f"{kind}.zip"
            with (
                zipfile.ZipFile(original_path) as original,
                zipfile.ZipFile(target_path, "w") as target,
            ):
                manifest = json.loads(original.read("manifest.json"))
                if kind == "traversal":
                    manifest["files"]["../escape"] = {"size": 4, "sha256": "invalid"}
                    target.writestr("../escape", b"evil")
                else:
                    manifest["checkpoint_versions"]["langgraph"] = "unsupported"
                target.writestr("manifest.json", json.dumps(manifest))
                for name in original.namelist():
                    if name != "manifest.json":
                        target.writestr(name, original.read(name))
            with pytest.raises(DomainError):
                await host.restore(kind, target_path)
            assert not (tmp_path / "state" / "profiles" / kind).exists()
        assert not (tmp_path / "state" / "escape").exists()
    finally:
        await host.close()
