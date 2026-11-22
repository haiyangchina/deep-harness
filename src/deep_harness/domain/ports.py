"""The model port does not expose framework message classes."""

from typing import Protocol


class ModelPort(Protocol):
    request_timeout: float
    identity: dict

    async def complete(self, prompt: str, messages: list[dict], tools: tuple[str, ...]) -> dict:
        """Return a normalized assistant message with stable tool call IDs."""
        ...
