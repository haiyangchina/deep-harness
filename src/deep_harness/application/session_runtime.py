"""Session configuration and branching authority, independent of transport entry points."""

from deep_harness.application.capabilities import WEB_TOOLS
from deep_harness.application.tools import tool_definitions
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.branches import BranchLedger
from deep_harness.infrastructure.policies import PolicyLedger
from deep_harness.runtime.harnesses import harness_contract


class SessionRuntime:
    def __init__(self, store, scope, model, limits, mcp=None, web_settings=None):
        self.store = store
        self.scope = scope
        self.model = model
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
        version=3,
    ):
        identity = dict(self.model.identity)
        if version == 1 and identity.get("provider") == "ollama":
            identity.pop("reasoning", None)
        result = {
            "version": version,
            "engine": engine,
            "workspace": str(self.scope.workspace),
            "model": identity,
            "tools": tool_definitions(tools, mcp_catalog),
            "execution": self.limits.model_dump(),
        }
        if version >= 3:
            result["context_projection"] = "deep-turns-v1" if engine == "deep" else "standard-v1"
        if version >= 2:
            result["model_request_timeout"] = self.model.request_timeout
            if self.web_settings is not None and set(tools).intersection(WEB_TOOLS):
                result["web"] = {
                    key: value
                    for key, value in self.web_settings.model_dump(mode="json").items()
                    if not key.endswith("_env") and key not in {"searxng_url", "firecrawl_url"}
                }
                result["web"]["endpoint_hash"] = fingerprint(
                    [self.web_settings.searxng_url, self.web_settings.firecrawl_url]
                )
        if knowledge is not None:
            result["knowledge"] = knowledge
        if harness != "general":
            result["harness"] = harness
            result["harness_contract"] = harness_contract(harness)
        mcp_names = [name for name in tools if name.startswith("mcp__")]
        if mcp_names:
            result["mcp_bindings"] = {name: mcp_catalog[name] for name in mcp_names}
        return result

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
        binding = await self.policies.get(session.id)
        knowledge = binding["snapshot"].get("knowledge") if binding else None
        catalog = await self.catalog_for(session.tools)
        await self.policies.assert_matches(
            session.id,
            self.snapshot(
                session.tools,
                engine=session.engine,
                harness=session.harness,
                knowledge=knowledge,
                mcp_catalog=catalog,
                version=binding["snapshot"].get("version", 1) if binding else 3,
            ),
        )

    async def status(self, session_id):
        status = await self.store.session_status(session_id, "operator")
        binding = await self.policies.get(session_id)
        knowledge = binding["snapshot"].get("knowledge") if binding else None
        catalog = await self.catalog_for(status["session"].tools)
        current = self.snapshot(
            status["session"].tools,
            engine=status["session"].engine,
            harness=status["session"].harness,
            knowledge=knowledge,
            mcp_catalog=catalog,
            version=binding["snapshot"].get("version", 1) if binding else 3,
        )
        saved = binding["snapshot"] if binding else {}
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
