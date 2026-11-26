"""Bounded and content-addressed change packages. Importing only saves data."""

import base64
import binascii
import hashlib
import json
import re
from pathlib import PurePosixPath, PureWindowsPath

from deep_harness.application.atomic_files import atomic_write, read_bytes
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError

MAX_BUNDLE_BYTES = 8 * 1024 * 1024
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def safe_name(name):
    if not isinstance(name, str) or not name or len(name) > 2048:
        return False
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and path.as_posix() == name
        and all(
            part not in {".", ".."}
            and not part.endswith((" ", "."))
            and not PureWindowsPath(part).is_reserved()
            and not re.search(r'[\\:*?"<>|\x00-\x1f]', part)
            for part in name.split("/")
        )
    )


def metadata(value):
    return value is None or (
        isinstance(value, dict)
        and set(value) == {"sha256", "mode"}
        and isinstance(value["sha256"], str)
        and DIGEST.fullmatch(value["sha256"])
        and type(value["mode"]) is int
        and 0 <= value["mode"] <= 0o777
    )


def valid_verification(value, revision):
    if value is None:
        return True
    return (
        isinstance(value, dict)
        and set(value) == {
            "id", "command", "cwd", "exit_code", "before_revision", "after_revision",
        }
        and all(
            isinstance(value[key], str) and 0 < len(value[key]) <= 4096
            for key in ("id", "command", "cwd")
        )
        and type(value["exit_code"]) is int
        and value["before_revision"] == value["after_revision"] == revision
    )


def load_bundle(raw):
    if len(raw) > MAX_BUNDLE_BYTES:
        raise DomainError("bundle_limit", "The change package exceeds 8 MiB")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {"id", "manifest"}:
            raise ValueError
        manifest = body["manifest"]
        if not isinstance(manifest, dict) or set(manifest) != {
            "format",
            "revision",
            "baseline_commit",
            "files",
            "verification",
        }:
            raise ValueError
        if manifest["format"] != 1 or not DIGEST.fullmatch(manifest["revision"]):
            raise ValueError
        commit = manifest["baseline_commit"]
        if commit is not None and not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
            raise ValueError
        if body["id"] != fingerprint(manifest) or not isinstance(manifest["files"], list):
            raise ValueError
        if not 1 <= len(manifest["files"]) <= 5000:
            raise ValueError
        if not valid_verification(manifest["verification"], manifest["revision"]):
            raise ValueError
        names = set()
        for row in manifest["files"]:
            if not isinstance(row, dict) or set(row) != {"path", "before", "after", "data"}:
                raise ValueError
            if not safe_name(row["path"]) or row["path"].casefold() in names:
                raise ValueError
            names.add(row["path"].casefold())
            before, after = row["before"], row["after"]
            if not metadata(before) or not metadata(after) or before == after:
                raise ValueError
            if after is None:
                if row["data"] is not None:
                    raise ValueError
            elif (
                hashlib.sha256(base64.b64decode(row["data"], validate=True)).hexdigest()
                != (after["sha256"])
            ):
                raise ValueError
        if any(
            parent.as_posix() in names for name in names for parent in PurePosixPath(name).parents
        ):
            raise ValueError
        return body
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, RecursionError) as exc:
        raise DomainError("bundle_invalid", "Invalid change package or content digest") from exc


def encode_bundle(manifest):
    body = {"id": fingerprint(manifest), "manifest": manifest}
    raw = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode()
    load_bundle(raw)
    return body["id"], raw


class BundleStore:
    def __init__(self, directory):
        self.directory = directory

    def path(self, identifier):
        if not DIGEST.fullmatch(identifier):
            raise DomainError("bundle_not_found", "Unknown change package")
        return self.directory / identifier[:32] / "bundle.json"

    def save(self, raw):
        bundle = load_bundle(raw)
        target = self.path(bundle["id"])
        if target.exists():
            if self.get(bundle["id"]) != bundle:
                raise DomainError(
                    "bundle_invalid", "A stored change package has a different digest"
                )
        else:
            atomic_write(target, raw)
        return bundle

    def get(self, identifier):
        try:
            bundle = load_bundle(read_bytes(self.path(identifier)))
        except FileNotFoundError as exc:
            raise DomainError("bundle_not_found", "Unknown change package") from exc
        if bundle["id"] != identifier:
            raise DomainError("bundle_invalid", "Stored bundle identity has changed")
        return bundle
