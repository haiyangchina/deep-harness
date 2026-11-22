"""Configured HTTP tools for the existing discovery and dispatch contract."""

import asyncio
import json
import re

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.credentials import read_secret

RESPONSE_BYTES = 48 * 1024


class HttpTools:
    def __init__(self, definitions):
        self.definitions = {}
        self.public_names = {}
        self.catalog = {}
        for name, definition in definitions.items():
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,63}", name):
                raise ValueError(f"Invalid connector name: {name}")
            public = "mcp__http__" + re.sub(r"[^a-zA-Z0-9_]", "_", name)
            if public in self.definitions:
                raise ValueError(f"Connector names collide: {name}")
            self.public_names[name] = public
            self.definitions[public] = definition
            try:
                Draft202012Validator.check_schema(definition.schema_)
                if definition.schema_.get("type") != "object":
                    raise ValueError("Connector input schema must be an object")
                if definition.result_schema is not None:
                    Draft202012Validator.check_schema(definition.result_schema)
            except SchemaError as exc:
                raise ValueError(f"Invalid connector schema: {name}") from exc
            self.catalog[public] = {
                "name": public,
                "description": definition.description,
                "parameters": definition.schema_,
                "schema_hash": fingerprint(definition.schema_),
                "config_hash": fingerprint(definition.model_dump(mode="json")),
            }

    def headers(self, definition):
        if not (definition.secret_env or definition.secret_file):
            return {}
        token = read_secret(definition.secret_env, definition.secret_file)
        if not token:
            raise DomainError("connector_secret_missing", "Connector credential is unavailable")
        return {"Authorization": f"Bearer {token}"}

    async def request(self, definition, method, url, headers, **kwargs):
        async with (
            asyncio.timeout(definition.timeout_seconds),
            httpx.AsyncClient(
                timeout=definition.timeout_seconds, follow_redirects=False, trust_env=False
            ) as client,
            client.stream(method, url, headers=headers, **kwargs) as response,
        ):
            if not 200 <= response.status_code < 300:
                raise DomainError(
                    "connector_response", f"Connector returned HTTP {response.status_code}"
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > RESPONSE_BYTES:
                    raise DomainError("connector_response", "Connector response exceeds size limit")
            try:
                return json.loads(body)
            except (ValueError, UnicodeError) as exc:
                raise DomainError("connector_response", "Connector returned invalid JSON") from exc

    async def call(
        self, name, arguments, expected_schema_hash, *, before_dispatch=None, effect_id=None
    ):
        definition = self.definitions[name]
        if self.catalog[name]["schema_hash"] != expected_schema_hash:
            raise DomainError("mcp_schema_changed", "Connector schema changed")
        try:
            Draft202012Validator(definition.schema_).validate(arguments)
        except ValidationError as exc:
            raise DomainError("connector_arguments", "Connector arguments fail validation") from exc
        headers = self.headers(definition)
        headers["Idempotency-Key"] = effect_id or ""
        if before_dispatch is not None:
            await before_dispatch()
        params = {"params": arguments} if definition.method == "GET" else {"json": arguments}
        result = await self.request(
            definition, definition.method, definition.url, headers, **params
        )
        if definition.result_schema is not None:
            try:
                Draft202012Validator(definition.result_schema).validate(result)
            except ValidationError as exc:
                raise DomainError(
                    "connector_response", "Connector output fails validation"
                ) from exc
        return json.dumps(result, ensure_ascii=False)

    async def reconcile(self, name, effect_id):
        definition = self.definitions[name]
        if not definition.reconcile_url:
            raise DomainError("connector_reconcile", "Connector does not support status lookup")
        try:
            return await self.request(
                definition,
                "GET",
                definition.reconcile_url,
                self.headers(definition),
                params={"effect_id": effect_id},
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise DomainError(
                "connector_unavailable", "Connector status could not be read"
            ) from exc
