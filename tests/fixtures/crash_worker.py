"""Disposable worker used to stop between an external write and its result receipt."""

import asyncio
import sys
from pathlib import Path

from deep_harness.bootstrap import ProjectHost


async def main():
    root = Path(sys.argv[1])
    host = ProjectHost(root / "state", root)
    app = await host.application("crash")
    session = await app.create_session("create", "Frozen")
    first = await app.store.admit(
        session,
        principal="operator",
        submission_id="one",
        text="first",
    )
    await app.store.admit(
        session,
        principal="operator",
        submission_id="two",
        text="follower",
    )
    claim = await app.executions.claim(session.id, app.owner)
    await app.receipts.prepare_effect(claim, "crashed-effect", "proposal")
    await app.receipts.dispatch_effect(claim, "crashed-effect")
    (root / "external-effect.txt").write_text("occurred-once", encoding="utf-8")
    print(f"{session.id} {first.id}", flush=True)
    await asyncio.to_thread(sys.stdin.readline)


if __name__ == "__main__":
    asyncio.run(main())
