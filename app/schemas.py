"""OpenAPI request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


class ResponseCreateRequest(BaseModel):
    """Queued Responses request.

    The proxy accepts both OpenAI-style `input` and the local simplified
    `system`/`usr` aliases. Extra OpenAI parameters are preserved.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(default="gpt-5.4-mini", description="Model name to request.")
    system: str | None = Field(
        default=None,
        description="Simplified system prompt. Alias also supported: system_prompt.",
    )
    system_prompt: str | None = Field(
        default=None, description="Alias for the simplified system prompt."
    )
    usr: str | None = Field(
        default=None,
        description="Simplified user prompt. Aliases also supported: user, user_prompt, prompt.",
    )
    user: str | None = Field(
        default=None,
        description="Alias for simplified user prompt when input is omitted.",
    )
    user_prompt: str | None = Field(
        default=None, description="Alias for the simplified user prompt."
    )
    prompt: str | None = Field(
        default=None, description="Alias for the simplified user prompt."
    )
    input: str | list[dict[str, Any] | str] | None = Field(
        default=None,
        description="OpenAI Responses-style input. If present, it takes precedence over system/usr aliases.",
    )
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        description="Reasoning effort override. Defaults to the server setting, currently low.",
    )
    reasoning: dict[str, Any] | None = Field(
        default=None,
        description="Responses-style reasoning object. `reasoning.effort` is also supported.",
    )
    stream: bool | None = Field(
        default=False, description="Must be false. Streaming is not supported."
    )


class ChatCompletionCreateRequest(BaseModel):
    """Queued Chat Completions request."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(default="gpt-5.4-mini", description="Model name to request.")
    messages: list[dict[str, Any]] = Field(
        default_factory=list, description="OpenAI Chat Completions messages."
    )
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        description="Reasoning effort override. Defaults to the server setting, currently low.",
    )
    reasoning: dict[str, Any] | None = Field(
        default=None,
        description="Alias object. If provided, `reasoning.effort` is converted to `reasoning_effort`.",
    )
    stream: bool | None = Field(
        default=False, description="Must be false. Streaming is not supported."
    )


class QueuedResponse(BaseModel):
    id: str
    object: Literal["response", "job"]
    created_at: int
    status: Literal["queued", "in_progress", "completed", "failed", "cancelled"]
    background: bool | None = None
    endpoint: str | None = None
    metadata: dict[str, Any]


class CompletedResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: str
    status: str
    output_text: str | None = None
    output: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None
