"""Runtime evidence gathered from installed public interfaces."""

import inspect
import sys
from importlib.metadata import version

from deepagents import create_deep_agent
from deepagents.middleware import FilesystemMiddleware


def report() -> dict:
    packages = (
        "langchain",
        "langchain-core",
        "langgraph",
        "deepagents",
        "langgraph-checkpoint-sqlite",
        "fastapi",
        "sqlalchemy",
    )
    return {
        "python": sys.version.split()[0],
        "interpreter": sys.executable,
        "packages": {name: version(name) for name in packages},
        "capabilities": {
            "deep_checkpointer": "checkpointer"
            in inspect.signature(
                create_deep_agent,
            ).parameters,
            "deep_filesystem_tool_allowlist": "tools"
            in inspect.signature(
                FilesystemMiddleware,
            ).parameters,
        },
        "production_engine": "standard",
        "deep_status": "read-only integration probe; not a session execution engine yet",
    }
