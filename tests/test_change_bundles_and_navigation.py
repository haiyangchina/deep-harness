import base64
import hashlib
import json
import stat
import sys
import textwrap

import pytest

from deep_harness.application.symbols import NavigationArgs
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import delivery
from deep_harness.infrastructure.change_bundles import encode_bundle, load_bundle
from deep_harness.infrastructure.lsp import navigate


def manifest(rows):
    return {
        "format": 1,
        "revision": "a" * 64,
        "baseline_commit": None,
        "files": rows,
        "verification": None,
    }


def change(target, data):
    return {
        "path": target.name,
        "before": {
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "mode": stat.S_IMODE(target.stat().st_mode),
        },
        "after": {
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": stat.S_IMODE(target.stat().st_mode),
        },
        "data": base64.b64encode(data).decode("ascii"),
    }


async def test_bundle_binary_tampering_partial_writes_and_reconciliation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    a, b = source / "a.bin", source / "b.txt"
    a.write_bytes(b"\x00original")
    b.write_bytes(b"original")
    rows = [change(a, b"\x00changed"), change(b, b"changed")]
    identifier, raw = encode_bundle(manifest(rows))
    tampered = json.loads(raw)
    tampered["manifest"]["files"][0]["data"] = "AA=="
    with pytest.raises(DomainError, match="Invalid change package"):
        load_bundle(json.dumps(tampered).encode())
    with pytest.raises(DomainError, match="Invalid change package"):
        encode_bundle(manifest([{**rows[0], "path": "../outside"}]))

    host = ProjectHost(tmp_path / "state", source)
    try:
        app = await host.application("default")
        preview = await app.imported_bundle(raw=raw)
        assert preview["id"] == identifier and preview["diff_truncated"]
        assert a.read_bytes() == b"\x00original"

        atomic_write = delivery.atomic_write
        writes = []

        def fail_second(target, data):
            if target == b:
                writes.append(target)
                raise OSError("mock crash")
            atomic_write(target, data)

        monkeypatch.setattr(delivery, "atomic_write", fail_second)
        for _ in range(2):
            with pytest.raises(DomainError) as error:
                await app.imported_bundle(identifier=identifier, action="apply")
            assert error.value.code == "unknown_effect"
        assert writes == [b]
        report = await app.imported_bundle(identifier=identifier, action="reconcile")
        assert report["observations"] == {"a.bin": ["after"], "b.txt": ["before"]}
        assert a.read_bytes() == b"\x00changed"
        assert b.read_bytes() == b"original"
    finally:
        await host.close()


@pytest.mark.parametrize("language,extension", [("python", ".py"), ("typescript", ".ts")])
async def test_language_server_returns_only_workspace_locations(
    tmp_path, monkeypatch, language, extension
):
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / ("module" + extension)
    target.write_text("symbol = 1", encoding="utf-8")
    fake = tmp_path / "server.py"
    fake.write_text(
        textwrap.dedent(r"""
        import json, sys

        def send(value):
            data = json.dumps(value).encode()
            sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            sys.stdout.buffer.flush()

        while line := sys.stdin.buffer.readline():
            if not line.lower().startswith(b"content-length:"):
                break
            size = int(line.split(b":")[1])
            assert sys.stdin.buffer.readline() == b"\r\n"
            request = json.loads(sys.stdin.buffer.read(size))
            if request["method"] == "initialize":
                send({"id": request["id"], "result": {"capabilities": {}}})
            if request["method"] == "textDocument/definition":
                uri = request["params"]["textDocument"]["uri"]
                send({"id": request["id"], "result": [
                    {"uri": uri, "range": {"start": {"line": 0, "character": 0}}},
                    {"uri": uri + "/../../outside", "range": {"start": {"line": 0}}},
                ]})
        """),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "DEEP_HARNESS_LANGUAGE_SERVERS",
        json.dumps(
            {
                language: [sys.executable, str(fake)],
            }
        ),
    )
    output = await navigate(
        root,
        target,
        NavigationArgs(path=target.name, operation="definition"),
        lambda name: (root / name).resolve(),
    )
    assert output == [{"path": target.name, "line": 1, "character": 0}]
