"""LLM provider implementations.

The proxy has two providers today: ``AnthropicProvider`` (pass-through) and
``OpenAIProvider`` (full protocol conversion). Both expose a common
``Provider.forward`` method so Phase 6's handler can treat them uniformly.
"""

from ccproxy.app.providers.anthropic import AnthropicProvider
from ccproxy.app.providers.base import Provider
from ccproxy.app.providers.openai import (
    OpenAIProvider,
    convert_anthropic_to_openai,
    transform_openai_response_to_anthropic,
    transform_openai_stream_to_anthropic,
)

__all__ = [
    "AnthropicProvider",
    "OpenAIProvider",
    "Provider",
    "convert_anthropic_to_openai",
    "transform_openai_response_to_anthropic",
    "transform_openai_stream_to_anthropic",
]
