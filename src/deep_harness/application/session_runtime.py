"""Session configuration and branching authority, independent of transport entry points."""

from copy import deepcopy
from itertools import product

from deep_harness.application.capabilities import WEB_TOOLS
from deep_harness.application.harness_spec import CONTEXT_POLICY
from deep_harness.application.tools import tool_definitions
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.branches import BranchLedger
from deep_harness.infrastructure.policies import PolicyLedger
from deep_harness.runtime.harnesses import harness_contract


def preserve_tool_binding(binding, snapshot):
    """Recognize compatible historical tool schemas without changing their fingerprints."""
    if not binding or binding["fingerprint"] == fingerprint(snapshot):
        return snapshot
    saved_tools = {tool["name"]: tool for tool in binding["snapshot"]["tools"]}
    for old_images, old_patch, old_roles, old_isolation, old_coding_tools in product(
        (False, True), repeat=5,
    ):
        legacy = deepcopy(snapshot)
        for tool in legacy["tools"]:
            if tool["name"] == "delegate_task":
                properties = tool["parameters"]["properties"]
                if old_images:
                    properties.pop("share_images", None)
                if old_roles:
                    properties["kind"]["enum"] = ["research", "coding"]
                if old_isolation:
                    properties.pop("isolate", None)
            if old_coding_tools and tool["name"] in {"list_files", "bash"}:
                old_tool = saved_tools.get(tool["name"])
                if old_tool is not None:
                    tool["description"] = old_tool["description"]
                if tool["name"] == "list_files" and old_tool and "recursive" not in (
                    old_tool["parameters"]["properties"]
                ):
                    tool["parameters"]["properties"].pop("recursive", None)
                    tool["parameters"]["title"] = "ListFilesArgs"
            if old_patch and tool["name"] == "apply_patch":
                tool["parameters"]["properties"]["files"]["minItems"] = 2
        if binding["fingerprint"] == fingerprint(legacy):
            return legacy
    return snapshot


class SessionRuntime:
    def __init__(self, store, scope, model, limits, mcp=None, web_settings=None, models=None):
        self.store = store
        self.scope = scope
        self.model = model
        self.models = models
        self.limits = limits
        self.mcp = mcp
        self.web_settings = web_settings
        self.policies = PolicyLedger(store)
        self.branches = BranchLedger(store)

    def snapshot(
        self,
        tools,
        *,
        engine="standard",
        harness="general",
        knowledge=None,
        mcp_catalog=None,
        model_id=None,
        coding_task=None,
    ):
        model = self.select_model(model_id, tools)
        result = {
            "version": 4,
            "engine": engine,
            "workspace": str(self.scope.workspace),
            "model": dict(model.identity),
            "tools": tool_definitions(tools, mcp_catalog),
            "execution": self.limits.model_dump(),
        }
        result["context_projection"] = CONTEXT_POLICY
        result["model_request_timeout"] = model.request_timeout
        if self.models is not None:
            result["model_id"] = model_id or self.models.default
            result["model_revision"] = self.models.revision(result["model_id"])
        if self.scope.server_mode:
            result["server_mode"] = self.scope.server_mode
            result["executor_revision"] = self.scope.executor_revision
        if coding_task is not None:
            result["coding_task"] = coding_task
        if self.web_settings is not None and set(tools).intersection(WEB_TOOLS):
            result["web"] = {
                key: value
                for key, value in self.web_settings.model_dump(mode="json").items()
                if not key.endswith("_env") and key not in {"searxng_url", "firecrawl_url"}
            }
            result["web"]["endpoint_hash"] = fingerprint(
                [self.web_settings.searxng_url, self.web_settings.firecrawl_url]
            )
            if "pi" not in self.web_settings.routing:
                result["web"].pop("pi_providers", None)
        if knowledge is not None:
            result["knowledge"] = knowledge
        if harness != "general":
            result["harness"] = harness
            result["harness_contract"] = harness_contract(harness)
        mcp_names = [name for name in tools if name.startswith("mcp__")]
        if mcp_names:
            result["mcp_bindings"] = {name: mcp_catalog[name] for name in mcp_names}
        return result

    def select_model(self, model_id, tools):
        if self.models is None:
            if model_id is not None:
                raise DomainError("model_not_found", "This host uses its launch model")
            return self.model
        return self.models.get(model_id, tools=bool(tools))

    def supports_vision(self, model_id=None):
        if self.models is not None:
            return "vision" in self.models.capabilities(model_id)
        return getattr(self.model, "vision", False)

    async def session_vision(self, session_id):
        if self.models is None:
            return self.supports_vision()
        binding = await self.policies.get(session_id)
        return self.supports_vision(binding["snapshot"].get("model_id") if binding else None)

    async def require_vision(self, session):
        if not await self.session_vision(session.id):
            raise DomainError("model_capability", "The selected model cannot accept images")

    async def catalog_for(self, tools):
        if any(name.startswith("mcp__") for name in tools):
            if self.mcp is None:
                raise DomainError("mcp_unavailable", "MCP registry is not configured")
            servers = set()
            for name in tools:
                if name.startswith("mcp__"):
                    parts = name.split("__", 2)
                    if len(parts) != 3 or not parts[1] or not parts[2]:
                        raise DomainError(
                            "mcp_not_found", "MCP tool name must be mcp__server__tool"
                        )
                    servers.add(parts[1])
            return await self.mcp.discover(servers)
        return None

    async def validate(self, session):
        if session.permission_mode not in {"ask", "auto", "full"}:
            raise DomainError("runtime_mismatch", "Session permission mode is unsupported")
        binding = await self.policies.get(session.id)
        knowledge = binding["snapshot"].get("knowledge") if binding else None
        catalog = await self.catalog_for(session.tools)
        current = self.snapshot(
            session.tools,
            engine=session.engine,
            harness=session.harness,
            knowledge=knowledge,
            mcp_catalog=catalog,
            model_id=binding["snapshot"].get("model_id") if binding else None,
            coding_task=binding["snapshot"].get("coding_task") if binding else None,
        )
        await self.policies.assert_matches(
            session.id, preserve_tool_binding(binding, current)
        )

    async def status(self, session_id):
        status = await self.store.session_status(session_id, "operator")
        binding = await self.policies.get(session_id)
        knowledge = binding["snapshot"].get("knowledge") if binding else None
        catalog = await self.catalog_for(status["session"].tools)
        saved = binding["snapshot"] if binding else {}
        status["vision"] = self.supports_vision(saved.get("model_id"))
        try:
            current = self.snapshot(
                status["session"].tools,
                engine=status["session"].engine,
                harness=status["session"].harness,
                knowledge=knowledge,
                mcp_catalog=catalog,
                model_id=saved.get("model_id"),
                coding_task=saved.get("coding_task"),
            )
        except DomainError as exc:
            if exc.code not in {"model_not_found", "model_secret_missing", "model_capability"}:
                raise
            status["runtime"] = {"binding": binding, "compatible": False, "error": exc.code}
            return status
        current = preserve_tool_binding(binding, current)
        status["runtime"] = {
            "binding": binding,
            "current_snapshot": current,
            "current_fingerprint": fingerprint(current),
            "compatible": binding is not None and binding["fingerprint"] == fingerprint(current),
            "changed_fields": sorted(
                key for key in saved.keys() | current.keys() if saved.get(key) != current.get(key)
            ),
        }
        return status
