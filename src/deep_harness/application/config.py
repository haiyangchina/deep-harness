"""Explicit model configuration without ambient provider auto-selection."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deep_harness.infrastructure.web.settings import WebSettings
from deep_harness.server_config import defaults_for, number, read_operator_config


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
    vision: bool | None = None
    provider: Literal["ollama", "openai", "deepseek", "diagnostic"]
    name: str = Field(min_length=1)
    base_url: str | None = None
    request_timeout_seconds: float = Field(gt=0, le=number("model.request_timeout_seconds_max"))
    context_window_tokens: int = Field(
        default=number("model.context_window_tokens"),
        ge=number("model.context_window_tokens_min"),
        le=number("model.context_window_tokens_max"),
    )
    # Thinking-enabled Ollama models (qwen3.5 and friends) put the answer in a
    # separate `thinking` field when reasoning is on. langchain-ollama does not
    # surface that streamed text in AIMessageChunk.content, so the adapter sees
    # an empty final answer while the provider still bills output tokens. Keep
    # reasoning off by default; set `reasoning = true` in [model] to opt in.
    reasoning: bool = False
    # Ollama unloads an idle model after ~5 minutes, so each later request pays
    # a multi-second reload. ChatOllama forwards keep_alive to /api/chat; keep
    # the local model resident between turns. Accepts durations like "30m" or
    # -1 (keep forever). Performance-only: excluded from the model revision.
    keep_alive: int | str | None = None


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_model_calls: int = Field(
        default=number("execution.max_model_calls"),
        ge=1,
        le=number("execution.max_model_calls_max"),
    )
    max_tool_calls: int = Field(
        default=number("execution.max_tool_calls"), ge=1, le=number("execution.max_tool_calls_max")
    )
    max_run_tokens: int = Field(
        default=number("execution.max_run_tokens"),
        ge=number("execution.max_run_tokens_min"),
        le=number("execution.max_run_tokens_max"),
    )
    # A single `read` page can return OUTPUT_CHARS (24_000) characters. Leave room for
    # several such pages plus prompt, tool schemas and summary instead of letting one
    # full-page read consume the entire budget. The provider byte budget from
    # context_window_tokens remains the effective cap for small windows.
    max_context_characters: int = Field(
        default=number("execution.max_context_characters"),
        ge=number("execution.max_context_characters_min"),
        le=number("execution.max_context_characters_max"),
    )
    summary_characters: int = Field(
        default=number("execution.summary_characters"),
        ge=number("execution.summary_characters_min"),
        le=number("execution.summary_characters_max"),
    )
    wall_seconds: float = Field(
        default=number("execution.wall_seconds"), ge=1, le=number("execution.wall_seconds_max")
    )
    # Ceiling for a single tool await. Without this, one stuck tool call can
    # hang the whole admission until the global wall budget expires, with no
    # progress events emitted in between. Kept below wall_seconds on purpose.
    tool_execution_timeout_seconds: float = Field(
        default=number("execution.tool_execution_timeout_seconds"),
        ge=number("execution.tool_execution_timeout_seconds_min"),
        le=number("execution.tool_execution_timeout_seconds_max"),
    )

    @model_validator(mode="after")
    def context_reserve(self):
        reserve = number("execution.min_context_reserve_chars")
        if self.max_context_characters - self.summary_characters < reserve:
            raise ValueError(
                f"Context budget must leave at least {reserve} characters after summary"
            )
        return self


def _operator_section(path, section, model):
    values = read_operator_config(path).get(section, {})
    shared = defaults_for(section, values)
    return {
        key: value
        for key, value in values.items()
        if key in model.model_fields or key not in shared
    }


def load_execution_settings(path: Path | None = None) -> ExecutionSettings:
    return ExecutionSettings.model_validate(_operator_section(path, "execution", ExecutionSettings))


def load_model_settings(path: Path | None = None, **overrides) -> ModelSettings:
    settings = defaults_for("model", ModelSettings.model_fields)
    file_settings = _operator_section(path, "model", ModelSettings)
    provider = overrides.get("provider") or file_settings.get("provider", settings["provider"])
    if provider != settings["provider"]:
        settings["base_url"] = None
    settings.update(file_settings)
    settings.update({key: value for key, value in overrides.items() if value is not None})
    return ModelSettings.model_validate(settings)


def load_web_settings(path: Path | None = None) -> WebSettings:
    """Overlay per-launch settings on the common YAML defaults."""
    settings = defaults_for("web", WebSettings.model_fields)
    settings.update(_operator_section(path, "web", WebSettings))
    return WebSettings.model_validate(settings)
