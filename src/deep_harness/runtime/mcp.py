"""Explicit profile MCP servers; discover and re-check tool schemas before dispatch."""

import asyncio
import ipaddress
import json
import os
import re
import tomllib
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx2
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from mcp import Client, StdioServerParameters
from mcp.client.auth.extensions.client_credentials import ClientCredentialsOAuthProvider
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError

SERVER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.\-/]{1,80}$")


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=32)
    transport: str
    command: str | None = None
    args: list[str] = Field(default_factory=list, max_length=64)
    url: str | None = None
    token_env: str | None = None
    auth: str = "none"
    client_id_env: str | None = None
    client_secret_env: str | None = None
    issuer: str | None = None
    scope: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_server(self):
        if not SERVER_NAME.fullmatch(self.name):
            raise ValueError("MCP server name needs lowercase letters, digits or underscores")
        if self.transport == "stdio":
            if not self.command or not Path(self.command).is_absolute() or self.url:
                raise ValueError("stdio MCP needs an absolute command and no URL")
        elif self.transport == "http":
            parsed = urlparse(self.url or "")
            if (
                self.command
                or self.args
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("HTTP MCP needs a URL without userinfo")
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("HTTP MCP must use HTTPS or local loopback HTTP")
        else:
            raise ValueError("MCP transport must be stdio or http")
        for key, reference in self.env.items():
            if not key.isidentifier() or not reference.isidentifier():
                raise ValueError("MCP env maps child names to host environment variable names")
        if self.token_env and not self.token_env.isidentifier():
            raise ValueError("MCP token_env must name an environment variable")
        if self.auth not in {"none", "client_credentials"}:
            raise ValueError("MCP auth must be none or client_credentials")
        if self.auth == "client_credentials":
            if self.transport != "http" or self.token_env:
                raise ValueError("MCP OAuth requires HTTP and cannot use token_env")
            if not self.client_id_env or not self.client_secret_env or not self.issuer:
                raise ValueError("MCP OAuth needs client_id_env, client_secret_env and issuer")
            if not self.client_id_env.isidentifier() or not self.client_secret_env.isidentifier():
                raise ValueError("MCP OAuth credentials must name environment variables")
            issuer = urlparse(self.issuer)
            if issuer.scheme != "https" and not (
                issuer.scheme == "http" and issuer.hostname in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("MCP OAuth issuer must use HTTPS or local loopback HTTP")
        elif self.client_id_env or self.client_secret_env or self.issuer or self.scope:
            raise ValueError("MCP OAuth fields require auth=client_credentials")
        return self


class MemoryTokenStorage:
    """Profile-process token cache; no OAuth secret enters session state or disk."""

    def __init__(self):
        self.tokens = None
        self.client_info = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client_info

    async def set_client_info(self, client_info):
        self.client_info = client_info


def read_servers(path: Path):
    if not path.exists():
        return []
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DomainError("mcp_config", "MCP config cannot be read") from exc
    if set(data) != {"servers"} or not isinstance(data["servers"], list):
        raise DomainError("mcp_config", "MCP config must contain only [[servers]] entries")
    try:
        servers = [ServerConfig.model_validate(row) for row in data["servers"]]
    except ValidationError as exc:
        raise DomainError("mcp_config", "MCP server configuration is invalid") from exc
    if len(servers) > 16 or len({server.name for server in servers}) != len(servers):
        raise DomainError("mcp_config", "MCP server names must be unique (maximum 16)")
    return servers


class McpRegistry:
    def __init__(self, scope, *, objects=None):
        self.scope = scope
        self.objects = objects or {}
        self.servers = {row.name: row for row in read_servers(scope.home / "mcp.toml")}
        if set(self.servers) & set(self.objects):
            raise DomainError("mcp_config", "In-process MCP test server duplicates configured name")
        self.catalog_cache = {}
        self.connections = {}
        self.connection_locks = {}
        self.token_storage = {name: MemoryTokenStorage() for name in self.servers}
        self.closed = False

    @asynccontextmanager
    async def client(self, name):
        lock = self.connection_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if self.closed:
                raise DomainError("mcp_closed", "MCP registry is closed")
            if name not in self.connections:
                stack = AsyncExitStack()
                try:
                    client = await self._connect(name, stack)
                except BaseException:
                    await stack.aclose()
                    raise
                self.connections[name] = (stack, client)
            try:
                yield self.connections[name][1]
            except BaseException:
                stack, _ = self.connections.pop(name)
                await stack.aclose()
                raise

    async def close(self):
        self.closed = True
        for name in tuple(self.connections):
            async with self.connection_locks[name]:
                connection = self.connections.pop(name, None)
                if connection:
                    await connection[0].aclose()

    async def _connect(self, name, stack):
        if name in self.objects:
            return await stack.enter_async_context(
                Client(self.objects[name], read_timeout_seconds=60)
            )
        config = self.servers.get(name)
        if config is None:
            raise DomainError("mcp_not_found", "MCP server is not configured for this profile")
        if config.transport == "stdio":
            if not await asyncio.to_thread(Path(config.command).is_file):
                raise DomainError("mcp_command", "Configured MCP executable is missing")
            child_env = {}
            for child_name, host_name in config.env.items():
                value = os.environ.get(host_name)
                if value is None:
                    raise DomainError("mcp_secret", "Configured MCP environment value is missing")
                child_env[child_name] = value
            source = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=child_env,
                cwd=self.scope.workspace,
            )
        else:
            headers = {}
            if config.token_env:
                token = os.environ.get(config.token_env)
                if not token:
                    raise DomainError("mcp_secret", "Configured MCP token is missing")
                headers["Authorization"] = f"Bearer {token}"
            auth = None
            if config.auth == "client_credentials":
                client_id = os.environ.get(config.client_id_env)
                client_secret = os.environ.get(config.client_secret_env)
                if not client_id or not client_secret:
                    raise DomainError("mcp_secret", "Configured MCP OAuth credentials are missing")
                auth = ClientCredentialsOAuthProvider(
                    server_url=config.url,
                    storage=self.token_storage[name],
                    client_id=client_id,
                    client_secret=client_secret,
                    scope=config.scope,
                    issuer=config.issuer,
                )
            hostname = urlparse(config.url).hostname
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname == "localhost"
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(headers=headers, auth=auth, timeout=60, trust_env=not loopback)
            )
            source = streamable_http_client(config.url, http_client=http_client)
        return await stack.enter_async_context(Client(source, read_timeout_seconds=60))

    async def discover(self, server_names=None):
        try:
            return await self._discover(server_names)
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError("mcp_unavailable", "Configured MCP server is unavailable") from exc

    async def _discover(self, server_names=None):
        catalog = {}
        available = set(self.servers) | set(self.objects)
        requested = available if server_names is None else set(server_names)
        if not requested <= available:
            raise DomainError("mcp_not_found", "MCP server is not configured for this profile")
        async with asyncio.timeout(90):
            for name in sorted(requested):
                async with self.client(name) as client:
                    catalog.update(await self._list_catalog(name, client))
                if len(catalog) > 256:
                    raise DomainError("mcp_schema", "MCP catalog exceeds 256 tools")
        self.catalog_cache.update(catalog)
        return catalog

    async def _list_catalog(self, name, client):
        catalog = {}
        seen_cursors = set()
        cursor = None
        while True:
            page = await client.list_tools(cursor=cursor, cache_mode="refresh")
            for tool in page.tools:
                if not TOOL_NAME.fullmatch(tool.name):
                    raise DomainError("mcp_schema", "MCP tool name is unsupported")
                safe = re.sub(r"[^A-Za-z0-9_]", "_", tool.name)
                public = f"mcp__{name}__{safe}"
                if public in catalog:
                    raise DomainError("mcp_schema", "MCP tool names collide")
                schema = tool.input_schema
                if (
                    not isinstance(schema, dict)
                    or schema.get("type") != "object"
                    or len(json.dumps(schema, ensure_ascii=False)) > 32_000
                ):
                    raise DomainError("mcp_schema", "MCP tool schema is unsupported")
                try:
                    Draft202012Validator.check_schema(schema)
                except SchemaError as exc:
                    raise DomainError("mcp_schema", "MCP tool schema is invalid") from exc
                description = tool.description or f"MCP {name}/{tool.name}"
                catalog[public] = {
                    "name": public,
                    "description": description[:4096],
                    "parameters": schema,
                    "server": name,
                    "remote_name": tool.name,
                    "schema_hash": fingerprint(schema),
                    "config_hash": (
                        fingerprint(self.servers[name].model_dump(mode="json"))
                        if name in self.servers
                        else "inprocess"
                    ),
                }
                if len(catalog) > 256:
                    raise DomainError("mcp_schema", "MCP catalog exceeds 256 tools")
            cursor = page.next_cursor
            if not cursor:
                break
            if cursor in seen_cursors:
                raise DomainError("mcp_schema", "MCP tool pagination repeated a cursor")
            seen_cursors.add(cursor)
        return catalog

    async def call(self, public_name, arguments, expected_schema_hash, *, before_dispatch=None):
        parts = public_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            raise DomainError("mcp_not_found", "MCP tool name is invalid")
        async with asyncio.timeout(90):
            async with self.client(parts[1]) as client:
                catalog = await self._list_catalog(parts[1], client)
                spec = catalog.get(public_name)
                if spec is None or spec["schema_hash"] != expected_schema_hash:
                    raise DomainError(
                        "mcp_schema_changed", "MCP tool schema changed during this session"
                    )
                if before_dispatch is not None:
                    await before_dispatch()
                result = await client.call_tool(
                    spec["remote_name"], arguments, read_timeout_seconds=60
                )
        payload = result.model_dump(mode="json", exclude_none=True)
        serialized = json.dumps(payload, ensure_ascii=False)
        if len(serialized) > 64_000:
            raise DomainError("mcp_result_large", "MCP result exceeds 64,000 characters")
        if result.is_error:
            raise DomainError("mcp_tool_error", "MCP tool reported an error after dispatch")
        return serialized
