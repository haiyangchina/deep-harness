"""Independent entry point, defaulting to the configured local Ollama model."""

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

from deep_harness.application.capabilities import HARNESSES
from deep_harness.application.config import (
    load_execution_settings,
    load_model_settings,
    load_web_settings,
)
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.images import MAX_IMAGE_BYTES
from deep_harness.domain.models import DomainError
from deep_harness.runtime.model_factory import open_model
from deep_harness.server_config import number, setting


def parser():
    result = argparse.ArgumentParser(prog="deep-harness")
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--workspace", type=Path, default=Path.cwd())
    result.add_argument("--profile", default="default")
    result.add_argument("--config", type=Path, help="Runtime configuration YAML or TOML override")
    result.add_argument("--provider", choices=["ollama", "openai", "deepseek", "diagnostic"])
    result.add_argument(
        "--model",
        help="Model identifier; defaults to the configured local Ollama model",
    )
    result.add_argument("--base-url")
    result.add_argument("--context-window", type=int, help="Ollama context window in tokens")
    result.add_argument("--api-key-env", default="OPENAI_API_KEY")
    commands = result.add_subparsers(dest="command", required=True)
    chat = commands.add_parser("chat")
    chat.add_argument("text", nargs="?", default="")
    chat.add_argument(
        "--image", action="append", type=Path, default=[], help="Attach a PNG or JPEG (repeatable)"
    )
    chat.add_argument("--session", help="Continue an existing session")
    chat.add_argument("--harness", choices=list(HARNESSES))
    chat.add_argument("--workspace-mode", choices=["auto", "worktree", "copy", "in_place"])
    chat.add_argument("--verify-command")
    chat.add_argument("--permission-mode", choices=["ask", "auto", "full"])
    chat.add_argument(
        "--acknowledge-full-access",
        action="store_true",
        help="Confirm the full-access risk notice (required with --permission-mode full)",
    )
    chat.add_argument(
        "--mcp-tool", action="append", default=[], help="Grant one configured MCP tool"
    )
    chat.add_argument(
        "--allow-delegation", action="store_true", help="Grant scoped Deep specialists"
    )
    chat.add_argument("--allow-patch", action="store_true", help="Grant multi-file patches")
    chat.add_argument(
        "--allow-memory",
        action="store_true",
        help="Grant memory saves under the session approval policy",
    )
    chat.add_argument(
        "--read-only", action="store_true", help="Grant only the read-only core tools"
    )
    chat.add_argument("--submission-id", help="Stable ID for safe submission retries")
    repl = commands.add_parser("repl")
    repl.add_argument("--session")
    repl.add_argument("--harness", choices=list(HARNESSES))
    repl.add_argument("--workspace-mode", choices=["auto", "worktree", "copy", "in_place"])
    repl.add_argument("--verify-command")
    repl.add_argument("--permission-mode", choices=["ask", "auto", "full"])
    repl.add_argument(
        "--acknowledge-full-access",
        action="store_true",
        help="Confirm the full-access risk notice (required with --permission-mode full)",
    )
    repl.add_argument("--mcp-tool", action="append", default=[])
    repl.add_argument("--allow-delegation", action="store_true")
    repl.add_argument("--allow-patch", action="store_true", help="Grant multi-file patches")
    repl.add_argument("--allow-memory", action="store_true")
    repl.add_argument("--read-only", action="store_true")
    sessions = commands.add_parser("sessions")
    sessions.add_argument("--after", default="")
    sessions.add_argument(
        "--limit",
        type=int,
        choices=range(1, number("pagination.max_page_size") + 1),
        default=number("pagination.page_size"),
    )
    inspect = commands.add_parser("inspect")
    inspect.add_argument("session")
    inspect.add_argument("--messages", action="store_true")
    diff = commands.add_parser("diff", help="Print the observed worktree diff for a coding turn")
    diff.add_argument("session")
    diff.add_argument("admission")
    delivery = commands.add_parser("delivery", help="Review, apply or undo an isolated task")
    delivery.add_argument("session")
    delivery.add_argument("--action", choices=["apply", "undo", "discard", "reconcile"])
    delivery.add_argument("--revision")
    delivery.add_argument("--change-set-id")
    bundle = commands.add_parser("bundle", help="Import, review or apply a change package")
    bundle.add_argument("file_or_id")
    bundle.add_argument("--action", choices=["import", "inspect", "apply", "undo", "reconcile"],
                        default="import")
    process = commands.add_parser("process", help="Manage local coding development processes")
    process.add_argument("session")
    process.add_argument("--command", dest="process_command")
    process.add_argument("--request-id")
    process.add_argument("--cwd", default=".")
    process.add_argument("--lifetime", type=float, default=3600)
    process.add_argument("--stop")
    permissions = commands.add_parser("permissions")
    permissions.add_argument("session")
    permissions.add_argument("--mode", required=True, choices=["ask", "auto", "full"])
    permissions.add_argument("--expected-revision", type=int, required=True)
    permissions.add_argument(
        "--acknowledge-full-access",
        action="store_true",
        help="Confirm the full-access risk notice (required with --mode full)",
    )
    delegations = commands.add_parser("delegations")
    delegations.add_argument("session")
    delegation_steer = commands.add_parser("delegation-steer")
    delegation_steer.add_argument("session")
    delegation_steer.add_argument("delegation")
    delegation_steer.add_argument("text")
    delegation_steer.add_argument("--submission-id")
    delegation_stop = commands.add_parser("delegation-stop")
    delegation_stop.add_argument("session")
    delegation_stop.add_argument("delegation")
    history = commands.add_parser("history")
    history.add_argument("session")
    history.add_argument("--after", default="")
    history.add_argument(
        "--limit",
        type=int,
        choices=range(1, number("pagination.max_page_size") + 1),
        default=number("pagination.page_size"),
    )
    search = commands.add_parser("search")
    search.add_argument("session")
    search.add_argument("query")
    search.add_argument(
        "--limit",
        type=int,
        choices=range(1, number("pagination.max_search_results") + 1),
        default=number("pagination.search_results"),
    )
    skill_add = commands.add_parser("skill-add")
    skill_add.add_argument("file", type=Path)
    commands.add_parser("skill-list")
    skill_disable = commands.add_parser("skill-disable")
    skill_disable.add_argument("name")
    memory_add = commands.add_parser("memory-add")
    memory_add.add_argument("content")
    commands.add_parser("memory-list")
    commands.add_parser("mcp-list")
    memory_search = commands.add_parser("memory-search")
    memory_search.add_argument("query")
    memory_forget = commands.add_parser("memory-forget")
    memory_forget.add_argument("memory_id")
    curate = commands.add_parser("curate")
    curate.add_argument("session")
    commands.add_parser("curation-list")
    curation_decide = commands.add_parser("curation-decide")
    curation_decide.add_argument("candidate_id")
    curation_decide.add_argument("--decision", required=True, choices=["accept", "reject"])
    usage = commands.add_parser("usage")
    usage.add_argument("session")
    fork = commands.add_parser("fork")
    fork.add_argument("session")
    fork.add_argument("--request-id", help="Stable ID for safe fork retries")
    approve = commands.add_parser("approve")
    approve.add_argument("session")
    approve.add_argument("interaction")
    approve.add_argument("--generation", required=True, type=int)
    approve.add_argument("--decision", required=True, choices=["approve", "deny"])
    web = commands.add_parser("web", help="Serve the shared, login-free browser workbench")
    web.add_argument("--instance-config", type=Path, required=True)
    web.add_argument("--assets", type=Path, help="Directory containing the built web interface")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=number("transport.http_port"))
    for name in ("backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("path", type=Path)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("session")
    cancel.add_argument("admission")
    resolve = commands.add_parser("resolve")
    resolve.add_argument("session")
    resolve.add_argument("admission")
    resolve.add_argument("--evidence-file", required=True, type=Path)
    doctor = commands.add_parser("doctor")
    doctor.add_argument(
        "--check-model", action="store_true", help="Inspect model metadata without generation"
    )
    return result


def build_host(args):
    execution_limits = load_execution_settings(args.config)
    web_settings = load_web_settings(args.config)
    recover_queued = args.command in {"chat", "repl", "serve", "approve"}
    settings = load_model_settings(
        args.config,
        provider=args.provider,
        name=args.model,
        base_url=args.base_url,
        context_window_tokens=args.context_window,
    )
    try:
        model = open_model(settings, args.api_key_env)
    except DomainError as exc:
        raise SystemExit(str(exc)) from exc

    def factory(scope):
        if settings.provider != "diagnostic" and scope.profile_id != args.profile:
            raise DomainError("profile_mismatch", "Model route is bound to the launch profile")
        return model

    return ProjectHost(
        args.state_dir,
        args.workspace,
        factory,
        recover_queued=recover_queued,
        execution_limits=execution_limits,
        web_settings=web_settings,
        upload_limit=MAX_IMAGE_BYTES,
    )


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    command_line = parser()
    args = command_line.parse_args()
    if args.command == "chat" and (not args.text.strip() and not args.image):
        command_line.error("chat requires text or --image")
    if args.command == "delivery" and bool(args.action) != bool(args.revision):
        command_line.error("delivery requires both --action and --revision to write files")
    if args.command == "process" and (
        (args.process_command is not None and (not args.request_id or args.stop))
        or not 1 <= args.lifetime <= 86400
    ):
        command_line.error("starting a process requires --command and --request-id")
    if args.command == "web":
        from deep_harness.transports.web_launcher import serve_web

        return serve_web(args)
    if args.command in {"backup", "restore", "cancel", "resolve"}:
        from deep_harness.transports.admin import run_admin

        try:
            code = asyncio.run(run_admin(build_host(args), args))
        except DomainError as exc:
            print(json.dumps({"code": exc.code, "message": str(exc)}), file=sys.stderr)
            code = 2
        raise SystemExit(code)
    if args.command == "doctor":
        from deep_harness.runtime.probe import report

        settings = load_model_settings(
            args.config,
            provider=args.provider,
            name=args.model,
            base_url=args.base_url,
            context_window_tokens=args.context_window,
        )
        evidence = {**report(), "model": settings.model_dump()}
        if args.check_model:
            from deep_harness.runtime.readiness import check_model

            evidence["model_readiness"] = asyncio.run(check_model(settings))
        print(
            json.dumps(
                evidence,
                ensure_ascii=False,
                indent=2,
            )
        )
        if args.check_model and evidence["model_readiness"]["status"] != "ready":
            raise SystemExit(1)
        return
    if args.command != "serve":
        from deep_harness.transports.console import run_console

        try:
            code = asyncio.run(run_console(build_host(args), args))
        except DomainError as exc:
            print(
                json.dumps({"code": exc.code, "message": str(exc)}, ensure_ascii=False),
                file=sys.stderr,
            )
            code = 2
        raise SystemExit(code)
    import uvicorn

    from deep_harness.transports.api import create_api

    args.state_dir.mkdir(parents=True, exist_ok=True)
    token_path = args.state_dir / "operator.token"
    if not token_path.exists():
        # Exclusive creation avoids competing launchers rotating an existing token.
        try:
            with token_path.open("x", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(number("transport.token_random_bytes")))
        except FileExistsError:
            pass
    token = token_path.read_text(encoding="utf-8").strip()
    print(f"Operator token file: {token_path.resolve()}")
    uvicorn.run(
        create_api(build_host(args), token, workspace_id=args.profile),
        host="127.0.0.1",
        port=args.port,
        log_level=setting("transport.cli_log_level"),
    )


if __name__ == "__main__":
    main()
