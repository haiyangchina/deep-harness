"""Single-owner composition for the browser-facing application."""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

from filelock import FileLock, Timeout
from jsonschema import Draft202012Validator

from deep_harness.application.config import ExecutionSettings
from deep_harness.application.jobs import ConfiguredStep, CreateJob, StagePlan
from deep_harness.application.workspace import READ_TOOLS
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.connectors import HttpTools
from deep_harness.infrastructure.web.settings import WebSettings
from deep_harness.runtime.model_factory import CatalogEntry, ModelCatalog
from deep_harness.runtime.sandbox import DockerExecutor


class InstanceHost:
    def __init__(
        self,
        settings,
        state_dir: Path,
        *,
        config_path=None,
        model_factory=None,
        python_tools=None,
    ):
        self.settings = settings
        self.state_dir = state_dir.resolve()
        self.models = ModelCatalog(
            {
                name: CatalogEntry(
                    model.label or name,
                    model.model_settings(),
                    model.secret_env,
                    model.capabilities,
                    model.concurrent_requests,
                    model.secret_file,
                )
                for name, model in settings.models.items()
            },
            settings.default_model,
            factory=model_factory,
        )
        self.connectors = HttpTools(settings.connectors)
        self.resources = AsyncExitStack()
        self.submit_lock = asyncio.Lock()
        self.run_slots = asyncio.Semaphore(settings.limits.active_runs)
        self.executor = (
            DockerExecutor(settings.sandbox, fingerprint(str(self.state_dir)))
            if settings.sandbox
            else None
        )
        hidden = [
            row.secret_file.resolve()
            for row in (*settings.models.values(), *settings.connectors.values())
            if row.secret_file is not None
        ]
        if config_path:
            hidden.append(config_path.resolve())
        self.protected_paths = tuple(hidden)
        self.hosts = {}
        for name, workspace in settings.workspaces.items():
            root = workspace.path.resolve(strict=True)
            if (
                self.executor
                and workspace.mode == "sandbox"
                and (
                    self.state_dir.is_relative_to(root)
                    or any(path.is_relative_to(root) for path in self.protected_paths)
                )
            ):
                raise ValueError("A sandbox workspace cannot contain the state or configuration")
            if root.is_relative_to(self.state_dir):
                raise ValueError("The work directory cannot be part of the instance state")
            self.hosts[name] = ProjectHost(
                self.state_dir,
                root,
                execution_limits=ExecutionSettings(),
                web_settings=WebSettings(),
                models=self.models,
                run_slots=self.run_slots,
                server_mode=workspace.mode,
                protected_paths=self.protected_paths,
                connectors=self.connectors,
                executor=self.executor if workspace.mode == "sandbox" else None,
                upload_limit=settings.limits.max_upload_bytes,
                mcp_objects={name: (python_tools or {}).get(name, {})},
            )
        for name, workflow in settings.workflows.items():
            self.workflow_schemas(name, workflow)

    def workflow_schemas(self, name, workflow):
        schemas = {}
        for step in workflow.steps:
            if step.type == "tool" and step.tool not in self.connectors.public_names:
                raise ValueError(f"Workflow {name} requires a registered HTTP tool: {step.tool}")
            if step.type != "verify":
                continue
            schema = step.schema_
            if schema is None:
                targets = [
                    next_step.tool
                    for next_step in workflow.steps
                    if next_step.type == "tool" and next_step.input_from == step.id
                ]
                if len(targets) != 1 or targets[0] not in self.connectors.public_names:
                    raise ValueError(f"Workflow {name} verification requires a JSON schema")
                schema = self.connectors.definitions[
                    self.connectors.public_names[targets[0]]
                ].schema_
            Draft202012Validator.check_schema(schema)
            schemas[step.id] = schema
        return schemas

    async def start(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(self.state_dir / "instance.lock", thread_local=False)
        try:
            await asyncio.to_thread(lock.acquire, timeout=0)
        except Timeout as exc:
            raise DomainError("owner_exists", "Another server owns this state directory") from exc
        self.resources.callback(lock.release)
        try:
            if self.executor and any(
                w.mode == "sandbox" for w in self.settings.workspaces.values()
            ):
                await self.executor.check()
                await self.executor.clean_orphans()
                self.resources.push_async_callback(self.executor.close)
            for name, host in self.hosts.items():
                self.resources.push_async_callback(host.close)
                await host.application(name)
        except BaseException:
            await self.resources.aclose()
            raise

    async def close(self):
        await self.resources.aclose()

    async def application(self, workspace_id):
        host = self.hosts.get(workspace_id)
        if host is None:
            raise DomainError("workspace_not_found", "Workspace is not configured")
        return await host.application(workspace_id)

    def tool_names(self, workspace_id, tools):
        workspace = self.settings.workspaces[workspace_id]
        available = set(READ_TOOLS) | set(workspace.mcp_tools) | set(self.connectors.catalog)
        if workspace.mode == "sandbox":
            available.add("bash")
        resolved = tuple(self.connectors.public_names.get(tool, tool) for tool in tools)
        if set(resolved) - available:
            raise DomainError("tool_denied", "Agent requests tools outside this workspace")
        return resolved

    async def create_session(self, workspace_id, body):
        app = await self.application(workspace_id)
        agent = self.settings.agents.get(body.agent_id)
        if body.agent_id and agent is None:
            raise DomainError("agent_not_found", "Agent is not configured")
        if agent:
            tools = self.tool_names(workspace_id, agent.tools)
            harness = agent.harness
            model_id = body.model_id or agent.model
            prompt = agent.instructions
        else:
            tools = self.tool_names(workspace_id, body.tools)
            harness = body.harness
            model_id = body.model_id
            prompt = body.prompt
        if harness in {"research", "research_web"} and any(
            name not in READ_TOOLS
            and (
                name not in self.connectors.definitions or self.connectors.definitions[name].writes
            )
            for name in tools
        ):
            raise DomainError("tool_denied", "Research agents only use read-only tools")
        model_id = model_id or self.models.default
        model = self.models.get(
            model_id,
            tools=bool(tools),
            required=agent.required_capabilities if agent else ("text",),
        )
        if harness != "general" and not hasattr(model, "model"):
            raise DomainError("model_capability", "This Harness requires a LangChain chat model")
        return await app.create_session(
            body.request_id,
            prompt,
            tools,
            harness=harness,
            model_id=model_id,
            engine="standard" if harness == "general" else "deep",
        )

    async def submit(self, workspace_id, session_id, submission_id, text, artifact_ids):
        app = await self.application(workspace_id)
        async with self.submit_lock:
            prior = await app.store.find_submission(session_id, submission_id)
            if prior is None:
                total = 0
                for name in self.hosts:
                    profile = await self.application(name)
                    total += await profile.store.queued_count()
                if total >= self.settings.limits.queued_runs:
                    raise DomainError("queue_full", "The instance queue is full")
            return await app.submit(session_id, submission_id, text, artifact_ids=artifact_ids)

    async def start_workflow(self, workspace_id, workflow_id, body):
        app = await self.application(workspace_id)
        workflow = self.settings.workflows.get(workflow_id)
        agent = self.settings.agents.get(body.agent_id)
        if workflow is None or agent is None or agent.workflow != workflow_id:
            raise DomainError("workflow_not_found", "Workflow is not configured for the agent")
        schemas = self.workflow_schemas(workflow_id, workflow)
        positions = {step.id: index for index, step in enumerate(workflow.steps)}
        stages = []
        model_id = body.model_id or agent.model or self.models.default
        self.models.get(model_id, required=agent.required_capabilities)
        for step in workflow.steps:
            if step.type == "agent":
                tools = self.tool_names(workspace_id, agent.tools)
                if any(
                    tool not in READ_TOOLS
                    and (
                        tool not in self.connectors.definitions
                        or self.connectors.definitions[tool].writes
                    )
                    for tool in tools
                ):
                    raise DomainError("tool_denied", "Workflow drafting tools must be read-only")
            elif step.type == "tool":
                tools = self.tool_names(workspace_id, (step.tool,))
                source = workflow.steps[positions[step.input_from]]
                if self.connectors.definitions[tools[0]].writes and source.type != "verify":
                    raise DomainError("workflow_config", "A write must consume verified input")
            else:
                tools = ()
            stages.append(
                StagePlan(
                    title=step.id,
                    goal=f"Configured {step.type} step",
                    acceptance="Complete with evidence",
                    read_only=True,
                    step=ConfiguredStep(
                        kind=step.type,
                        model_id=model_id,
                        tools=tools,
                        source=positions.get(step.input_from),
                        schema=schemas.get(step.id),
                        instructions=agent.instructions if step.type == "agent" else "",
                    ),
                )
            )
        async with self.submit_lock:
            return await app.create_job(
                CreateJob(request_id=body.request_id, goal=body.goal, stages=stages)
            )

    def public_catalog(self):
        return {
            "default_workspace": self.settings.default_workspace,
            "default_model": self.settings.default_model,
            "workspaces": [
                {"id": name, "mode": row.mode, "mcp_tools": list(row.mcp_tools)}
                for name, row in self.settings.workspaces.items()
            ],
            "models": self.models.list(),
            "agents": [
                {
                    "id": name,
                    "label": row.label or name,
                    "harness": row.harness,
                    "workflow": row.workflow,
                    "required_capabilities": sorted(row.required_capabilities),
                }
                for name, row in self.settings.agents.items()
            ],
            "tools": sorted(set(READ_TOOLS) | self.connectors.public_names.keys()),
            "workflows": list(self.settings.workflows),
        }
