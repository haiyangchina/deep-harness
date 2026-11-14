"""CLI lifecycle operations; restore does not open the target profile first."""

import asyncio
import json

from deep_harness.transports.console import display
from deep_harness.transports.operations import Resolution


async def backup(host, args):
    app = await host.application(args.profile)
    display(await app.backup(args.path))


async def restore(host, args):
    display(await host.restore(args.profile, args.path))


async def cancel(host, args):
    app = await host.application(args.profile)
    display(await app.cancel(args.session, args.admission))


async def resolve(host, args):
    text = await asyncio.to_thread(args.evidence_file.read_text, encoding="utf-8")
    request = Resolution.model_validate(json.loads(text))
    app = await host.application(args.profile)
    display(
        await app.reconcile(
            args.session,
            args.admission,
            request.generation,
            request.evidence,
            [effect.model_dump() for effect in request.effects],
        )
    )


COMMANDS = {"backup": backup, "restore": restore, "cancel": cancel, "resolve": resolve}


async def run_admin(host, args):
    try:
        await COMMANDS[args.command](host, args)
        return 0
    finally:
        await host.close()
