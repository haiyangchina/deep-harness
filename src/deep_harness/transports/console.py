"""Local operator commands share the same application authority as HTTP."""

import asyncio
import json
import shlex
import sys
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from deep_harness.application.capabilities import derive_grants
from deep_harness.application.workspace import PROJECT_PROMPT
from deep_harness.domain.images import MAX_IMAGE_BYTES, MAX_IMAGES
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.change_bundles import MAX_BUNDLE_BYTES
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
            or args.workspace_mode
            or args.verify_command
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
    harness = args.harness or (
        "coding" if not args.read_only and hasattr(app.model, "model") else "general"
    )
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
        workspace_mode=(args.workspace_mode or "auto") if harness == "coding" and not args.read_only
        else args.workspace_mode,
        verification_command=args.verify_command,
    )


async def upload_images(app, session_id, paths):
    if len(paths) > MAX_IMAGES:
        raise DomainError("invalid_attachments", "Use up to four images per message")
    ids = []
    for path in paths:
        row = await app.publish_attachment(session_id, image_chunks(path), path.name, image=True)
        ids.append(row["id"])
    return ids


async def image_chunks(path):
    try:
        with path.open("rb") as source:
            while data := await asyncio.to_thread(source.read, 64 * 1024):
                yield data
    except OSError as exc:
        raise DomainError("image_read", f"Could not read image: {path.name}") from exc


async def send(app, session_id, text, submission_id=None, image_paths=()):
    if image_paths and submission_id:
        existing = await app.store.find_submission(session_id, submission_id)
        if existing:
            ids = await app.store.admission_image_ids(existing)
            if existing.text != text or len(ids) != len(image_paths):
                raise DomainError("idempotency_conflict", "Submission payload changed")
            for image_id, path in zip(ids, image_paths, strict=True):
                row, _ = await app.get_image(session_id, image_id)
                if await asyncio.to_thread(image_digest, path) != row["sha256"]:
                    raise DomainError("idempotency_conflict", "Submission image changed")
            if existing.status == "queued":
                app.schedule(session_id)
            await app.wait_idle()
            return await display_admission(app, session_id, existing.id)
    image_ids = await upload_images(app, session_id, image_paths)
    admission = await app.submit(
        session_id, submission_id or str(uuid4()), text, image_ids=image_ids
    )
    await app.wait_idle()
    return await display_admission(app, session_id, admission.id)


def image_digest(path):
    try:
        with path.open("rb") as source:
            data = source.read(MAX_IMAGE_BYTES + 1)
    except OSError as exc:
        raise DomainError("image_read", f"Could not read image: {path.name}") from exc
    return sha256(data).hexdigest()


async def display_admission(app, session_id, admission_id):
    final = await app.store.get_admission(admission_id, session_id)
    output = {**asdict(final), "admission_id": final.id}
    if final.status == "waiting_approval":
        output["approvals"] = (await app.sessions.status(session_id))["approvals"]
    display(output)
    return final


async def repl(app, session):
    print(f"Session: {session.id}")
    print("Commands: /image PATH [TEXT], /status, /approve ID GENERATION, "
          "/deny ID GENERATION, /exit")
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
            elif text == "/image" or text.startswith("/image "):
                try:
                    # Preserve Windows path separators as well as spaces inside quoted paths.
                    parts = shlex.split(text, posix=False)
                except ValueError as exc:
                    raise DomainError(
                        "invalid_command", "Image command has invalid quoting"
                    ) from exc
                if len(parts) < 2:
                    raise DomainError("invalid_command", "Use /image PATH [TEXT]")
                name = parts[1]
                if name[:1] in {'"', "'"}:
                    name = name[1:-1]
                await send(app, session.id, " ".join(parts[2:]), image_paths=[Path(name)])
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


async def coding_diff(app, args):
    await app.store.get_session(args.session, "operator")
    admission = await app.store.get_admission(args.admission, args.session)
    if admission.status == "completed":
        try:
            workspace = json.loads(admission.result)["execution_evidence"]["workspace"]
        except (TypeError, ValueError, KeyError):
            workspace = None
        if workspace and workspace["status"] != "unavailable":
            # A redirected Windows stdout must preserve LF even for a CRLF source diff.
            sys.stdout.buffer.write(workspace["diff"].encode("utf-8"))
            if workspace["status"] != "observed" or workspace["diff_truncated"]:
                print("The saved worktree diff is partial.", file=sys.stderr)
            return 0
    raise DomainError("diff_unavailable", "This coding turn has no saved worktree diff")


async def deliver(app, args):
    display(await app.deliver(args.session, args.action, args.revision, args.change_set_id))
    return 0


async def change_bundle(app, args):
    if args.action == "import":
        try:
            raw = await asyncio.to_thread(read_bundle_file, args.file_or_id)
        except OSError as exc:
            raise DomainError("bundle_invalid", "Cannot read the change package") from exc
        result = await app.imported_bundle(raw=raw)
    else:
        action = None if args.action == "inspect" else args.action
        result = await app.imported_bundle(identifier=args.file_or_id, action=action)
    display(result)
    return 0


def read_bundle_file(name):
    with Path(name).open("rb") as source:
        return source.read(MAX_BUNDLE_BYTES + 1)


async def development_process(app, args):
    start = None
    if args.process_command:
        start = {
            "command": args.process_command, "request_id": args.request_id,
            "cwd": args.cwd, "lifetime": args.lifetime,
        }
    result = await app.development_processes(args.session, start=start, stop=args.stop)
    if start:
        while result["status"] == "running":
            await asyncio.sleep(0.5)
            rows = await app.development_processes(args.session)
            result = next(row for row in rows if row["id"] == result["id"])
    display(result)
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
    final = await send(app, session.id, args.text, args.submission_id, args.image)
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
    "diff": coding_diff,
    "delivery": deliver,
    "bundle": change_bundle,
    "process": development_process,
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
