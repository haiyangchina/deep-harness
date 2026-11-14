"""A shared lease path protects creation/restore as well as already existing profiles."""

import asyncio

from filelock import FileLock, Timeout

from deep_harness.domain.models import DomainError


def lease_path(scope):
    return scope.state_root / "locks" / f"{scope.profile_id}.lock"


async def acquire_lease(scope):
    path = lease_path(scope)
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    lock = FileLock(path, thread_local=False)
    try:
        await asyncio.to_thread(lock.acquire, timeout=0)
    except Timeout as exc:
        raise DomainError("owner_exists", "Another process already owns this profile") from exc
    return lock
