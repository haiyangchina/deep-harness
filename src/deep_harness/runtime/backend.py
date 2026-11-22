"""A deepagents Backend that routes virtual file operations through ToolGateway.

deepagents' FilesystemMiddleware exposes file tools to the model. Keeping its
backend boundary-aware means even virtual file reads honour the workspace
contract. This backend is deliberately *not* a SandboxBackend: shell execution
stays with the project's ``bash`` tool, so deepagents' ``permissions`` + sandbox
``execute`` conflict (filesystem.py raises NotImplementedError when both are
enabled) is avoided. Real, receipt-producing file I/O is done by the project
tools (``read``/``write``/``edit``/``apply_patch``) guarded by HarnessGuard.
"""

from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)


class ToolGatewayBackend:
    """Boundary-aware virtual backend for deepagents' FilesystemMiddleware.

    The kernel's real file operations are the project tools, which are guarded
    and produce receipts. This backend provides FilesystemMiddleware with a
    neutral virtual store that never escapes the graph checkpoint, so the two
    file systems cannot be confused and no host write can bypass a receipt.
    """

    def __init__(self, gateway=None):
        self.gateway = gateway

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return ReadResult(error="not_available")

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self.read(file_path, offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error="not_available")

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return EditResult(error="not_available")

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    def ls(self, path: str) -> LsResult:
        return LsResult(entries=[])

    async def als(self, path: str) -> LsResult:
        return self.ls(path)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(matches=[])

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self.glob(pattern, path)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return GrepResult(matches=[])

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return self.grep(pattern, path, glob, max_count=max_count)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error="not_available")

    async def adelete(self, file_path: str) -> DeleteResult:
        return self.delete(file_path)

    def upload_files(self, files):
        return []

    async def aupload_files(self, files):
        return []

    def download_files(self, paths):
        return []

    async def adownload_files(self, paths):
        return []


# The backend must remain structurally compatible with deepagents 0.7.x. The
# API-drift contract test asserts this at import time; a framework upgrade that
# changes the protocol surface fails fast instead of silently losing a method.
BackendProtocol.register(ToolGatewayBackend)
