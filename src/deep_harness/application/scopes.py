"""Profile routing never consults mutable process environment."""

import re
from pathlib import Path

from deep_harness.domain.models import DomainError, ProfileScope

_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class ProfileResolver:
    def __init__(self, state_dir: Path, workspace: Path):
        self.state_dir = state_dir.resolve()
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be a directory")

    def resolve(self, profile_id: str) -> ProfileScope:
        if not _PROFILE.fullmatch(profile_id):
            raise DomainError("invalid_profile", "Profile must be a simple identifier")
        home = self.state_dir / "profiles" / profile_id
        if not home.resolve().is_relative_to(self.state_dir):
            raise DomainError("invalid_profile", "Profile path escapes state directory")
        return ProfileScope(profile_id, home, self.workspace, self.state_dir)
