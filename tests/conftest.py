"""All rewrite tests use disposable profile and user directories."""

import sys

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "platforms(*names): native windows/linux/macos tests")


def pytest_collection_modifyitems(items):
    hosts = {"windows": "win32", "linux": "linux", "macos": "darwin"}
    for item in items:
        marks = list(item.iter_markers("platforms"))
        if not marks:
            continue
        if len(marks) != 1 or not marks[0].args or set(marks[0].args) - hosts.keys():
            raise pytest.UsageError("Use one platforms marker with windows/linux/macos host names")
        if sys.platform not in {hosts[name] for name in marks[0].args}:
            item.add_marker(pytest.mark.skip(reason="Requires its native host platform"))


@pytest.fixture(autouse=True)
def isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("Harness_HOME", str(home / "harness"))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
