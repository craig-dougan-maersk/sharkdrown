"""OpenAI Chat Completions API types — used by the OpenAI provider.

These mirror the public shapes documented at https://platform.openai.com/docs/api-reference/chat.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ChatRole = Literal["system", "user", "assistant", "tool", "developer"]


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    arguments: str = ""


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: ChatRole
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


class ChatCompletionsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage]
    max_tokens: int | None = Field(default=None, alias="max_tokens")
    max_completion_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: str | list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    # OpenAI accepts a string ("auto", "none", "required") or a typed object;
    # we keep this loose and let the converter decide which form to emit.
    tool_choice: str | dict[str, Any] | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    user: str | None = None


class ChatChoice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int
    message: ChatMessage | None = None
    delta: dict[str, Any] | None = None
    finish_reason: str | None = None


class ChatCompletionsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatChoice]
    usage: dict[str, Any] | None = None
    system_fingerprint: str | None = None
