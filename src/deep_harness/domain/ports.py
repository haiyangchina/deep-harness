"""Execution and model ports do not expose framework message classes."""

from typing import Protocol

from deep_harness.domain.models import Session


class ModelPort(Protocol):
    request_timeout: float
    identity: dict

    async def complete(self, prompt: str, messages: list[dict], tools: tuple[str, ...]) -> dict:
        """Return a normalized assistant message with stable tool call IDs."""
        ...


class RuntimePort(Protocol):
    async def run(self, session: Session, admission_id: str, text: str) -> str:
        """Execute a newly admitted input."""
        ...
