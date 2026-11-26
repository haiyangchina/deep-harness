"""Exact, operator-authorized delivery from an isolated task to its project."""

import base64
import hashlib
import json
import os
import stat

from deep_harness.application.atomic_files import MAX_FILE_BYTES, atomic_write
from deep_harness.application.patches import FilePatch, prepare_file
from deep_harness.application.permissions import sensitive_path
from deep_harness.domain.models import DomainError, TaskWorkspace
from deep_harness.infrastructure.change_bundles import encode_bundle
from deep_harness.infrastructure.coding_workspace import capture, changes_between
from deep_harness.infrastructure.task_workspaces import file_digest
from deep_harness.server_config import number


def read_blob(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise DomainError("delivery_conflict", "A delivery entry is not a regular file")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise DomainError("file_too_large", "A delivery file exceeds 8 MiB")
    return data


class TaskDelivery:
    def __init__(self, manager, task):
        self.manager = manager
        self.task = task
        self.home = manager.home(task.task_id)
        self.record = self.home / "delivery.json"

    def status(self):
        return json.loads(self.record.read_bytes()) if self.record.exists() else None

    def observe(self):
        baseline = json.loads((self.home / "baseline.json").read_bytes())
        final = capture(
            self.manager.scope_for(self.task), include_text=True, previous_paths=baseline["files"]
        )
        return baseline, final

    def preview(self, bundle_store=None, verification=None):
        if not self.task.root.exists():
            return {
                "status": "discarded",
                "files": [],
                "diff": "",
                "diff_truncated": False,
                "delivery": self.status(),
            }
        baseline, final = self.observe()
        result = changes_between(baseline, final)
        if bundle_store and result["status"] == "observed" and result["files"]:
            try:
                identifier, package = self.freeze(verification, observed=(baseline, final))
                bundle_store.save(package)
                result["change_set_id"] = identifier
            except DomainError as exc:
                if exc.code not in {"bundle_invalid", "bundle_limit", "file_too_large"}:
                    raise
                result["bundle_error"] = exc.code
        return {
            **result,
            "revision": final["digest"],
            "mode": self.task.mode,
            "baseline_commit": self.task.baseline_commit,
            "delivery": self.status(),
        }

    def freeze(self, verification=None, *, observed=None):
        baseline, final = observed if observed is not None else self.observe()
        changes = changes_between(baseline, final)
        if changes["status"] != "observed" or not changes["files"]:
            raise DomainError("bundle_invalid", "No complete change set is available")
        if verification and (
            verification.get("before_revision") != final["digest"]
            or verification.get("after_revision") != final["digest"]
        ):
            verification = None
        files = []
        encoded_bytes = 0
        for row in changes["files"]:
            name = row["path"]
            before, after = self.file_info(baseline, name), self.file_info(final, name)
            data = None
            if after:
                path = self.target(self.task.root, name)
                self.check(path, after)
                data = base64.b64encode(read_blob(path)).decode("ascii")
                encoded_bytes += len(data)
                if encoded_bytes > 8 * 1024 * 1024:
                    raise DomainError("bundle_limit", "The change package exceeds 8 MiB")
            files.append({"path": name, "before": before, "after": after, "data": data})
        return encode_bundle(
            {
                "format": 1,
                "revision": final["digest"],
                "baseline_commit": self.task.baseline_commit,
                "files": files,
                "verification": verification,
            }
        )

    def merge_proposal(self):
        """Provide exact patches for the subset representable by the managed patch tool."""
        baseline, final = self.observe()
        change = changes_between(baseline, final)
        files = change["files"]
        if change["status"] != "observed" or len(files) > number("patch.max_files"):
            return None
        proposal = []
        actual_size = 0
        for row in files:
            name = row["path"]
            before = baseline["files"].get(name)
            after = final["files"].get(name)
            if before and after and before["mode"] != after["mode"]:
                return None
            single = changes_between(
                {"files": {name: before} if before else {}, "complete": True},
                {"files": {name: after} if after else {}, "complete": True},
            )
            diff = single["diff"]
            actual_size += len(diff)
            if single["diff_truncated"] or not diff or actual_size > 16_000:
                return None
            patch = FilePatch(path=name, expected_sha256=row["before_sha256"], diff=diff)
            if before is None and after and "\r\n" in after.get("text", ""):
                return None
            # Apply against an unchanged parent copy when possible. Later parent edits are
            # still rejected by the apply_patch tool's normal preflight.
            try:
                parent = self.target(self.task.source, name)
                prepared = prepare_file(parent, patch)
            except (DomainError, OSError):
                return None
            if prepared is not None and hashlib.sha256(prepared).hexdigest() != row["sha256"]:
                return None
            proposal.append(patch.model_dump())
        return proposal

    def command(self, action, revision, bundle=None):
        previous = self.status()
        if action == "reconcile":
            if not previous or previous["revision"] != revision:
                raise DomainError("delivery_conflict", "No delivery matches this revision")
            return self.reconcile(previous)
        if previous and previous["status"] in {"applying", "undoing"}:
            raise DomainError("unknown_effect", "A previous delivery requires reconciliation")
        if action == "discard":
            review = self.preview()
            if review["status"] != "observed" or review["revision"] != revision:
                raise DomainError(
                    "delivery_conflict", "Review the current workspace before discard"
                )
            self.manager.discard(self.task)
            return {"status": "discarded", "revision": revision}
        if action == "undo":
            if previous is None or previous["revision"] != revision:
                raise DomainError("delivery_conflict", "No delivery matches this revision")
            if previous["status"] == "undone":
                return previous
            if previous["status"] != "applied":
                raise DomainError("delivery_conflict", "There is no applied change to undo")
            return self.run(previous, reverse=True)
        if previous is not None and previous["status"] != "not_applied":
            if previous["status"] == "applied" and previous["revision"] == revision:
                if bundle and previous.get("change_set_id") != bundle["id"]:
                    raise DomainError("delivery_conflict", "A different change set was applied")
                return previous
            raise DomainError("delivery_conflict", "This task already has a delivery record")
        baseline, final = self.observe()
        if not (baseline["complete"] and final["complete"]) or final["digest"] != revision:
            raise DomainError("delivery_conflict", "Review a complete, current change set")
        if bundle:
            if bundle["manifest"]["revision"] != revision:
                raise DomainError("delivery_conflict", "The reviewed change set is stale")
            return self.apply_bundle(bundle)
        changes = changes_between(baseline, final)["files"]
        if not changes:
            raise DomainError("delivery_conflict", "The task has no changes to deliver")
        plan = {
            "revision": revision,
            "files": [
                {
                    "path": row["path"],
                    "before": self.file_info(baseline, row["path"]),
                    "after": self.file_info(final, row["path"]),
                }
                for row in changes
            ],
        }
        return self.run(plan, reverse=False)

    def apply_bundle(self, bundle, *, portable=False):
        manifest = bundle["manifest"]
        files = manifest["files"]
        plan = {
            "revision": manifest["revision"],
            "change_set_id": bundle["id"],
            "portable": portable,
            "files": [{key: row[key] for key in ("path", "before", "after")} for row in files],
        }
        contents = {
            row["path"]: base64.b64decode(row["data"]) for row in files if row["data"] is not None
        }
        return self.run(plan, reverse=False, contents=contents)

    def reconcile(self, previous):
        if previous["status"] not in {"applying", "undoing"}:
            return previous
        matches = {}
        for row in previous["files"]:
            path = self.target(self.task.source, row["path"])
            observed = []
            for state in ("before", "after"):
                try:
                    self.check(path, row[state], portable=previous.get("portable", False))
                except DomainError:
                    continue
                observed.append(state)
            matches[row["path"]] = observed
        if all("before" in observed for observed in matches.values()):
            status = "undone" if previous["status"] == "undoing" else "not_applied"
        elif all("after" in observed for observed in matches.values()):
            status = "applied"
        else:
            status = previous["status"]
        result = {**previous, "status": status, "observations": matches}
        atomic_write(self.record, json.dumps(result).encode())
        return result

    @staticmethod
    def file_info(snapshot, name):
        if row := snapshot["files"].get(name):
            return {key: row[key] for key in ("sha256", "mode")}
        return None

    def target(self, root, name):
        path = root / name
        project = self.manager.project
        if (
            not path.is_relative_to(root)
            or path.resolve() != path
            or sensitive_path(path)
            or any(path.is_relative_to(item) for item in project.protected_paths)
            or (
                root == self.task.source
                and not project.task_workspace
                and path.is_relative_to(project.state_root)
            )
        ):
            raise DomainError("delivery_conflict", f"Unsafe delivery target: {name}")
        return path

    @staticmethod
    def check(path, expected, *, portable=False):
        if not path.exists():
            matches = expected is None
        else:
            mode = path.stat().st_mode
            matches = (
                expected is not None
                and stat.S_ISREG(mode)
                and (
                    stat.S_IMODE(mode) == expected["mode"]
                    or (
                        portable
                        and os.name == "nt"
                        and (mode & 0o200) == (expected["mode"] & 0o200)
                    )
                )
                and file_digest(path) == expected["sha256"]
            )
        if not matches:
            raise DomainError("delivery_conflict", f"Workspace file changed: {path.name}")

    def run(self, plan, *, reverse, contents=None):
        operations = []
        for index, row in enumerate(plan["files"]):
            path = self.target(self.task.source, row["path"])
            before, after = row["before"], row["after"]
            expected, desired = (after, before) if reverse else (before, after)
            self.check(path, expected, portable=plan.get("portable", False))
            backup = self.home / "delivery-before" / str(index)
            data = None
            if not reverse and before is not None:
                atomic_write(backup, read_blob(path))
                self.check(backup, {**before, "mode": stat.S_IMODE(backup.stat().st_mode)})
            if desired is not None:
                if contents is not None and not reverse:
                    data = contents[row["path"]]
                else:
                    origin = backup if reverse else self.target(self.task.root, row["path"])
                    if reverse:
                        self.check(origin, {**desired, "mode": stat.S_IMODE(origin.stat().st_mode)})
                    else:
                        self.check(origin, desired)
                    data = read_blob(origin)
                if hashlib.sha256(data).hexdigest() != desired["sha256"]:
                    raise DomainError(
                        "delivery_conflict", "A planned file changed during preflight"
                    )
            operations.append((row["path"], expected, desired, data))

        pending = {**plan, "status": "undoing" if reverse else "applying"}
        atomic_write(self.record, json.dumps(pending).encode())
        try:
            for name, expected, desired, data in operations:
                path = self.target(self.task.source, name)
                self.check(path, expected, portable=plan.get("portable", False))
                if data is None:
                    path.unlink()
                else:
                    atomic_write(path, data)
                    path.chmod(desired["mode"])
            result = {**plan, "status": "undone" if reverse else "applied"}
            atomic_write(self.record, json.dumps(result).encode())
        except (DomainError, OSError) as exc:
            raise DomainError(
                "unknown_effect", "Delivery stopped after dispatch; inspect changes"
            ) from exc
        return result


class ImportedDelivery(TaskDelivery):
    def __init__(self, manager, store, bundle):
        root = manager.project.workspace
        super().__init__(manager, TaskWorkspace(bundle["id"], "in_place", root, root))
        self.home = store.path(bundle["id"]).parent
        self.record = self.home / "delivery.json"
        self.bundle = bundle

    def preview(self, *_):
        manifest = self.bundle["manifest"]
        rows = []
        before_files = {}
        after_files = {}
        for row in manifest["files"]:
            name = row["path"]
            try:
                self.check(self.target(self.task.source, name), row["before"], portable=True)
                conflict = False
            except (DomainError, OSError):
                conflict = True
            rows.append(
                {
                    "path": name,
                    "before": row["before"],
                    "after": row["after"],
                    "conflict": conflict,
                }
            )
            if conflict:
                continue
            if row["before"]:
                before_files[name] = self.diff_entry(
                    row["before"], read_blob(self.target(self.task.source, name))
                )
            if row["after"]:
                after_files[name] = self.diff_entry(row["after"], base64.b64decode(row["data"]))
        changes = changes_between(
            {"files": before_files, "complete": True}, {"files": after_files, "complete": True}
        )
        return {
            "id": self.bundle["id"],
            "revision": manifest["revision"],
            "files": rows,
            "verification": manifest["verification"],
            "delivery": self.status(),
            "diff": changes["diff"],
            "diff_truncated": changes["diff_truncated"] or any(row["conflict"] for row in rows),
        }

    @staticmethod
    def diff_entry(metadata, data):
        entry = dict(metadata)
        if len(data) <= 32768 and b"\0" not in data:
            try:
                entry["text"] = data.decode("utf-8")
            except UnicodeDecodeError:
                pass
        return entry

    def command(self, action, revision, bundle=None):
        if revision != self.bundle["manifest"]["revision"]:
            raise DomainError("delivery_conflict", "A different change set was reviewed")
        if action in {"undo", "reconcile"}:
            return super().command(action, revision)
        previous = self.status()
        if previous and previous["status"] == "applied":
            return previous
        if previous and previous["status"] != "not_applied":
            raise DomainError("unknown_effect", "Review the previous delivery record")
        return self.apply_bundle(self.bundle, portable=True)
