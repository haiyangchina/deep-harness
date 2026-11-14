"""Admission gates and bounded graceful shutdown for a single profile owner."""

import asyncio
from functools import wraps

from deep_harness.domain.models import DomainError


def mutation(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        async with self.control:
            if self.closing:
                raise DomainError("draining", "Application is draining; new mutations are refused")
            return await method(self, *args, **kwargs)

    return guarded


async def drain_tasks(tasks, grace_seconds):
    current = tuple(tasks)
    if not current:
        return
    _, pending = await asyncio.wait(current, timeout=grace_seconds)
    for task in pending:
        task.cancel()
    await asyncio.gather(*current, return_exceptions=True)
