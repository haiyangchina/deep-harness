"""Saved local project folders for one Web instance."""

import json
import os
from pathlib import Path

from deep_harness.application.atomic_files import atomic_write
from deep_harness.application.permissions import sensitive_path
from deep_harness.domain.models import DomainError


class LocalProjects:
    def __init__(self, state_dir, protected_paths):
        self.file = state_dir / "projects.json"
        self.protected_paths = (state_dir, *protected_paths)

    def load(self):
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def save(self, projects):
        atomic_write(self.file, json.dumps(projects, ensure_ascii=False).encode("utf-8"))

    def directory(self, raw):
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise DomainError("project_directory", "请输入绝对目录路径。")
        try:
            target = path.resolve(strict=True)
            allowed = self.allowed(target) and target.is_dir()
        except (OSError, RuntimeError, ValueError):
            allowed = False
        if not allowed:
            raise DomainError("project_directory", "目录不存在或不可作为项目使用。")
        return target

    def allowed(self, target):
        return not sensitive_path(target) and not any(
            target.is_relative_to(path) for path in self.protected_paths
        )

    def browse(self, raw, initial):
        root = self.directory(raw or str(initial))
        try:
            with os.scandir(root) as entries:
                directories = sorted(
                    (entry.name for entry in entries
                     if entry.is_dir(follow_symlinks=False)
                     and not Path(entry.path).is_junction()
                     and self.allowed(Path(entry.path))),
                    key=str.casefold,
                )
        except OSError as exc:
            raise DomainError("project_directory", "无法列出此目录。") from exc
        return {
            "path": str(root),
            "parent": str(root.parent) if root != root.parent else None,
            "directories": [
                {"name": name, "path": str(root / name)} for name in directories[:300]
            ],
            "truncated": len(directories) > 300,
            "drives": os.listdrives() if os.name == "nt" else [],
        }
