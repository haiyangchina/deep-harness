"""Profile-owned transport to the bundled Pi search router."""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from itertools import count
from pathlib import Path
from urllib.request import getproxies

from deep_harness.application.process_environment import command_environment
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.web.extract import publication_date
from deep_harness.infrastructure.web.models import SearchHit, SearchOptions

_BUNDLE = Path(__file__).with_name("pi_dist") / "search.cjs"
_ERRORS = {
    "quota": "web_rate_limited",
    "auth": "web_auth",
    "credential": "web_auth",
}
_PROXY_ENV = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS",
}


class PiProvider:
    name = "pi"

    def __init__(self, settings):
        self.settings = settings
        self.available = "pi" in settings.routing
        self._lock = asyncio.Lock()
        self._ids = count(1)
        self._process = None
        self._reader = None
        self._directory = None
        self._pending = {}
        self._closed = False

    def _command(self):
        electron = os.getenv("DEEP_HARNESS_PI_ELECTRON")
        node = electron or shutil.which("node")
        if not node:
            raise DomainError("web_unavailable", "Pi search requires Node.js 24+")
        bundle = Path(os.environ.get("DEEP_HARNESS_PI_BUNDLE") or _BUNDLE).resolve()
        if not bundle.is_file():
            raise DomainError("web_unavailable", "Pi search bundle is missing")
        return node, bundle

    @staticmethod
    def supported_filters(_options):
        return {"freshness"}

    async def _start(self):
        # Caller owns the lock, including during restart and teardown.
        if self._closed:
            raise DomainError("web_disabled", "Pi search provider is closed")
        if self._reader is not None and not self._reader.done():
            return self._process
        await self._stop()
        node, bundle = self._command()
        directory = tempfile.TemporaryDirectory(prefix="deep-harness-pi-")
        self._directory = directory
        try:
            config = {
                "webSearch": {"allowedProviders": self.settings.pi_providers},
                "searchRouting": {
                    "providers": self.settings.pi_providers,
                    "fallbackOn": [
                        "empty", "network", "quota", "transient", "invalid-response", "unsupported",
                    ],
                    # Leave time for the worker to send the response before Python cancels.
                    "timeoutMs": max(1, int(self.settings.provider_timeout_seconds * 900)),
                },
            }
            (Path(directory.name) / "web-search.json").write_text(json.dumps(config), "utf-8")
            env = command_environment()
            env.update({
                key: value for key, value in os.environ.items() if key.upper() in _PROXY_ENV
            })
            # Match HTTPX's effective proxy discovery on Windows. Node's
            # environment proxy support does not read the system proxy itself.
            for scheme, value in getproxies().items():
                name = f"{scheme.upper()}_PROXY"
                if name in _PROXY_ENV and name not in env and name.lower() not in env:
                    env[name] = value
            keys = {
                "bocha": ("BOCHA_API_KEY", self.settings.bocha_api_key_env),
                "brave": ("BRAVE_API_KEY", self.settings.brave_api_key_env),
                "exa": ("EXA_API_KEY", self.settings.exa_api_key_env),
                "tavily": ("TAVILY_API_KEY", self.settings.tavily_api_key_env),
            }
            for provider in self.settings.pi_providers:
                if provider in keys:
                    name, reference = keys[provider]
                    if key := os.environ.get(reference):
                        env[name] = key
            env["PI_CODING_AGENT_DIR"] = directory.name
            env["NODE_USE_ENV_PROXY"] = "1"
            if os.getenv("DEEP_HARNESS_PI_ELECTRON"):
                env["ELECTRON_RUN_AS_NODE"] = "1"
            self._process = await asyncio.create_subprocess_exec(
                node,
                str(bundle),
                cwd=directory.name,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                limit=512 * 1024,
            )
            self._reader = asyncio.create_task(self._read(self._process))
            return self._process
        except BaseException:
            await self._stop()
            raise

    async def _read(self, proc):
        try:
            while line := await proc.stdout.readline():
                reply = json.loads(line)
                future = self._pending.get(reply["id"])
                if future is not None and not future.done():
                    future.set_result(reply)
        except (OSError, ValueError, KeyError, TypeError):
            with suppress(ProcessLookupError):
                proc.kill()
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        DomainError("web_backend", "Pi search worker disconnected")
                    )

    async def _stop(self):
        proc, self._process = self._process, None
        if proc is not None:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1)
            except TimeoutError:
                if proc.returncode is None:
                    with suppress(ProcessLookupError):
                        proc.kill()
                await proc.wait()
            if self._reader is not None:
                await self._reader
                self._reader = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None

    async def close(self):
        async with self._lock:
            self._closed = True
            await self._stop()

    async def search(self, query, limit):
        return await self.search_with_options(query, limit, SearchOptions())

    async def search_with_options(self, query, limit, options):
        request_id = next(self._ids)
        request = {"id": request_id, "query": query, "limit": limit}
        if options.freshness:
            request["freshness"] = options.freshness
        async with self._lock:
            proc = await self._start()
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
        try:
            proc.stdin.write(json.dumps(request).encode() + b"\n")
            await proc.stdin.drain()
            reply = await future
        except asyncio.CancelledError:
            if not proc.stdin.is_closing():
                cancel = json.dumps({"id": request_id, "cancel": True}).encode() + b"\n"
                with suppress(OSError, RuntimeError):
                    proc.stdin.write(cancel)
            raise
        finally:
            self._pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                future.exception()
        if error := reply.get("error"):
            raise DomainError(_ERRORS.get(error, "web_backend"), "Pi search route failed")
        return [
            SearchHit(
                title=row["title"],
                url=row["url"],
                snippet=row["snippet"],
                published_at=publication_date(row["published_at"]),
                provider=f"pi/{row['provider']}",
            )
            for row in reply["hits"]
        ]

    async def fetch(self, url):
        return None
