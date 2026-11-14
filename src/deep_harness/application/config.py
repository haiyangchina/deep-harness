"""Explicit model configuration without ambient provider auto-selection."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["ollama", "openai", "diagnostic"]
    name: str = Field(min_length=1)
    base_url: str | None = None
    request_timeout_seconds: float = Field(gt=0, le=1800)
    context_window_tokens: int = Field(default=32768, ge=4096, le=1_000_000)
    # Thinking-enabled Ollama models (qwen3.5 and friends) put the answer in a
    # separate `thinking` field when reasoning is on. langchain-ollama does not
    # surface that streamed text in AIMessageChunk.content, so the adapter sees
    # an empty final answer while the provider still bills output tokens. Keep
    # reasoning off by default; set `reasoning = true` in [model] to opt in.
    reasoning: bool = False


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_model_calls: int = Field(default=16, ge=1, le=128)
    max_tool_calls: int = Field(default=64, ge=1, le=1024)
    max_run_tokens: int = Field(default=100_000, ge=100, le=10_000_000)
    # A single `read` page can return OUTPUT_CHARS (24_000) characters. Leave room for
    # several such pages plus prompt, tool schemas and summary instead of letting one
    # full-page read consume the entire budget. The provider byte budget from
    # context_window_tokens remains the effective cap for small windows.
    max_context_characters: int = Field(default=96_000, ge=2000, le=1_000_000)
    summary_characters: int = Field(default=2000, ge=256, le=4000)
    wall_seconds: float = Field(default=600, ge=1, le=3600)

    @model_validator(mode="after")
    def context_reserve(self):
        if self.max_context_characters - self.summary_characters < 1024:
            raise ValueError("Context budget must leave at least 1024 characters after summary")
        return self


def load_execution_settings(path: Path | None = None):
    if path is None:
        return ExecutionSettings()
    with path.open("rb") as stream:
        return ExecutionSettings.model_validate(tomllib.load(stream).get("execution", {}))


def load_model_settings(path: Path | None = None, **overrides) -> ModelSettings:
    defaults_path = Path(__file__).parents[1] / "defaults.toml"
    with defaults_path.open("rb") as stream:
        settings = tomllib.load(stream)["model"]
    file_settings = {}
    if path is not None:
        with path.open("rb") as stream:
            file_settings = tomllib.load(stream)["model"]
    provider = overrides.get("provider") or file_settings.get("provider", settings["provider"])
    if provider != settings["provider"]:
        settings["base_url"] = None
    settings.update(file_settings)
    settings.update({key: value for key, value in overrides.items() if value is not None})
    return ModelSettings.model_validate(settings)
