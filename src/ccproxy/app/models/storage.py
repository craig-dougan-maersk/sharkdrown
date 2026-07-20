"""Internal storage DTOs for captured requests and responses.

These are deliberately simpler than the public API DTOs: they hold the
data the dashboard needs to render and filter on, not every detail of the
original wire format.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RequestPath = Literal["passthrough", "converted", "subagent"]


class ResponseLog(BaseModel):
    """Captured response data for a single request."""

    model_config = ConfigDict(extra="allow")

    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
    body_text: str | None = None
    response_time_ms: int | None = None
    is_streaming: bool = False
    streaming_chunks: list[str] = Field(default_factory=list)
    completed_at: datetime | None = None

    @property
    def assembled_text(self) -> str:
        """For streaming responses, reconstruct the assistant's text from
        ``content_block_delta`` events whose ``delta.type == "text_delta"``.

        Anthropic's streaming protocol sends the model's reply as a
        sequence of small JSON frames; a typical "Hello world" reply
        arrives as ~3–15 separate ``data: {...}`` lines. Showing the raw
        chunks in the dashboard is useful for debugging the wire
        protocol but is unreadable when you actually want to see *what
        the model said* — this property joins them back together.

        Returns an empty string for non-streaming responses (use
        ``body_text`` for those).
        """
        if not self.is_streaming or not self.streaming_chunks:
            return ""
        out: list[str] = []
        for chunk in self.streaming_chunks:
            payload = chunk
            if payload.startswith("data:"):
                payload = payload[len("data:") :].lstrip()
            try:
                obj = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                continue
            if obj.get("type") != "content_block_delta":
                continue
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if isinstance(text, str):
                    out.append(text)
        return "".join(out)


class PromptGrade(BaseModel):
    """LLM self-evaluation grading stored against a request.

    Reserved for the (currently unimplemented) ``/api/grade-prompt`` endpoint
    referenced by the React UI's commented-out button.
    """

    model_config = ConfigDict(extra="allow")

    score: int = Field(ge=1, le=5)
    feedback: str = ""
    criteria: dict[str, dict[str, Any]] = Field(default_factory=dict)
    improved_prompt: str | None = None
    grading_timestamp: datetime | None = None


class RequestLog(BaseModel):
    """One captured proxied request, persisted to SQLite.

    ``request_path`` records *how* the request was handled — passthrough
    (Anthropic→Anthropic), converted (Anthropic→OpenAI via the protocol
    adapter), or subagent (converted because the SHA256 of the subagent
    system prompt matched a configured mapping).

    ``input_tokens`` / ``output_tokens`` / ``total_tokens`` are extracted
    from the upstream's ``usage`` block (Anthropic) or top-level usage
    fields (OpenAI) when the response is received. ``None`` when the
    upstream didn't include usage (e.g. error responses, older APIs).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    timestamp: datetime
    method: str
    endpoint: str
    status_code: int | None = None
    request_path: RequestPath = "passthrough"
    user_agent: str | None = None
    content_type: str | None = None
    original_model: str | None = None
    routed_model: str | None = None
    routed_provider: Literal["anthropic", "openai"] | None = None
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body: dict[str, Any] = Field(default_factory=dict)
    response: ResponseLog | None = None
    prompt_grade: PromptGrade | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
