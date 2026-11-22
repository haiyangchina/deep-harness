"""Local operator commands share the same application authority as HTTP."""

import asyncio
import json
import shlex
from dataclasses import asdict
from uuid import uuid4

from deep_harness.application.capabilities import derive_grants
from deep_harness.application.workspace import PROJECT_PROMPT
from deep_harness.domain.models import DomainError
from deep_harness.runtime.inference import usage_totals
from deep_harness.server_config import number


def display(value):
    print(json.dumps(value, default=asdict, ensure_ascii=False, indent=2))


async def select_session(app, args):
    if args.session:
        if (
            args.allow_memory
            or args.allow_delegation
            or args.allow_patch
            or args.mcp_tool
            or args.permission_mode
            or args.read_only
        ):
            raise DomainError("immutable_grants", "Existing session tool grants cannot be changed")
        session = await app.store.get_session(args.session, "operator")
        if args.harness and args.harness != session.harness:
            raise DomainError("immutable_harness", "Existing session harness cannot be changed")
        return session
    if args.read_only and (
        args.allow_memory
        or args.allow_delegation
        or args.allow_patch
        or args.mcp_tool
    ):
        raise DomainError("immutable_grants", "--read-only cannot be combined with extra grants")
    harness = args.harness or "general"
    extensions = []
    if args.allow_patch:
        extensions.append("apply_patch")
    if args.allow_memory:
        extensions.append("save_memory")
    if args.allow_delegation:
        extensions.append("delegate_task")
    grant_harness = "research" if args.read_only else harness
    tools = derive_grants(grant_harness, extensions, args.mcp_tool)
    # The execution kernel is derived from the harness tier, never chosen by the
    # operator. Specialised tiers require the unified deepagents kernel.
    engine = "deep" if harness != "general" else "standard"
    return await app.create_session(
        str(uuid4()),
        PROJECT_PROMPT,
        tools,
        engine=engine,
        harness=harness,
        permission_mode=args.permission_mode or "ask",
        full_access_acknowledged=args.acknowledge_full_access,
    )


async def send(app, session_id, text, submission_id=None):
    admission = await app.submit(session_id, submission_id or str(uuid4()), text)
    await app.wait_idle()
    final = await app.store.get_admission(admission.id, session_id)
    output = {**asdict(final), "admission_id": final.id}
    if final.status == "waiting_approval":
        output["approvals"] = (await app.sessions.status(session_id))["approvals"]
    display(output)
    return final


async def repl(app, session):
    print(f"Session: {session.id}")
    print("Commands: /status, /approve ID GENERATION, /deny ID GENERATION, /exit")
    while True:
        try:
            text = await asyncio.to_thread(input, "you> ")
        except EOFError:
            return 0
        text = text.strip()
        if not text:
            continue
        if text == "/exit":
            return 0
        try:
            if text == "/status":
                display(await app.sessions.status(session.id))
            elif text.startswith(("/approve", "/deny")):
                try:
                    parts = shlex.split(text)
                except ValueError as exc:
                    raise DomainError(
                        "invalid_command", "Approval command has invalid quoting"
                    ) from exc
                if len(parts) != 3 or parts[0] not in {"/approve", "/deny"}:
                    raise DomainError(
                        "invalid_command", "Use /approve ID GENERATION or /deny ID GENERATION"
                    )
                try:
                    generation = int(parts[2])
                except ValueError as exc:
                    raise DomainError(
                        "invalid_generation", "Generation must be an integer"
                    ) from exc
                await app.approve(session.id, parts[1], generation, parts[0][1:])
                await app.wait_idle()
                display(await app.sessions.status(session.id))
                history = await app.store.history(session.id)
                if history and history[-1]["role"] == "assistant":
                    print(history[-1]["content"])
            else:
                await send(app, session.id, text)
        except DomainError as exc:
            display({"code": exc.code, "message": str(exc)})


async def list_sessions(app, args):
    display(await app.store.list_sessions("operator", after=args.after, limit=args.limit))
    return 0


async def inspect_session(app, args):
    snapshot = await app.sessions.status(args.session)
    if args.messages:
        snapshot["messages"] = await app.store.history(args.session)
    display(snapshot)
    return 0


async def history(app, args):
    await app.store.get_session(args.session, "operator")
    display(await app.store.history_page(args.session, after=args.after, limit=args.limit))
    return 0


async def search(app, args):
    await app.store.get_session(args.session, "operator")
    matches = await app.store.search_history(args.session, args.query, limit=args.limit)
    display({"messages": matches})
    return 0


async def skill_add(app, args):
    with args.file.open("r", encoding="utf-8") as stream:
        document = stream.read(number("skills.document_max_length") + 1)
    display(await app.install_skill(document))
    return 0


async def skill_list(app, args):
    display({"skills": await app.knowledge.list_skills()})
    return 0


async def skill_disable(app, args):
    await app.disable_skill(args.name)
    display({"disabled": args.name})
    return 0


async def memory_add(app, args):
    display(await app.add_memory(args.content))
    return 0


async def memory_list(app, args):
    display({"memories": await app.knowledge.list_memories()})
    return 0


async def mcp_list(app, args):
    display({"tools": await app.mcp.discover()})
    return 0


async def memory_search(app, args):
    display({"memories": await app.knowledge.search(args.query)})
    return 0


async def memory_forget(app, args):
    await app.forget_memory(args.memory_id)
    display({"forgotten": args.memory_id})
    return 0


async def curate(app, args):
    display(await app.curate(args.session))
    return 0


async def curation_list(app, args):
    display({"candidates": await app.curator.ledger.list_candidates()})
    return 0


async def curation_decide(app, args):
    display(await app.decide_memory_candidate(args.candidate_id, args.decision))
    return 0


async def usage(app, args):
    await app.store.get_session(args.session, "operator")
    display(await usage_totals(app.store, args.session))
    return 0


async def delegations(app, args):
    display({"delegations": await app.list_delegations(args.session)})
    return 0


async def delegation_steer(app, args):
    display(
        await app.steer_delegation(
            args.session, args.delegation, args.submission_id or str(uuid4()), args.text
        )
    )
    return 0


async def delegation_stop(app, args):
    display(await app.stop_delegation(args.session, args.delegation))
    return 0


async def approve(app, args):
    await app.approve(args.session, args.interaction, args.generation, args.decision)
    await app.wait_idle()
    display(await app.sessions.status(args.session))
    return 0


async def fork(app, args):
    display(await app.fork_session(args.session, args.request_id or str(uuid4())))
    return 0




async def conversation(app, args):
    session = await select_session(app, args)
    if args.command == "repl":
        return await repl(app, session)
    final = await send(app, session.id, args.text, args.submission_id)
    return {"completed": 0, "waiting_approval": 3}.get(final.status, 1)


async def permissions(app, args):
    display(
        await app.set_permissions(
            args.session,
            args.mode,
            args.expected_revision,
            args.acknowledge_full_access,
        )
    )
    return 0


COMMANDS = {
    "permissions": permissions,
    "sessions": list_sessions,
    "inspect": inspect_session,
    "history": history,
    "search": search,
    "skill-add": skill_add,
    "skill-list": skill_list,
    "skill-disable": skill_disable,
    "memory-add": memory_add,
    "memory-list": memory_list,
    "mcp-list": mcp_list,
    "memory-search": memory_search,
    "memory-forget": memory_forget,
    "curate": curate,
    "curation-list": curation_list,
    "curation-decide": curation_decide,
    "usage": usage,
    "delegations": delegations,
    "delegation-steer": delegation_steer,
    "delegation-stop": delegation_stop,
    "approve": approve,
    "fork": fork,
    "chat": conversation,
    "repl": conversation,
}


async def run_console(host, args):
    try:
        return await COMMANDS[args.command](await host.application(args.profile), args)
    finally:
        await host.close()
