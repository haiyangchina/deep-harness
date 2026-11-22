"""Consistent profile archives and atomic restore into an unused profile directory."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from importlib.metadata import version
from pathlib import Path, PurePosixPath

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.database import SCHEMA_VERSION
from deep_harness.server_config import number

MAX_ARCHIVE_BYTES = number("archives.max_bytes")
DATABASES = ("business.db", "checkpoints.db")


def checkpoint_versions():
    return {
        name: version(name)
        for name in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-sqlite")
    }


def snapshot_database(source, target):
    with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(target)) as snapshot:
        original.backup(snapshot)
        if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise DomainError("backup_invalid", "Database integrity check failed")


def create_archive(scope, destination):
    destination = destination.resolve()
    if destination.exists():
        raise DomainError("archive_exists", "Backup destination already exists")
    if destination.is_relative_to(scope.home):
        raise DomainError(
            "archive_path_denied", "Write profile archives outside the profile directory"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deep-harness-backup-") as temporary:
        staging = Path(temporary)
        files = {}
        for name in DATABASES:
            snapshot_database(scope.home / name, staging / name)
            files[name] = staging / name
        for path in scope.home.rglob("*"):
            relative = path.relative_to(scope.home)
            if path.is_symlink():
                raise DomainError("archive_symlink", "Profile archives refuse symbolic links")
            if not path.is_file() or relative.parts[0] == "backups":
                continue
            if (
                path.name == "owner.lock"
                or path.name.endswith((".db-wal", ".db-shm"))
                or relative.as_posix() in DATABASES
            ):
                continue
            if relative.as_posix() == "manifest.json":
                raise DomainError(
                    "archive_reserved_path", "manifest.json is reserved by the archive format"
                )
            if path.suffix == ".db":
                snapshot = staging / relative
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot_database(path, snapshot)
                files[relative.as_posix()] = snapshot
                continue
            files[relative.as_posix()] = path
        if (
            len(files) > number("archives.max_files")
            or sum(path.stat().st_size for path in files.values()) > MAX_ARCHIVE_BYTES
        ):
            raise DomainError("archive_too_large", "Profile archive exceeds local backup limits")
        manifest = {
            "format": 1,
            "profile_id": scope.profile_id,
            "workspace": str(scope.workspace),
            "checkpoint_versions": checkpoint_versions(),
            "files": {
                name: {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                }
                for name, path in files.items()
            },
        }
        descriptor, partial_name = tempfile.mkstemp(
            prefix=".deep-harness-backup-", dir=destination.parent
        )
        os.close(descriptor)
        partial = Path(partial_name)
        try:
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
                for name, path in files.items():
                    archive.write(path, name)
            # A hard link provides exclusive publication without replacing an existing backup.
            os.link(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    return {"path": str(destination), "files": len(files), "manifest": manifest}


def validate_manifest(archive, scope):
    entries = archive.infolist()
    if (
        len(entries) > number("archives.max_files") + 1
        or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES
    ):
        raise DomainError("archive_too_large", "Archive exceeds extraction limits")
    names = [entry.filename for entry in entries]
    if len(set(names)) != len(names) or "manifest.json" not in names:
        raise DomainError("archive_invalid", "Archive has duplicate entries or no manifest")
    try:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["format"] != 1 or not isinstance(manifest["files"], dict):
            raise ValueError("Unsupported manifest")
        files = manifest["files"]
        if set(names) != {"manifest.json", *files} or not set(DATABASES).issubset(files):
            raise ValueError("Archive entries do not match manifest")
        if manifest["checkpoint_versions"] != checkpoint_versions():
            raise DomainError(
                "checkpoint_version_mismatch",
                "Restore requires the recorded checkpoint SDK versions",
            )
        if Path(manifest["workspace"]) != scope.workspace:
            raise DomainError(
                "workspace_mismatch",
                "Restore with the original workspace; fork to change execution configuration",
            )
        for name in files:
            path = PurePosixPath(name)
            if (
                not path.parts
                or path.as_posix() != name
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or ":" in name
                or any(part.rstrip(" .") != part for part in path.parts)
            ):
                raise ValueError("Unsafe archive path")
    except (ValueError, KeyError, TypeError) as exc:
        raise DomainError("archive_invalid", "Archive manifest is invalid") from exc
    return manifest


def restore_archive(scope, source):
    if scope.home.exists():
        raise DomainError("profile_exists", "Restore requires a new, unused profile identifier")
    scope.home.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=scope.home.parent))
    try:
        with zipfile.ZipFile(source) as archive:
            manifest = validate_manifest(archive, scope)
            for name, expected in manifest["files"].items():
                data = archive.read(name)
                if (
                    len(data) != expected["size"]
                    or hashlib.sha256(data).hexdigest() != expected["sha256"]
                ):
                    raise DomainError(
                        "archive_corrupt", "Archive content does not match its integrity manifest"
                    )
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        for name in DATABASES:
            with closing(sqlite3.connect(staging / name)) as conn:
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise DomainError(
                        "archive_corrupt", "Restored database failed integrity validation"
                    )
                if name == "business.db":
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if version != SCHEMA_VERSION:
                        raise DomainError("schema_unsupported", "Archive schema is unsupported")
        rebind_profile(staging / "business.db", manifest["profile_id"], scope.profile_id)
        os.replace(staging, scope.home)
        return {
            "profile_id": scope.profile_id,
            "source_profile_id": manifest["profile_id"],
            "status": "restored",
        }
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError) as exc:
        raise DomainError("archive_invalid", "Archive is invalid") from exc
    finally:
        if staging.exists():
            if (
                staging.resolve().parent != scope.home.parent.resolve()
                or not staging.name.startswith(".restore-")
            ):
                raise DomainError(
                    "cleanup_path_denied", "Restore staging path is outside the profile root"
                )
            shutil.rmtree(staging)


def rebind_profile(database, original, target):
    with closing(sqlite3.connect(database)) as conn:
        profiles = {row[0] for row in conn.execute("SELECT DISTINCT profile_id FROM sessions")}
        if profiles - {original}:
            raise DomainError("archive_invalid", "Business database contains another profile")
        conn.execute("UPDATE sessions SET profile_id=? WHERE profile_id=?", (target, original))
        conn.execute(
            "UPDATE profile_bindings SET profile_id=? WHERE profile_id=?", (target, original)
        )
        conn.commit()
