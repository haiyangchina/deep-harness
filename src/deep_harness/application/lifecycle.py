"""Admission gates and bounded graceful shutdown for a single profile owner."""

import asyncio
from functools import wraps

from deep_harness.domain.models import DomainError
from deep_harness.server_config import number


def mutation(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        task = asyncio.current_task()
        async with self.control:
            if self.closing:
                raise DomainError("draining", "Application is draining; new mutations are refused")
            if self.maintenance:
                raise DomainError("maintenance", "A profile backup is in progress")
            self.mutations.add(task)
        try:
            return await method(self, *args, **kwargs)
        finally:
            self.mutations.discard(task)

    return guarded


async def drain_tasks(tasks, grace_seconds):
    current = tuple(tasks)
    if not current:
        return
    _, pending = await asyncio.wait(current, timeout=grace_seconds)
    for task in pending:
        task.cancel()
    if pending:
        _, stuck = await asyncio.wait(
            pending, timeout=max(number("lifecycle.cancel_grace_min_seconds"), grace_seconds)
        )
        if stuck:
            raise DomainError(
                "shutdown_incomplete", "Some owned tasks did not stop before shutdown"
            )
