"""Model routing defaults and explicit offline/remote overrides."""

import pytest

from deep_harness.application.config import load_model_settings
from deep_harness.cli import build_host, parser
from deep_harness.runtime.models import DiagnosticModel


def arguments(tmp_path, *options):
    return parser().parse_args(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--workspace",
            str(tmp_path),
            *options,
            "chat",
            "hello",
        ]
    )


async def test_default_cli_routes_to_local_ollama_without_cloud_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    host = build_host(arguments(tmp_path))
    try:
        app = await host.application("default")
        assert app.model.model.model == "qwen3.5:9b"
        assert app.model.model.base_url == "http://127.0.0.1:11434"
        assert app.model.request_timeout == 180
    finally:
        await host.close()


async def test_offline_diagnostic_remains_an_explicit_opt_in(tmp_path):
    host = build_host(arguments(tmp_path, "--provider", "diagnostic"))
    try:
        app = await host.application("default")
        assert isinstance(app.model, DiagnosticModel)
    finally:
        await host.close()


def test_openai_override_does_not_inherit_ollama_endpoint(tmp_path, monkeypatch):
    settings = load_model_settings(provider="openai", name="test-cloud-model")
    assert settings.base_url is None
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="missing"):
        build_host(arguments(tmp_path, "--provider", "openai", "--model", "test-cloud-model"))


def test_explicit_toml_and_cli_model_overrides_are_applied(tmp_path):
    path = tmp_path / "model.toml"
    path.write_text(
        '[model]\nname = "another-local-model"\nbase_url = "http://127.0.0.1:22000"\n',
        encoding="utf-8",
    )
    settings = load_model_settings(path, name="cli-model")
    assert settings.name == "cli-model"
    assert settings.base_url == "http://127.0.0.1:22000"
